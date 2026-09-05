# Operator runbook

## Launch and expand

Start with one `contextmesh run` process and managed PostgreSQL. For expansion, deploy `contextmesh serve` replicas behind an HTTPS load balancer and independent `contextmesh worker` replicas. There is no sticky-session requirement. Give each instance the same tenant issuer configuration and gateway settings. Each process has its own PostgreSQL pool; sum `pool_size` across API and worker processes when sizing database connections. A transaction-mode connection pooler can help, since tenant context is transaction-local.

Use a separate `cm_owner` migration role and `cm_runtime` application role. The supplied grants script assumes those role names. Apply migrations once per release and then grants. Never run application traffic as a PostgreSQL superuser, table-owner migration credential, or BYPASSRLS role. Place PostgreSQL on a private network with encrypted transport and credentials supplied by your deployment secret manager.

The supplied Compose stack is a loopback development deployment with intentionally recognizable local credentials. Production uses managed PostgreSQL, `config/production.example.json` adapted to your issuer, no `dev_tokens`, and no `--allow-dev-auth`. The container runs as a non-root user. Termination drains HTTP requests and lets bounded worker requests finish; allow at least 60 seconds for worker shutdown and up to 240 seconds for in-flight query drain.

## Okta

Use a **custom authorization server**, an audience dedicated to ContextMesh, and a groups claim in access tokens. Configure the exact issuer, audience, JWKS URL, and owner group. Verify with `contextmesh request /v1/identity`. ContextMesh validates RS256 signatures, issuer, audience, expiry, and not-before; it does not accept unsigned tokens or trust a tenant in a request body. [Okta authorization servers](https://developer.okta.com/docs/concepts/auth-servers/) and [groups claims](https://developer.okta.com/docs/guides/customize-tokens-groups-claim/main/).

JWKS are cached for five minutes; publish old and new keys with an overlap during rotation. Access tokens should be short-lived. Person group membership refreshes when a valid person token is presented. Agents use that stored membership; local suspension immediately disables both the person and their agents. Connect your existing identity lifecycle system to `/v1/principals/status` if suspension must happen before JWT expiration/session refresh. ContextMesh does not call Okta management APIs or SCIM.

An identity owner can delegate via `/v1/agents`; tokens are hashed at rest and returned only at creation. Delegation cannot produce owner privileges. Revoke compromised tokens through their agent ID. The tenant-wide security epoch invalidates queries racing with revocation.

## Inference configuration

- `CONTEXTMESH_MODEL_URL`: OpenAI-compatible base URL ending in `/v1`.
- `CONTEXTMESH_MODEL`: default model identifier, resolved into new graph configurations.
- `CONTEXTMESH_MODEL_KEY`: optional gateway bearer key.

The service calls `/chat/completions` with JSON response mode, bounded output tokens, and graph-specific temperature/model settings. Gateway calls are part of normal operation, including maintenance and approved restricted-evidence evaluation. The deployment's gateway therefore receives the evidence needed for those operations. ContextMesh does not log those bodies or persist provider error text. Configure gateway logging/retention consistently with your organization's data policy.

Provider transport or schema failures retry through the worker queue. Queries degrade to bounded deterministic retrieval if planning fails; release evaluation fails closed by returning no unapproved guidance. The current gateway adapter uses Chat Completions, not a proprietary sessions protocol.

## Inspect and repair

With an owner token:

```sh
contextmesh request /v1/status
contextmesh request /v1/jobs
contextmesh request '/v1/events?search=forecast'
contextmesh request '/v1/audit?after=0'
contextmesh request /v1/lineage/SOURCE_OR_CLAIM_UUID
```

Source search works before curation finishes. Audit entries identify the person, delegated agent, source event, curation job, and graph. Lineage gives the exact source quote and immutable graph configuration. Query receipts identify surviving accessible claims; they intentionally do not archive answer text or restricted reasoning.

Correct a source by submitting a higher revision using the same `source` and `external_id`. Graphs update incrementally. Earlier revisions remain owner-auditable and stop participating in retrieval. Never edit the underlying source row to make a correction.

## Rebuild and promote

Create a JSON file containing the configuration shown in the API guide, then:

```sh
contextmesh request --method POST --body graph.json /v1/graphs
contextmesh request /v1/graphs
contextmesh request --method POST /v1/graphs/GRAPH_UUID/promote
```

A rebuild backfills current evidence in batches while ingestion continues. Queries can explicitly select the new graph before promotion for inspection. Promotion rejects graphs with pending or failed work. Retry failed jobs after addressing a gateway/configuration issue:

```sh
contextmesh request --method POST /v1/graphs/GRAPH_UUID/retry
```

Configurations cannot be edited in place; create another graph for changed extraction rules. Archive an inactive graph when you no longer need incremental maintenance. Multiple graphs amplify inference cost, so maintain only the useful versions.

## Restrict or remove information

To retroactively mark a source restricted, write:

```json
{"classification":"restricted","read_groups":["finance"]}
```

Then:

```sh
contextmesh request --method POST --body classification.json /v1/events/EVENT_UUID/classification
```

The operation affects all revisions, and all graph reads immediately honor the new classification. Dependent release policies are disabled until explicitly reapproved.

To remove the source from retrieval:

```sh
contextmesh request --method POST /v1/events/EVENT_UUID/redact
```

Redaction applies to the whole source revision family and all graph versions. It clears raw payloads, removes projected claims/edges, cancels jobs, removes dependent release payloads, and invalidates racing queries. It is idempotent and irreversible. Owners also lose access to the payload. Preserve required context through an explicitly authored, separately reviewed replacement source if needed; redacted source identities cannot be revived.

`GET /v1/redactions` exports identifiers without content. The helper script can export a ledger and reapply it to an isolated restored database through the service:

```sh
python3 scripts/redactions.py export redactions.json
python3 scripts/redactions.py apply redactions.json
python3 scripts/redactions.py apply redactions.json --execute
```

Apply defaults to a dry run. The helper verifies tenant identity. Retain a current redaction ledger outside any older database snapshot you might restore.

## Backups and recovery

Use managed PostgreSQL backups and point-in-time recovery. A normal restore to the latest committed state retains redactions. For restoration to an earlier point: keep the restored service inaccessible, replay the newer redaction ledger, verify sensitive canaries through source search and every retained graph, then enable traffic. An old snapshot alone cannot know about later redactions. WAL/backup bytes and external gateway/client copies are outside the application's retrievability guarantee.

Workers can be restarted without job loss. Leases recover after 60 seconds. `failed` jobs require investigation and explicit retry; do not endlessly requeue them. Audit and source tombstones remain authoritative when a projection is discarded.

## Monitor

`/health` is liveness, `/ready` tests database access. `/v1/metrics` exposes tenant job counts and oldest pending-job age in Prometheus text format using an owner credential. `/v1/status` and `/v1/jobs` provide operator-friendly JSON. Logs identify tenant/job failures without including source payloads.

Alert on readiness failure, failed jobs, sustained queue age, elevated HTTP 429 rates, database saturation, and frequent `context_changed_retry`. Observe gateway latency/cost separately. Monitor both queue depth and inference throughput before increasing workers; an overloaded gateway will not improve with more retries.

## Safe deployment sequence

1. Back up PostgreSQL; export the current redaction ledger when restoring older snapshots is possible.
2. Apply new migrations using the migration identity and apply runtime grants.
3. Deploy workers and APIs with the same configuration. Preserve backward compatibility during rolling upgrades.
4. Check readiness, source capture, query, and queue progress using a non-sensitive canary.
5. Retire old replicas after in-flight work drains.

There is no schema rollback command that might silently discard evidence. Prefer forward repair migrations and application rollback only when the newer schema remains compatible.
