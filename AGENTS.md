# ContextMesh engineering guide

ContextMesh is a Rust service with PostgreSQL as its durable state. `site/` is the marketing site; it is deployed independently.

## Boundaries

- `domain.rs`: graph configuration and source-grounded claim contracts.
- `auth.rs`: OIDC verification, delegated identities, group resolution. Tenant identity never comes from a request body.
- `events.rs`: evidence capture, corrections, classification, and redaction.
- `graphs.rs` / `worker.rs`: graph lifecycle and leased incremental derivation. Inference happens outside database transactions.
- `inference.rs`: OpenAI-compatible gateway boundary. Never log prompts, responses, API keys, or provider error bodies.
- `query.rs`: authorized candidate retrieval, graph association, bounded planning, context packets, and receipts.
- `policy.rs`: approved disclosure choices. Restricted free-form model output must never reach the caller.
- `ops.rs`: owner-only audit and operational actions.
- `db.rs` / `migrations/`: tenant-scoped transactions and schema. Every tenant-owned query needs both an explicit tenant predicate and RLS context.
- `api.rs` / `main.rs`: HTTP wiring, runtime lifecycle, CLI, and stdio MCP transport.

Prefer tactical edits in these boundaries over broad refactoring. Keep source contracts independent of transport details. Add a module when a new responsibility warrants it; do not split crates just to mirror folders.

## Invariants

1. Every claim and edge retains an evidence path. Source corrections are new revisions.
2. Redaction removes retrievability across all graph versions and cannot be undone by replay, a stale worker, or a receipt.
3. Retroactive classification invalidates dependent release policies; reapproval is explicit.
4. An agent acts for a person, inherits current stored groups, and never gains owner privileges through delegation.
5. All inference outputs are untrusted. Source quote matching establishes provenance, not factual truth or semantic entailment.
6. Graph configurations are immutable. New models or representation parameters require a new graph. Existing graphs receive incremental source events until archived.
7. Audit entries contain identifiers and operational metadata, not source payloads or answers.

## Verification

Run `cargo fmt --check`, `cargo clippy --locked --all-targets -- -D warnings`, and `cargo build --locked`. The primary regression suite is `python3 tests/e2e.py`, using real service processes, real PostgreSQL, and a controllable inference gateway. The CI workflow provisions isolated owner/runtime DB roles.

Prefer black-box tests for behavior, races, process failure, and access boundaries. Do not replace PostgreSQL with an in-memory fake. Never claim a test ran when only compilation succeeded. Do not add credentials or local database artifacts to git.

Migrations already deployed must not be edited: add a new migration. Public APIs must fail closed on identity and disclosure errors. Preserve bounded work, explicit retry states, and content-free error responses.
