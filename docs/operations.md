# Operations

## Deployment and identities

Start with the combined `contextmesh run` process and PostgreSQL. Split `serve` and `worker` processes when API or curation load warrants independent scaling. Apply migrations as a separate deployment step using `cm_owner`; grant runtime rights with `scripts/grants.sql` and run the service as `cm_runtime`. Runtime credentials must be neither database superusers nor BYPASSRLS roles.

Configure each tenant with an OIDC issuer, audience, and JWKS URL. Okta custom authorization-server access tokens supply the person subject and group claims. The configured administrative group defaults to `contextmesh-admins`. Group names are used directly; ContextMesh does not infer an organizational hierarchy from them.

Development tokens are allowed only with `--allow-dev-auth`. Never enable that flag in production. Example configurations are in `config/`.

People create short-lived delegated agent tokens through `POST /v1/agents`. An agent acts for its person and reads their current stored groups; it cannot delegate again or perform administrative actions. Revoke an individual token through `/v1/agents/{id}/revoke`, or suspend a principal through `/v1/principals/status`.

OIDC group changes become visible when the person's token is processed again or an operator updates status. This release does not implement background Okta/SCIM synchronization. Local revocation increments the tenant security epoch, checked before context/inference output is committed.

## Inference and curation

Set `CONTEXTMESH_MODEL_URL` to an OpenAI-compatible `/v1` gateway, `CONTEXTMESH_MODEL` to the selected curator model, and `CONTEXTMESH_MODEL_KEY` to its credential. The gateway uses JSON chat completions and bounded responses. Prompts, responses, provider errors, and credentials must not be logged.

Without a configured gateway, capture/context work over original records. With one, new captured records schedule curation jobs. Workers read chronological conversation material and relevant notes, preserve the actual input manifest, and append new records. Generated notes are not automatically re-enqueued. Model changes apply to later runs; prior notes retain their recorded derivation identity. Gateway aliases should resolve to stable model versions when reproducibility matters.

Inspect `/v1/status`, `/v1/jobs`, and `/v1/metrics` for backlog, failures, and queue age. Jobs retain bounded attempts and leases so workers can recover after process failure. A failure or malformed model response never publishes a partial note. Queue state is the only curation lifecycle; records do not have pending/active/promoted flags.

The current worker processes bounded windows and reports coverage in derivation metadata. Do not interpret a bounded window as a guaranteed complete re-read of every historical message. Evaluate long conversations and curation cost against your workload before increasing limits.

## Release policies

An administrator approves a bounded disclosure rule:

```json
{
  "name": "capacity-guidance",
  "purpose": "capacity-planning",
  "audiences": ["engineering"],
  "instruction": "Choose conservative when the evidence requires preserving current capacity; otherwise abstain.",
  "outputs": {"conservative": "Plan within the current approved capacity envelope."},
  "evidence": ["1d5f325a-8158-4f5b-abfe-cd47391163eb"]
}
```

Creation is approval. A separate evaluator may inspect exactly the approved record evidence, including inherited restrictions, and select an approved output key or abstain. The caller receives only the corresponding literal output text. No unrestricted model paraphrase or hidden provenance is released. The choice itself conveys information, so approval authorizes that disclosure.

Supersession, reclassification, and redaction invalidate affected policies. Reapproval creates a policy with current evidence. Explicit revoke disables the rule immediately. Administrative policy metadata is never included in ordinary context results.

## Redaction and reclassification

`POST /v1/records/{id}/classification` changes only visibility/groups; it never changes content or project/conversation identity. Derived records remain subject to the current access restrictions of every input ancestor. Dependent release approvals are invalidated.

`POST /v1/records/{id}/redact` makes the record and its dependent records unavailable across context, direct reads, and curation. Tombstone IDs and content-free audit information remain. The operation is irreversible, and reusing a deleted ID is rejected. Privacy mutation and worker publication serialize so an in-flight extraction cannot revive removed material.

The retrievability guarantee applies to the service, not information already sent to a client or inference gateway. Operators control those systems' retention. Restore procedures must apply later deletions before opening the restored service.

## Backup and restore

Back up PostgreSQL, deployment configuration, and a separately retained content-free deletion export. Never include API keys in deletion exports or evaluation artifacts.

```sh
python3 scripts/redactions.py export deletions.json
```

After restoring a backup, keep access restricted to operators. Point `CONTEXTMESH_URL` and `CONTEXTMESH_TOKEN` at the restored service and inspect the deletion replay:

```sh
python3 scripts/redactions.py apply deletions.json
python3 scripts/redactions.py apply deletions.json --execute
```

The ledger is tenant-bound. Reapply all deletion exports newer than the snapshot, inspect failures, and verify record/context denial before reopening access. A database snapshot cannot discover deletion requests that occurred after it was created. Local harness outboxes and previously exported transcripts require their own retention controls.

Migration 0003 replaces the pre-release event/claim/graph schema and intentionally discards its knowledge. It is not a migration for preserving prior production data. Export required earlier material before deployment or create a fresh database.

## Audit and limits

`GET /v1/audit` returns identifiers and operational metadata; it does not retain source text or full context responses. `GET /v1/records/{id}` provides authorized lineage. There is no persisted context session or receipt database.

All lineage traversal, retrieval, upload, output, and inference paths are bounded. Overflow must fail closed rather than disclose partially checked ancestry. Observe queue age, inference spend, and rejection rates when scaling. The application supports multiple API/worker processes against one PostgreSQL writer; company-wide throughput is an evaluation target, not an established benchmark result.
