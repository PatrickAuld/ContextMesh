"""Black-box regression suite using real service processes and PostgreSQL."""

import base64, concurrent.futures, json, os, socket, subprocess, tempfile, threading, time, unittest, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BINARY = str(
    Path(
        os.environ.get("CONTEXTMESH_BINARY", ROOT / "target/debug/contextmesh")
    ).resolve()
)
TENANT = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
ADMIN = "test-admin-credential-not-production"
USER = "test-user-credential-not-production"
FINANCE = "test-finance-credential-not-production"
OUTSIDER = "test-other-credential-not-production"


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def eventually(fn, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            value = fn()
            if value:
                return value
        except (OSError, URLError):
            pass
        time.sleep(0.2)
    raise AssertionError("condition not met")


def http(url, path, body=None, token=USER):
    req = Request(
        url + path,
        data=None if body is None else json.dumps(body).encode(),
        method="POST" if body is not None else "GET",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
    )
    try:
        with urlopen(req, timeout=90) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw.decode(errors="replace")


class Gateway(BaseHTTPRequestHandler):
    calls = []
    gates = {}
    entered = {}

    def log_message(self, *_):
        pass

    def reply(self, status, value):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.reply(200, getattr(Gateway, "jwks", {"keys": []}))

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self.reply(404, {})
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Gateway.calls.append(payload)
        assert payload.get("response_format") == {"type": "json_object"}
        request = json.loads(payload["messages"][-1]["content"])
        if "approved_outputs" in request:
            marker = request["question"]
            if marker in self.gates:
                self.entered[marker].set()
                self.gates[marker].wait(30)
            key = "invalid" if "UNAPPROVED" in marker else "allow"
            return self.reply(
                200,
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"key": key}),
                            },
                        }
                    ]
                },
            )
        rows = request.get("conversation_records", [])
        assert isinstance(rows, list) and isinstance(
            request.get("relevant_notes"), list
        )
        assert isinstance(request.get("input_manifest"), list) and isinstance(
            request.get("coverage"), dict
        )
        marker = next(
            (r["content"] for r in rows if "BLOCK" in r.get("content", "")), ""
        )
        if marker in self.gates:
            self.entered[marker].set()
            self.gates[marker].wait(30)
        records = [
            {
                "content": "Derived: " + r["content"],
                "supports": [{"record_id": r["id"], "quote": r["content"]}],
            }
            for r in rows
            if r.get("content")
        ]
        self.reply(
            200,
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"records": records}),
                        },
                    }
                ]
            },
        )


class SystemTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="contextmesh-e2e-")
        cls.directory = Path(cls.temp.name)
        cls.logdir = Path(
            os.environ.get("CONTEXTMESH_TEST_LOG_DIR", cls.directory / "logs")
        )
        cls.logdir.mkdir(parents=True, exist_ok=True)
        cls.env = os.environ.copy()
        cls.db = os.environ["DATABASE_URL"]
        cls.owner_db = os.environ.get("MIGRATION_DATABASE_URL", cls.db)
        subprocess.run([BINARY, "migrate", "--database-url", cls.owner_db], check=True)
        cls.gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        threading.Thread(target=cls.gateway.serve_forever, daemon=True).start()
        cls.issuer = f"http://127.0.0.1:{cls.gateway.server_port}"
        cls.key = cls.directory / "key.pem"
        subprocess.run(
            [
                "openssl",
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(cls.key),
            ],
            check=True,
            capture_output=True,
        )
        modulus = (
            subprocess.check_output(
                ["openssl", "rsa", "-in", str(cls.key), "-modulus", "-noout"],
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
            .split("=")[1]
        )
        Gateway.jwks = {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": "test-key",
                    "use": "sig",
                    "alg": "RS256",
                    "n": base64.urlsafe_b64encode(bytes.fromhex(modulus))
                    .decode()
                    .rstrip("="),
                    "e": "AQAB",
                }
            ]
        }
        config = {
            "tenants": [
                {
                    "id": TENANT,
                    "name": "Engineering",
                    "issuer": cls.issuer,
                    "audience": "api://contextmesh",
                    "jwks_url": cls.issuer + "/keys",
                },
                {"id": OTHER, "name": "Other"},
            ],
            "dev_tokens": [
                {
                    "token": ADMIN,
                    "tenant_id": TENANT,
                    "subject": "owner",
                    "groups": ["contextmesh-admins", "engineering", "finance"],
                },
                {
                    "token": USER,
                    "tenant_id": TENANT,
                    "subject": "engineer",
                    "groups": ["engineering"],
                },
                {
                    "token": FINANCE,
                    "tenant_id": TENANT,
                    "subject": "analyst",
                    "groups": ["finance"],
                },
                {
                    "token": OUTSIDER,
                    "tenant_id": OTHER,
                    "subject": "outsider",
                    "groups": [],
                },
            ],
        }
        cls.config = cls.directory / "config.json"
        cls.config.write_text(json.dumps(config))
        cls.env.update(
            CONTEXTMESH_MODEL_URL=cls.issuer + "/v1",
            CONTEXTMESH_MODEL="mock-curator",
            CONTEXTMESH_MODEL_KEY="fixture",
        )
        cls.logs = []
        cls.servers = []
        cls.urls = []
        for _ in range(2):
            address = f"127.0.0.1:{port()}"
            cls.urls.append("http://" + address)
            cls.servers.append(cls.start("serve", "--listen", address))
        for url in cls.urls:
            eventually(lambda url=url: http(url, "/health")[0] == 200)
        cls.workers = [cls.start("worker", "--workers", "2")]

    @classmethod
    def start(cls, mode, *args):
        log = open(cls.logdir / f"{mode}-{len(cls.logs)}.log", "wb")
        cls.logs.append(log)
        return subprocess.Popen(
            [BINARY, mode, "--config", str(cls.config), "--allow-dev-auth", *args],
            env=cls.env,
            stdout=log,
            stderr=log,
        )

    @classmethod
    def tearDownClass(cls):
        for p in cls.servers + cls.workers:
            if p.poll() is None:
                p.terminate()
        for p in cls.servers + cls.workers:
            try:
                p.wait(20)
            except subprocess.TimeoutExpired:
                p.kill()
        cls.gateway.shutdown()
        for f in cls.logs:
            f.close()
        cls.temp.cleanup()

    def call(self, path, body=None, token=USER, code=200, replica=0):
        status, result = http(self.urls[replica], path, body, token)
        self.assertEqual(status, code, result)
        return result

    def record(self, content, token=USER, rid=None, **extra):
        rid = rid or str(uuid.uuid4())
        return rid, self.call(
            "/v1/records",
            {"records": [{"id": rid, "content": content, **extra}]},
            token,
        )

    def context(self, task, token=USER, **extra):
        return self.call("/v1/context", {"task": task, **extra}, token)

    def sql(self, statement, owner=False, ok=True):
        result = subprocess.run(
            [
                "psql",
                self.owner_db if owner else self.db,
                "-v",
                "ON_ERROR_STOP=1",
                "-qAtc",
                statement,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        return result.stdout.strip()

    def oidc(self, **changes):
        claims = {
            "iss": self.issuer,
            "aud": "api://contextmesh",
            "sub": "oidc-user",
            "exp": int(time.time()) + 300,
            "groups": ["engineering"],
        }
        claims.update(changes)
        enc = (
            lambda x: base64.urlsafe_b64encode(json.dumps(x).encode())
            .decode()
            .rstrip("=")
        )
        value = enc({"alg": "RS256", "kid": "test-key"}) + "." + enc(claims)
        sig = subprocess.check_output(
            ["openssl", "dgst", "-sha256", "-sign", str(self.key)], input=value.encode()
        )
        return value + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")

    def test_identity_delegation_oidc_and_tenants(self):
        self.call("/v1/identity", token="invalid", code=401)
        agent = self.call("/v1/agents", {"name": "agent", "ttl_seconds": 600})
        self.assertEqual(
            self.call("/v1/identity", token=agent["token"])["subject"], "engineer"
        )
        rid, _ = self.record("tenant private", agent["token"])
        self.call("/v1/records/" + rid, token=OUTSIDER, code=404)
        self.call("/v1/agents", {"name": "nested"}, agent["token"], code=403)
        token = self.oidc()
        self.assertEqual(self.call("/v1/identity", token=token)["subject"], "oidc-user")
        for bad in (
            self.oidc(aud="bad"),
            self.oidc(exp=int(time.time()) - 60),
            token[:-8] + "aaaaaaaa",
        ):
            self.call("/v1/identity", token=bad, code=401)

    def test_atomic_batch_concurrency_rollback_and_reuse(self):
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        body = {
            "records": [
                {"id": first, "content": "batch root"},
                {
                    "id": second,
                    "content": "batch child",
                    "inputs": [first],
                    "supports": [{"record_id": first, "quote": "batch root"}],
                },
            ]
        }
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda i: http(self.urls[i % 2], "/v1/records", body), range(12)
                )
            )
        self.assertTrue(all(x[0] == 200 for x in results), results)
        rollback = str(uuid.uuid4())
        self.call(
            "/v1/records",
            {
                "records": [
                    {"id": rollback, "content": "must roll back"},
                    {
                        "id": str(uuid.uuid4()),
                        "content": "bad",
                        "inputs": [str(uuid.uuid4())],
                    },
                ]
            },
            code=400,
        )
        self.call("/v1/records/" + rollback, code=404)
        self.call(
            "/v1/records", {"records": [{"id": first, "content": "changed"}]}, code=409
        )
        self.call("/v1/records/" + first + "/redact", {}, ADMIN)
        self.call(
            "/v1/records",
            {"records": [{"id": first, "content": "batch root"}]},
            code=409,
        )

    def test_transitive_cross_user_and_scope_filter(self):
        root, _ = self.record(
            "finance ancestor",
            FINANCE,
            scope={"visibility": "restricted", "groups": ["finance"]},
        )
        middle, _ = self.record("internal middle", FINANCE, inputs=[root])
        leaf, _ = self.record("internal leaf", FINANCE, inputs=[middle])
        for rid in (middle, leaf):
            self.call("/v1/records/" + rid, token=USER, code=404)
        self.assertNotIn(
            leaf, {r["id"] for r in self.context("internal", USER)["records"]}
        )
        scoped, _ = self.record(
            "alpha only", scope={"visibility": "internal", "project": "alpha"}
        )
        self.assertNotIn(
            scoped,
            {
                r["id"]
                for r in self.context("alpha", scopes=[{"project": "beta"}])["records"]
            },
        )

    def test_duplicate_retry_cannot_bypass_input_authorization(self):
        root, _ = self.record("retry authorization root")
        self.call(
            "/v1/records/" + root + "/classification",
            {"visibility": "restricted", "groups": ["finance"]},
            ADMIN,
        )
        child = str(uuid.uuid4())
        body = {
            "records": [
                {"id": root, "content": "retry authorization root"},
                {"id": child, "content": "must remain atomic", "inputs": [root]},
            ]
        }
        status, _ = http(self.urls[0], "/v1/records", body, USER)
        self.assertIn(status, (400, 403))
        self.call("/v1/records/" + child, token=ADMIN, code=404)

    def test_lineage_beyond_authorization_bound_fails_closed(self):
        parent, _ = self.record("bounded lineage 0", scope={"visibility": "internal"})
        for depth in range(1, 66):
            parent, _ = self.record(
                f"bounded lineage {depth}",
                inputs=[parent],
                scope={"visibility": "internal"},
            )
        self.call("/v1/records/" + parent, code=404)
        status, _ = http(
            self.urls[0],
            "/v1/context",
            {"task": "bounded lineage", "starting_records": [parent]},
            USER,
        )
        self.assertIn(status, (400, 403))

    def test_supersession_stale_note_and_private_successor(self):
        old, _ = self.record("route east", scope={"visibility": "internal"})
        note, _ = self.record("east note", inputs=[old])
        new, _ = self.record("route west", inputs=[old], supersedes=[old])
        ids = {r["id"] for r in self.context("route")["records"]}
        self.assertIn(new, ids)
        self.assertNotIn(old, ids)
        self.assertNotIn(note, ids)
        public, _ = self.record("shared datum", scope={"visibility": "internal"})
        private, _ = self.record(
            "owner revision", ADMIN, inputs=[public], supersedes=[public]
        )
        ids = {r["id"] for r in self.context("shared", USER)["records"]}
        self.assertIn(public, ids)
        self.assertNotIn(private, ids)

    def test_redaction_gates_inflight_worker_and_descendants(self):
        marker = "BLOCK redaction worker"
        Gateway.gates[marker] = threading.Event()
        Gateway.entered[marker] = threading.Event()
        source, _ = self.record(marker)
        self.assertTrue(Gateway.entered[marker].wait(15))
        child, _ = self.record("redaction child", inputs=[source])
        self.call("/v1/records/" + source + "/redact", {}, ADMIN)
        Gateway.gates[marker].set()
        eventually(
            lambda: not any(
                j.get("record_id") == source
                for j in self.call("/v1/jobs", token=ADMIN)["jobs"]
            )
        )
        for rid in (source, child):
            self.call("/v1/records/" + rid, token=ADMIN, code=404)
        self.assertNotIn(marker, json.dumps(self.context("redaction", ADMIN)))

    def test_policy_allowlist_invalidation_and_race(self):
        evidence, _ = self.record(
            "restricted evidence",
            FINANCE,
            scope={"visibility": "restricted", "groups": ["finance"]},
        )
        policy = {
            "name": "release",
            "purpose": "planning",
            "audiences": ["engineering"],
            "instruction": "choose allow",
            "outputs": {"allow": "Finite approved guidance"},
            "evidence": [evidence],
        }
        self.call("/v1/policies", policy, code=403)
        self.call("/v1/policies", policy, ADMIN)
        self.assertEqual(
            self.context("normal", purpose="planning")["guidance"],
            ["Finite approved guidance"],
        )
        self.assertEqual(self.context("UNAPPROVED", purpose="planning")["guidance"], [])
        marker = "BLOCK policy gate"
        Gateway.gates[marker] = threading.Event()
        Gateway.entered[marker] = threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                http,
                self.urls[0],
                "/v1/context",
                {"task": marker, "purpose": "planning"},
            )
            self.assertTrue(Gateway.entered[marker].wait(15))
            self.call("/v1/records/" + evidence + "/redact", {}, ADMIN)
            Gateway.gates[marker].set()
            status, response = future.result(30)
        self.assertEqual(status, 409, response)
        self.assertNotIn("Finite approved guidance", json.dumps(response))

    def test_worker_lease_restart_has_one_derivation(self):
        marker = "BLOCK lease restart"
        Gateway.gates[marker] = threading.Event()
        Gateway.entered[marker] = threading.Event()
        source, _ = self.record(marker)
        self.assertTrue(Gateway.entered[marker].wait(15))
        for worker in self.workers:
            worker.kill()
            worker.wait(10)
        self.sql(
            f"BEGIN; SET LOCAL app.tenant_id='{TENANT}'; UPDATE jobs SET lease_until=now()-interval '1 second' WHERE record_id='{source}'; COMMIT;",
            owner=True,
        )
        Gateway.gates[marker].set()
        self.workers = [self.start("worker", "--workers", "2")]
        eventually(
            lambda: not any(
                j.get("record_id") == source
                for j in self.call("/v1/jobs", token=ADMIN)["jobs"]
            ),
            40,
        )
        count = self.sql(
            f"BEGIN; SET LOCAL app.tenant_id='{TENANT}'; SELECT count(*) FROM records WHERE derivation IS NOT NULL AND '{source}'=ANY(inputs); COMMIT;",
            owner=True,
        )
        self.assertEqual(count.splitlines()[-1], "1")

    def test_sql_rls_immutability_manifest_and_budget(self):
        rid, _ = self.record("immutable SQL")
        self.assertEqual(self.sql("SELECT count(*) FROM records"), "0")
        self.sql(
            f"BEGIN; SET LOCAL app.tenant_id='{TENANT}'; UPDATE records SET content='changed' WHERE id='{rid}'; COMMIT;",
            owner=True,
            ok=False,
        )
        Gateway.calls.clear()
        scope = {"conversation": "manifest-test"}
        first, _ = self.record("manifest first", scope=scope)
        second, _ = self.record("manifest second", inputs=[first], scope=scope)
        request = eventually(
            lambda: next(
                (
                    json.loads(call["messages"][-1]["content"])
                    for call in reversed(Gateway.calls)
                    if second
                    in json.loads(call["messages"][-1]["content"]).get(
                        "input_manifest", []
                    )
                ),
                None,
            )
        )
        self.assertEqual(
            len(request["input_manifest"]), len(set(request["input_manifest"]))
        )
        self.assertTrue({first, second}.issubset(request["input_manifest"]))
        packet = self.context("manifest", max_tokens=80)
        self.assertLessEqual(len(packet["context_text"].encode()), 80)
        self.assertTrue(packet["truncated"])
        self.assertNotIn(
            "restricted evidence",
            json.dumps(self.call("/v1/audit", token=ADMIN)["entries"]),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
