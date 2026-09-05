# ContextMesh

A multi-tenant Rust service that turns source evidence into shared, contextual memory for agents. PostgreSQL is the durable store. Run one combined process initially; scale API replicas and inference workers independently as adoption grows.

ContextMesh plugs into existing harnesses and bots through HTTP, a Rust client, and stdio MCP. It does not implement Slack or other messaging applications.

## Start locally

Requires Docker Compose:

```sh
docker compose up --build -d
export CONTEXTMESH_URL=http://127.0.0.1:8787
export CONTEXTMESH_TOKEN=local-person-token-change-before-sharing
curl -fsS "$CONTEXTMESH_URL/health"
```

The development stack creates PostgreSQL with separate migration and runtime roles, applies migrations, and starts the API and four workers. It binds host ports to loopback. The example identities require the explicit `--allow-dev-auth` flag; omit that flag and use Okta configuration in production.

Without an inference gateway, the initial graph records literal observations and entity associations. Configure an OpenAI-compatible gateway to extract claims from natural language and enable planned retrieval:

```sh
export CONTEXTMESH_MODEL_URL=https://your-gateway.example/v1
export CONTEXTMESH_MODEL=your-model
export CONTEXTMESH_MODEL_KEY=your-key
```

For Compose, these variables are forwarded to the service. Configure them before the first start, or create an `llm` graph afterward. Existing graph configurations do not change automatically.

## Capture and retrieve

```sh
curl -fsS "$CONTEXTMESH_URL/v1/events" \
  -H "Authorization: Bearer $CONTEXTMESH_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"source":"harness","external_id":"message-1","revision":1,"text":"Compare exposure changes within matched cohorts.","context":{"team":"discovery","entities":["type:Exposure"]}}'

curl -fsS "$CONTEXTMESH_URL/v1/query" \
  -H "Authorization: Bearer $CONTEXTMESH_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"query":"Implement an exposure regression investigator","entities":["type:Exposure"],"context":{"team":"discovery"},"agentic":true}'
```

Curation is asynchronous. An immediate query need not include a just-inserted event. Inject `context_text` as sourced contextual data into the next model request. Inspect `memories`, `conflicts`, and `use: revalidate` when deciding what the agent can rely on. The API supplies context rather than an unrestricted final-answer chatbot.

Stable `(source, external_id, revision)` identifiers make ingestion idempotent. Corrections use a higher revision. Corrections replace the source's current contribution in every maintained graph; historical inputs remain auditable until redacted. Source ownership prevents another person from silently replacing a source.

## Build and run without Docker

Use Rust 1.98+ and PostgreSQL 16+. Create the `cm_owner` and `cm_runtime` roles with your own passwords, give `cm_owner` ownership of the database/schema, then:

```sh
cargo build --release --locked
export DATABASE_URL=postgres://cm_owner:password@localhost/contextmesh
./target/release/contextmesh migrate
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/grants.sql
export DATABASE_URL=postgres://cm_runtime:password@localhost/contextmesh
./target/release/contextmesh run --config config/development.json --allow-dev-auth
```

`run` starts HTTP and workers. `serve` starts only HTTP; `worker` runs workers without a listener. Run migrations as a separate deployment step. The service refuses database superuser/BYPASSRLS credentials.

## Operations and integrations

Read the [documentation on the ContextMesh site](https://patrickauld.github.io/ContextMesh/docs/).

- [Architecture](docs/architecture.md): evidence, incremental graphs, inference, isolation, and scaling.
- [API and integration guide](docs/api.md): endpoint contracts, delegated agents, Rust client, MCP, and capture/retrieval hooks.
- [Operations](docs/operations.md): Okta setup, graph rebuilds, audit, redaction, release approval, recovery, and deployment.
- [System validation](docs/testing.md): black-box scenarios and running the suite.
- [Marketing site](https://patrickauld.github.io/ContextMesh/).

## Design boundaries

Tenant isolation is enforced now. Inside a tenant, knowledge is `internal` or `restricted` to named identity-provider groups; granular object/action ACLs remain future work. Approved release policies can use restricted evidence to select an explicitly allowed output. A model cannot release arbitrary paraphrases of restricted material.

Graph versions coexist and update incrementally. There is no A/B assignment, experimentation dashboard, or statistical comparison machinery. Rebuilding with different models is supported; deterministic model re-execution is not promised.

Source quote validation establishes provenance, not semantic truth. Real-model quality and company-wide throughput require workload-specific evaluation. CI exercises actual process and database behavior with a controllable inference gateway.
