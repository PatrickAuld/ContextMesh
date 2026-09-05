"""Export and replay content-free deletion identifiers across database restores."""
import argparse
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
import uuid

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('operation', choices=['export', 'apply'])
parser.add_argument('file', type=Path)
parser.add_argument('--execute', action='store_true')
args = parser.parse_args()
base = os.environ.get('CONTEXTMESH_URL', 'http://127.0.0.1:8787').rstrip('/')
token = os.environ['CONTEXTMESH_TOKEN']


def request(path, post=False):
    req = urllib.request.Request(base + path, data=b'{}' if post else None,
                                 headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


identity = request('/v1/identity')
if not identity['admin'] or identity['agent']:
    raise SystemExit('An owner identity is required.')
if args.operation == 'export':
    after = 0
    events = []
    while True:
        page = request(f'/v1/redactions?after={after}')
        events.extend(e['event_id'] for e in page['events'])
        if not page['events']:
            break
        after = page['next_after']
    data = {'schema': 1, 'tenant_id': identity['tenant'], 'event_ids': events}
    with args.file.open('x') as output:
        json.dump(data, output, indent=2)
        output.write('\n')
    print(json.dumps({'exported': len(events), 'file': str(args.file)}))
else:
    data = json.loads(args.file.read_text())
    if data.get('schema') != 1 or data.get('tenant_id') != identity['tenant']:
        raise SystemExit('Ledger schema or tenant mismatch; no mutations performed.')
    ids = [str(uuid.UUID(value)) for value in data['event_ids']]
    if not args.execute:
        print(json.dumps({'dry_run': True, 'redactions': len(ids), 'tenant_id': identity['tenant']}))
    else:
        applied = absent = 0
        for event in ids:
            try:
                request('/v1/events/' + event + '/redact', post=True)
                applied += 1
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    absent += 1
                else:
                    raise
        print(json.dumps({'applied': applied, 'absent_from_snapshot': absent}))
