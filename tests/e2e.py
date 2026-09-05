"""Process-level tests: compiled service + PostgreSQL + controllable HTTP gateway."""
import base64
import concurrent.futures
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
BINARY = str(Path(os.environ.get('CONTEXTMESH_BINARY', ROOT / 'target/debug/contextmesh')).resolve())
TENANT = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
ADMIN = 'test-admin-credential-not-production'
USER = 'test-user-credential-not-production'
FINANCE = 'test-finance-credential-not-production'
OUTSIDER = 'test-other-credential-not-production'


def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def eventually(fn, timeout=25):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = fn()
            if last:
                return last
        except (urllib.error.URLError, ConnectionError):
            pass
        time.sleep(.2)
    raise AssertionError(f'Condition not met within {timeout}s: {last!r}')


def http(url, path, body=None, token=USER, method=None):
    req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                 method=method or ('POST' if body is not None else 'GET'),
                                 headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    try:
        with urllib.request.urlopen(req, timeout=220) as r:
            content = r.read()
            return r.status, json.loads(content) if content else None
    except urllib.error.HTTPError as e:
        content = e.read()
        try:
            return e.code, json.loads(content)
        except json.JSONDecodeError:
            return e.code, content.decode()


class Gateway(BaseHTTPRequestHandler):
    counts = {}
    gates = {}
    entered = {}
    lock = threading.Lock()
    jwks = {}
    healed = set()

    def log_message(self, *_):
        pass

    def reply(self, code, body):
        data = json.dumps(body).encode()
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self.reply(200, self.jwks)

    def do_POST(self):
        if self.path != '/v1/chat/completions':
            return self.reply(404, {})
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert data['response_format'] == {'type': 'json_object'}
        system = data['messages'][0]['content']
        payload = json.loads(data['messages'][1]['content'])
        if system.startswith('Extract'):
            source = payload['source']
            with self.lock:
                self.counts[source] = self.counts.get(source, 0) + 1
                attempt = self.counts[source]
            if source in self.entered:
                self.entered[source].set()
                self.gates[source].wait(35)
            if source.startswith('TRANSIENT') and attempt == 1:
                return self.reply(503, {'error': 'temporary'})
            context = payload['context']
            entities = context.get('entities', ['type:Exposure'])
            text = ('Variant B: ' if payload['curation_rules'] == 'variant-b' else '') + source
            claim = {'text': text, 'quote': source if not source.startswith('POISON') or source in self.healed else 'not in source',
                     'intent': 'guidance', 'entities': entities, 'applies': context.get('applies', {}),
                     'dependencies': context.get('dependencies', {}), 'slot': context.get('slot'),
                     'relations': [{'from': entities[0], 'relation': 'related', 'to': entities[1]}] if len(entities) > 1 else []}
            answer = {'claims': [claim]}
        elif system.startswith('Plan'):
            answer = {'done': True}
        else:
            question = payload['question']
            if question in self.entered:
                self.entered[question].set()
                self.gates[question].wait(35)
            answer = {'key': 'secret leaked' if 'UNSAFE_POLICY' in payload['question'] else 'conservative'}
        self.reply(200, {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': json.dumps(answer)}}]})


class SystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='contextmesh-e2e-')
        cls.directory = Path(cls.temp.name)
        cls.env = os.environ.copy()
        cls.db = os.environ['DATABASE_URL']
        cls.env['RUST_LOG'] = 'contextmesh=info'
        subprocess.run([BINARY, 'migrate', '--database-url', os.environ.get('MIGRATION_DATABASE_URL', cls.db)], check=True)
        cls.gateway = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
        threading.Thread(target=cls.gateway.serve_forever, daemon=True).start()
        issuer = f'http://127.0.0.1:{cls.gateway.server_port}'
        key = cls.directory / 'key.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048', '-out', str(key)], check=True, capture_output=True)
        modulus = subprocess.check_output(['openssl', 'rsa', '-in', str(key), '-modulus', '-noout'], stderr=subprocess.DEVNULL).decode().strip().split('=')[1]
        Gateway.jwks = {'keys': [{'kty': 'RSA', 'kid': 'test-key', 'use': 'sig', 'alg': 'RS256',
                                  'n': base64.urlsafe_b64encode(bytes.fromhex(modulus)).decode().rstrip('='), 'e': 'AQAB'}]}
        cls.issuer = issuer
        cls.key = key
        config = {'tenants': [{'id': TENANT, 'name': 'Engineering', 'issuer': issuer, 'audience': 'api://contextmesh', 'jwks_url': issuer + '/keys'},
                              {'id': OTHER, 'name': 'Other company'}],
                  'dev_tokens': [{'token': ADMIN, 'tenant_id': TENANT, 'subject': 'owner', 'groups': ['contextmesh-admins']},
                                 {'token': USER, 'tenant_id': TENANT, 'subject': 'engineer', 'groups': ['engineering']},
                                 {'token': FINANCE, 'tenant_id': TENANT, 'subject': 'analyst', 'groups': ['finance']},
                                 {'token': OUTSIDER, 'tenant_id': OTHER, 'subject': 'outsider', 'groups': []}]}
        cls.config = cls.directory / 'config.json'
        cls.config.write_text(json.dumps(config))
        cls.env.update(CONTEXTMESH_MODEL_URL=issuer + '/v1', CONTEXTMESH_MODEL='mock-curator', CONTEXTMESH_MODEL_KEY='mock-provider-key')
        cls.logs = []
        cls.servers = []
        cls.workers = []
        cls.urls = []
        for i in range(2):
            address = f'127.0.0.1:{port()}'
            cls.urls.append('http://' + address)
            cls.servers.append(cls.start('serve', '--listen', address))
        for url in cls.urls:
            eventually(lambda: http(url, '/health')[0] == 200)
        cls.start_workers()

    @classmethod
    def start(cls, mode, *args):
        log = open(cls.directory / f'process-{len(cls.logs)}.log', 'wb')
        cls.logs.append(log)
        return subprocess.Popen([BINARY, mode, '--config', str(cls.config), '--allow-dev-auth', *args], env=cls.env, stdout=log, stderr=log)

    @classmethod
    def start_workers(cls):
        cls.workers = [cls.start('worker', '--workers', '2') for _ in range(2)]

    @classmethod
    def tearDownClass(cls):
        for process in cls.servers + cls.workers:
            if process.poll() is None:
                process.terminate()
        for process in cls.servers + cls.workers:
            try:
                process.wait(50)
            except subprocess.TimeoutExpired:
                process.kill()
        cls.gateway.shutdown()
        for log in cls.logs:
            log.close()
        if os.environ.get('CONTEXTMESH_TEST_LOG_DIR'):
            import shutil
            shutil.copytree(cls.directory, os.environ['CONTEXTMESH_TEST_LOG_DIR'], dirs_exist_ok=True)
        cls.temp.cleanup()

    def call(self, path, body=None, token=USER, code=200, replica=0):
        status, result = http(self.urls[replica], path, body, token)
        self.assertEqual(status, code, result)
        return result

    def insert(self, text, external=None, token=USER, **extra):
        return self.call('/v1/events', {'source': 'e2e', 'external_id': external or text, 'revision': 1, 'text': text, **extra}, token)['event_id']

    def find(self, text, **extra):
        return self.call('/v1/query', {'query': text, **extra})

    def wait_memory(self, text, **extra):
        return eventually(lambda: self.find(text, **extra)['memories'])

    def test_01_identity_isolation_and_delegation(self):
        self.call('/v1/identity', token='invalid', code=401)
        self.call('/v1/identity', token=USER)
        delegated = self.call('/v1/agents', {'name': 'coding-agent', 'ttl_seconds': 3600})
        identity = self.call('/v1/identity', token=delegated['token'])
        self.assertEqual(identity['subject'], 'engineer')
        self.assertEqual(identity['agent'], delegated['agent_id'])
        self.assertFalse(identity['admin'])
        self.call('/v1/graphs', {'name': 'forbidden'}, delegated['token'], code=403)
        self.call('/v1/agents', {'name': 'chain'}, delegated['token'], code=403)
        event = self.insert('delegated provenance', token=delegated['token'])
        self.assertEqual(self.call('/v1/events/' + event)['agent_id'], delegated['agent_id'])
        self.call('/v1/events/' + event, token=OUTSIDER, code=404)
        self.call('/v1/agents/' + delegated['agent_id'] + '/revoke', {})
        self.call('/v1/identity', token=delegated['token'], code=401)

    def test_02_idempotency_concurrent_replicas_and_correction(self):
        body = {'source': 'e2e', 'external_id': 'concurrent', 'revision': 1, 'text': 'Cohort procedure original', 'context': {'entities': ['type:Cohort']}}
        def send(i):
            return http(self.urls[i % 2], '/v1/events', body)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(send, range(12)))
        self.assertTrue(all(code == 200 for code, _ in results), results)
        ids = {r['event_id'] for _, r in results}
        self.assertEqual(len(ids), 1)
        self.wait_memory('Cohort procedure original')
        self.call('/v1/events', {**body, 'text': 'changed at same revision'}, code=409)
        revised = self.call('/v1/events', {**body, 'revision': 2, 'text': 'Cohort procedure corrected'})
        self.wait_memory('Cohort procedure corrected')
        self.assertTrue(all('original' not in m['text'] for m in self.find('Cohort procedure')['memories']))
        self.call('/v1/events/' + next(iter(ids)), code=404)
        self.assertEqual(self.call('/v1/events/' + revised['event_id'])['revision'], 2)

    def test_03_graph_rebuild_incremental_updates_and_promotion(self):
        graph = self.call('/v1/graphs', {'name': 'alternative', 'config': {'mode': 'llm', 'instructions': 'variant-b'}}, ADMIN)['graph_id']
        eventually(lambda: any(g['id'] == graph and g['pending'] == 0 and g['state'] == 'ready' for g in self.call('/v1/graphs')['graphs']))
        self.assertTrue(all(m['text'].startswith('Variant B:') for m in self.find('Cohort', graph_id=graph)['memories']))
        self.insert('Incremental graph maintenance')
        self.wait_memory('Incremental graph maintenance', graph_id=graph)
        self.wait_memory('Incremental graph maintenance')
        eventually(lambda: http(self.urls[0], '/v1/graphs/' + graph + '/promote', {}, ADMIN)[0] == 200)
        self.assertEqual(self.find('Incremental')['graph_id'], graph)
        self.call('/v1/graphs/' + graph + '/archive', {}, ADMIN, code=409)

    def test_04_applicability_dependencies_and_conflicts(self):
        self.insert('Versioned quantization recipe', context={'entities': ['model:A'], 'dependencies': {'library': 'v1'}, 'applies': {'project': 'quantization'}})
        eventually(lambda: self.find('Versioned', context={'project': 'quantization'})['memories'])
        self.assertEqual(self.find('Versioned', context={'project': 'other'})['memories'], [])
        stale = self.find('Versioned', context={'project': 'quantization', 'versions': {'library': 'v2'}})
        self.assertEqual(stale['memories'][0]['use'], 'revalidate')
        current = self.find('Versioned', context={'project': 'quantization', 'versions': {'library': 'v1'}})
        self.assertEqual(current['memories'][0]['use'], 'applicable')
        for variant in ['left', 'right']:
            self.insert('Conflict convention ' + variant, context={'slot': 'cohort-method', 'entities': ['conflict:cohort']})
        eventually(lambda: len(self.find('Conflict convention')['memories']) >= 2)
        self.assertTrue(self.find('Conflict convention')['conflicts'])

    def test_05_restricted_evidence_and_approved_release(self):
        event = self.insert('Budget forecast SECRET_ACQUISITION', token=FINANCE, classification='restricted', read_groups=['finance'])
        eventually(lambda: self.call('/v1/query', {'query': 'SECRET_ACQUISITION'}, FINANCE)['memories'])
        packet = self.find('SECRET_ACQUISITION')
        self.assertNotIn('SECRET_ACQUISITION', json.dumps(packet))
        self.call('/v1/events/' + event, code=404)
        policy = {'name': 'budget-planning', 'purpose': 'capacity', 'audiences': ['engineering'],
                  'instruction': 'Select conservative when next-quarter budget is uncertain.',
                  'outputs': {'conservative': 'Plan within the current approved capacity envelope.'}, 'evidence': [event]}
        self.call('/v1/policies', policy, code=403)
        policy_id = self.call('/v1/policies', policy, ADMIN)['policy_id']
        answer = self.find('How much capacity should we plan?', purpose='capacity')
        self.assertEqual(answer['guidance'], [policy['outputs']['conservative']])
        self.assertNotIn(event, json.dumps(answer))
        self.assertNotIn('SECRET_ACQUISITION', json.dumps(answer))
        self.assertEqual(self.find('UNSAFE_POLICY', purpose='capacity')['guidance'], [])
        self.call('/v1/events/' + event + '/classification', {'classification': 'restricted', 'read_groups': ['finance']}, ADMIN)
        self.assertEqual(self.find('capacity', purpose='capacity')['guidance'], [])
        self.assertFalse(next(p for p in self.call('/v1/policies', token=ADMIN)['policies'] if p['id'] == policy_id)['enabled'])

    def test_06_redaction_all_projections_receipts_and_rebuilds(self):
        event = self.insert('Sensitive removable canary')
        self.wait_memory('Sensitive removable canary')
        packet = self.find('Sensitive removable canary')
        removed_claims = {m['id'] for m in packet['memories'] if m['event_id'] == event}
        self.assertTrue(removed_claims)
        receipt = packet['receipt_id']
        self.assertTrue(self.call('/v1/receipts/' + receipt)['claim_ids'])
        self.call('/v1/events/' + event + '/redact', {}, code=403)
        self.call('/v1/events/' + event + '/redact', {}, ADMIN)
        self.call('/v1/events/' + event, token=ADMIN, code=404)
        remaining = set(self.call('/v1/receipts/' + receipt)['claim_ids'])
        self.assertTrue(remaining.isdisjoint(removed_claims))
        for graph in self.call('/v1/graphs')['graphs']:
            self.assertFalse(any('removable canary' in m['text'] for m in self.find('removable canary', graph_id=graph['id'])['memories']))
        self.call('/v1/events', {'source': 'e2e', 'external_id': 'Sensitive removable canary', 'revision': 2, 'text': 'reintroduced'}, code=409)
        rebuilt = self.call('/v1/graphs', {'name': 'after-redaction', 'config': {'mode': 'literal'}}, ADMIN)['graph_id']
        eventually(lambda: next(g for g in self.call('/v1/graphs')['graphs'] if g['id'] == rebuilt)['state'] == 'ready')
        self.assertFalse(any('removable canary' in m['text'] for m in self.find('removable canary', graph_id=rebuilt)['memories']))

    def test_07_inflight_redaction_cannot_resurrect_claims(self):
        text = 'BLOCK redaction race canary'
        Gateway.gates[text] = threading.Event()
        Gateway.entered[text] = threading.Event()
        event = self.insert(text)
        self.assertTrue(Gateway.entered[text].wait(10))
        self.call('/v1/events/' + event + '/redact', {}, ADMIN)
        Gateway.gates[text].set()
        time.sleep(.5)
        for graph in self.call('/v1/graphs')['graphs']:
            self.assertFalse(any('redaction race canary' in m['text'] for m in self.find('race canary', graph_id=graph['id'])['memories']))

    def test_08_gateway_retry_and_grounding_failure(self):
        text = 'TRANSIENT recoverable inference'
        self.insert(text)
        self.wait_memory(text)
        self.assertGreaterEqual(Gateway.counts[text], 2)
        event = self.insert('POISON invalid grounding')
        eventually(lambda: any(j['event_id'] == event and j['state'] == 'failed' for j in self.call('/v1/jobs', token=ADMIN)['jobs']), timeout=65)
        self.assertFalse(any(m['event_id'] == event for m in self.find('POISON')['memories']))
        Gateway.healed.add('POISON invalid grounding')
        for graph in self.call('/v1/graphs')['graphs']:
            self.call('/v1/graphs/' + graph['id'] + '/retry', {}, ADMIN)
        self.wait_memory('POISON invalid grounding')
        self.call('/v1/events/' + event + '/redact', {}, ADMIN)

    def test_09_worker_process_death_and_lease_recovery(self):
        text = 'BLOCK durable lease recovery'
        Gateway.gates[text] = threading.Event()
        Gateway.entered[text] = threading.Event()
        event = self.insert(text)
        self.assertTrue(Gateway.entered[text].wait(10))
        for process in self.workers:
            process.kill()
            process.wait()
        Gateway.gates[text].set()
        self.start_workers()
        eventually(lambda: all(not any(j['event_id'] == event for j in self.call('/v1/jobs', token=ADMIN)['jobs']) for _ in [0]), timeout=90)
        packet = self.find('durable lease recovery')
        matching = [m for m in packet['memories'] if m['event_id'] == event]
        self.assertEqual(len(matching), 1)

    def token(self, **overrides):
        claims = {'iss': self.issuer, 'aud': 'api://contextmesh', 'sub': 'okta-user', 'exp': int(time.time()) + 300, 'groups': ['engineering']}
        claims.update(overrides)
        encode = lambda x: base64.urlsafe_b64encode(json.dumps(x).encode()).decode().rstrip('=')
        value = encode({'alg': 'RS256', 'kid': 'test-key'}) + '.' + encode(claims)
        signature = subprocess.check_output(['openssl', 'dgst', '-sha256', '-sign', str(self.key)], input=value.encode())
        return value + '.' + base64.urlsafe_b64encode(signature).decode().rstrip('=')

    def test_10_oidc_signatures_claims_groups_and_revocation(self):
        token = self.token()
        self.assertEqual(self.call('/v1/identity', token=token)['subject'], 'okta-user')
        self.call('/v1/identity', token=self.token(aud='wrong'), code=401)
        self.call('/v1/identity', token=self.token(iss='https://attacker.invalid'), code=401)
        self.call('/v1/identity', token=self.token(exp=int(time.time()) - 120), code=401)
        self.call('/v1/identity', token=token[:-12] + 'aaaaaaaaaaaa', code=401)
        agent = self.call('/v1/agents', {'name': 'okta-agent'}, token)['token']
        self.call('/v1/identity', token=self.token(groups=['finance']))
        self.assertEqual(self.call('/v1/identity', token=agent)['groups'], ['finance'])
        self.call('/v1/principals/status', {'subject': 'okta-user', 'disabled': True}, ADMIN)
        self.call('/v1/identity', token=agent, code=401)
        self.call('/v1/identity', token=token, code=403)

    def test_11_mcp_and_operational_cli(self):
        process = subprocess.Popen([BINARY, 'mcp', '--url', self.urls[0], '--token', USER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'memory_extract', 'arguments': {'query': 'Cohort'}}},
        ]
        output, error = process.communicate('\n'.join(map(json.dumps, messages)) + '\n', timeout=30)
        self.assertEqual(process.returncode, 0, error)
        responses = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(len(responses), 3)
        self.assertEqual(len(responses[1]['result']['tools']), 2)
        self.assertFalse(responses[2]['result']['isError'])
        result = subprocess.run([BINARY, 'request', '--url', self.urls[0], '--token', ADMIN, '/v1/status'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['tenant_id'], TENANT)

    def test_12_audit_lineage_rls_and_no_secret_telemetry(self):
        packet = self.find('Cohort')
        lineage = self.call('/v1/lineage/' + packet['memories'][0]['id'], token=ADMIN)['lineage']
        self.assertEqual(len(lineage), 1)
        self.assertIn('job_id', lineage[0])
        self.call('/v1/audit', code=403)
        entries = self.call('/v1/audit', token=ADMIN)['entries']
        self.assertTrue(any(e['action'] == 'event.redact' for e in entries))
        self.assertNotIn('SECRET_ACQUISITION', json.dumps(entries))
        self.assertEqual(self.call('/v1/query', {'query': 'Cohort'}, OUTSIDER)['memories'], [])
        empty = subprocess.check_output(['psql', self.db, '-Atc', 'SELECT count(*) FROM events'], text=True).strip()
        self.assertEqual(empty, '0')
        mutation = subprocess.run(['psql', self.db, '-v', 'ON_ERROR_STOP=1', '-c', f"BEGIN; SET LOCAL app.tenant_id='{TENANT}'; DELETE FROM audit;"], capture_output=True, text=True)
        self.assertNotEqual(mutation.returncode, 0)
        for log in self.directory.glob('*.log'):
            self.assertNotIn('SECRET_ACQUISITION', log.read_text())

    def test_13_outbox_survives_api_outage_and_quarantines_rejection(self):
        self.servers[0].terminate()
        self.servers[0].wait(15)
        body = self.directory / 'capture.json'
        body.write_text(json.dumps({'source': 'client', 'external_id': 'outbox-1', 'revision': 1, 'text': 'Durable outbox evidence'}))
        outbox = self.directory / 'outbox'
        command = [BINARY, 'capture', '--url', self.urls[0], '--token', USER, '--outbox', str(outbox), str(body)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['pending'], 1)
        self.servers[0] = self.start('serve', '--listen', self.urls[0].removeprefix('http://'))
        eventually(lambda: http(self.urls[0], '/health')[0] == 200)
        command = [BINARY, 'flush', '--url', self.urls[0], '--token', USER, '--outbox', str(outbox)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['sent'], 1)
        self.wait_memory('Durable outbox evidence')
        self.assertFalse(list(outbox.rglob('*.pending')))
        body.write_text(json.dumps({'source': 'client', 'external_id': 'invalid', 'revision': 0, 'text': 'invalid revision'}))
        result = subprocess.run([BINARY, 'capture', '--url', self.urls[0], '--token', USER, '--outbox', str(outbox), str(body)], capture_output=True, text=True, timeout=20)
        self.assertEqual(json.loads(result.stdout)['rejected'], 1)
        self.assertEqual(len(list(outbox.rglob('*.rejected'))), 1)

    def test_14_restricted_query_race_fails_closed(self):
        event = self.insert('Restricted race input', token=FINANCE, classification='restricted', read_groups=['finance'])
        self.call('/v1/policies', {'name': 'race', 'purpose': 'race', 'audiences': ['engineering'], 'instruction': 'Select conservative', 'outputs': {'conservative': 'Approved race guidance'}, 'evidence': [event]}, ADMIN)
        question = 'BLOCK policy query race'
        Gateway.gates[question] = threading.Event()
        Gateway.entered[question] = threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(http, self.urls[0], '/v1/query', {'query': question, 'purpose': 'race'})
            self.assertTrue(Gateway.entered[question].wait(10))
            self.call('/v1/events/' + event + '/redact', {}, ADMIN)
            Gateway.gates[question].set()
            code, packet = result.result(timeout=20)
        self.assertEqual(code, 409, packet)
        self.assertEqual(packet['error'], 'context_changed_retry')
        self.assertNotIn('Approved race guidance', json.dumps(packet))

    def test_15_owner_search_classification_and_metrics(self):
        event = self.insert('Operations searchable source')
        self.call('/v1/events?search=searchable', code=403)
        results = self.call('/v1/events?search=searchable', token=ADMIN)['events']
        self.assertTrue(any(e['id'] == event for e in results))
        self.call('/v1/events/' + event + '/classification', {'classification': 'restricted', 'read_groups': ['finance']}, ADMIN)
        self.call('/v1/events/' + event, code=404)
        self.call('/v1/events/' + event, token=FINANCE)
        self.call('/v1/events/' + event + '/classification', {'classification': 'internal'}, ADMIN)
        self.call('/v1/events/' + event)
        req = urllib.request.Request(self.urls[0] + '/v1/metrics', headers={'Authorization': 'Bearer ' + ADMIN})
        with urllib.request.urlopen(req) as response:
            self.assertIn('contextmesh_jobs', response.read().decode())

    def test_16_redaction_ledger_dry_run_and_reapply(self):
        env = self.env.copy()
        env.update(CONTEXTMESH_URL=self.urls[0], CONTEXTMESH_TOKEN=ADMIN)
        path = self.directory / 'redactions.json'
        helper = str(ROOT / 'scripts/redactions.py')
        result = subprocess.run(['python3', helper, 'export', str(path)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(json.loads(result.stdout)['exported'], 0)
        result = subprocess.run(['python3', helper, 'apply', str(path)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['dry_run'])
        result = subprocess.run(['python3', helper, 'apply', str(path), '--execute'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(json.loads(result.stdout)['applied'], 0)
        data = json.loads(path.read_text())
        data['tenant_id'] = OTHER
        path.write_text(json.dumps(data))
        result = subprocess.run(['python3', helper, 'apply', str(path), '--execute'], env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
