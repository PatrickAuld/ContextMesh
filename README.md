# ContextMesh

Shared memory for agents, captured from the work they do. Harnesses append conversation content automatically; ContextMesh curates useful notes and supplies authorized context to the next task. Every derived record retains its input lineage.

The service has one immutable record model, an append API, and a context API. PostgreSQL is the durable store. Run one combined Rust process initially; scale API and worker processes independently.

## Start locally

```sh
docker compose up --build -d
export CONTEXTMESH_URL=http://127.0.0.1:8787
export CONTEXTMESH_TOKEN=local-person-token-change-before-sharing
curl -fsS "$CONTEXTMESH_URL/ready"
```

The development stack creates separate migration/runtime database roles and binds to loopback. Development identities require `--allow-dev-auth`. Production uses Okta/OIDC and delegated agent credentials.

## Capture and prepare context

```sh
curl -fsS "$CONTEXTMESH_URL/v1/records" \
  -H "Authorization: Bearer $CONTEXTMESH_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"records":[{"id":"b02caf86-a30a-4b90-ab91-68c4881c144c","content":"Compare exposure changes within matched cohorts.","scope":{"conversation":"investigation-1","project":"discovery","visibility":"internal"},"metadata":{"role":"user"}}]}'

curl -fsS "$CONTEXTMESH_URL/v1/context" \
  -H "Authorization: Bearer $CONTEXTMESH_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task":"Implement an exposure regression investigator","scopes":[{"project":"discovery"}],"max_tokens":2048}'
```

Retry appends with the same IDs and payloads. Corrections get new IDs and explicit `supersedes` references. Personal visibility is the default; the example explicitly shares within the tenant. Inject returned `context_text` as sourced data and preserve record IDs when capturing subsequent derived notes.

Configure an OpenAI-compatible gateway for asynchronous conversation curation:

```sh
export CONTEXTMESH_MODEL_URL=https://your-gateway.example/v1
export CONTEXTMESH_MODEL=your-curator-model
export CONTEXTMESH_MODEL_KEY=your-key
docker compose up --build -d
```

Without a gateway, capture and context retrieval still work over original records. Curation is asynchronous; an immediate context request need not contain a newly generated note.

## Build and run

Requires Rust 1.98+ and PostgreSQL 16+. Create `cm_owner` and `cm_runtime` roles and a database owned by `cm_owner`, then:

```sh
cargo build --release --locked
export DATABASE_URL=postgres://cm_owner:password@localhost/contextmesh
./target/release/contextmesh migrate
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/grants.sql
export DATABASE_URL=postgres://cm_runtime:password@localhost/contextmesh
./target/release/contextmesh run --config config/development.json --allow-dev-auth
```

`run` starts HTTP and workers; `serve` and `worker` run them separately. Apply migrations before deployment. The runtime rejects superuser/BYPASSRLS credentials.

This is a breaking pre-release redesign. Migration 0003 replaces the earlier event/claim/graph schema; it does not convert old knowledge. Use a fresh database for this release or export anything needed before migrating.

## Documentation

- [Architecture](docs/architecture.md): immutable records, lineage, curation, and access.
- [API and harness integration](docs/api.md): append, context preparation, durable capture, and MCP.
- [Operations](docs/operations.md): identity, inference, jobs, release policies, and redaction.
- [System validation](docs/testing.md): tests against real PostgreSQL and service processes.
- [Evaluation strategy](docs/evaluation.md): quality, isolation, baselines, and release gates.
- [Documentation site](https://patrickauld.github.io/ContextMesh/docs/).

The service enforces tenant isolation and transitive input access. Approved release policies allow only explicitly approved output choices. Source quote validation establishes provenance, not semantic truth. Real-model quality and production throughput require workload-specific evaluation.
