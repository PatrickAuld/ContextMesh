# ContextMesh engineering guide

ContextMesh is a Rust service with PostgreSQL as its durable store. `site/` is the separately deployed marketing/docs site.

## Boundaries

- `domain.rs`: the single immutable record contract, scopes, and source supports.
- `records.rs` / `records/`: atomic append, idempotency, lineage, canonical authorization/availability, supersession, and privacy controls.
- `worker.rs`: leased curation jobs that append derived records; inference occurs outside transactions.
- `inference.rs`: OpenAI-compatible gateway. Never log prompts, responses, credentials, or provider error bodies.
- `query.rs`: task context selection and bounded lineage traversal; no persistent query/session state.
- `policy.rs`: approved disclosure choices; restricted free-form model output cannot reach callers.
- `auth.rs` / `ops.rs`: OIDC/delegation, current identity state, audit, and operational controls.
- `db.rs` / `migrations/`: tenant transactions, immutable log, relational lineage, and operational schema.
- `api.rs` / `main.rs` / `client.rs`: transport, runtime lifecycle, and durable harness capture.

Keep one canonical layer for lineage/availability. Do not reintroduce separate event, claim, graph lifecycle, or context receipt models. Prefer direct typed contracts; keep files below 1000 lines and avoid thin wrappers that merely move complexity.

## Invariants

1. Every generated record retains all actual model inputs mechanically, distinct from supporting citations.
2. Supersession is explicit and authorized. Historical records remain inspectable; timestamps alone cannot overwrite knowledge.
3. Authorization covers every transitive input before inference or disclosure. Scope filters never grant permission. Traversal bounds fail closed.
4. Redaction removes retrievability of a record and its descendants. Replay, stale workers, and restore cannot revive deleted content.
5. Reclassification only changes access metadata and invalidates dependent release policies.
6. Agents act for a person, inherit current stored groups, and never gain administrator privileges through delegation.
7. Captured content and inference output are untrusted. Exact quotes establish provenance, not truth.
8. Batch append and worker completion are atomic. All tenant-owned SQL includes explicit tenant predicates and RLS context.
9. Audit/error logs contain identifiers and operational metadata, not payloads or answers.

## Verification

Run `cargo fmt --check`, `cargo clippy --locked --all-targets -- -D warnings`, `cargo build --locked`, and `cargo test --locked`. The regression suite is `python3 tests/e2e.py` against real service processes, PostgreSQL, and a controllable inference gateway. CI provisions separate database owner/runtime roles.

Prefer black-box tests for behavior, races, restarts, and access boundaries. Do not replace PostgreSQL with an in-memory fake. Distinguish deterministic contract tests and synthetic evals from real-model quality. Never claim a suite ran when only compilation succeeded. Keep credentials, local databases, and process logs out of git.

Do not edit previously committed migrations: add another migration. Breaking pre-release changes are allowed when explicitly requested. Preserve bounded work, operational retries, and content-free error responses.
