# System validation

The primary suite is `tests/e2e.py`. It talks to compiled Rust processes over HTTP and stdio, uses real PostgreSQL, and controls inference through a local OpenAI-compatible HTTP server. It starts two API processes and two worker processes with two workers each. It does not import Rust handlers or substitute an in-memory database.

The workflow `.github/workflows/system-tests.yml` creates a PostgreSQL 16 service, separate migration/runtime roles, builds the application, and runs the suite on pushes and pull requests. Formatting and strict Clippy are required before the black-box run.

Scenarios cover:

- Opaque agent delegation, provenance, revocation, and prevention of delegated administration.
- Cross-tenant API isolation and PostgreSQL row-security enforcement on an unscoped runtime connection.
- Concurrent idempotent ingestion through two API replicas, revision conflict detection, and source correction.
- Rebuilds with different model instructions, simultaneous graph versions, incremental updates, and promotion.
- Applicability, version-sensitive revalidation, and explicit conflict slots.
- Restricted evidence invisibility; approved release output; rejection of unknown model outputs; retroactive classification invalidation.
- Redaction across projections, receipts, source history, and subsequent rebuilds.
- Redaction while curation or privileged query inference is in flight.
- Transient inference retries, invalid grounding, failed-job state, and operator retry.
- Killing every worker process during curation and reclaiming actual expired leases, without manually changing database state.
- OIDC RS256 signatures, audience/issuer/expiry checks, group refresh, and person suspension.
- MCP as a separate stdio process and operational CLI requests.
- Audit immutability, lineage, source search, and absence of sensitive canaries from application logs.
- Durable client capture while an API process is down, replay after restart, and rejection quarantine.
- Owner metrics and classification controls.

The suite requires a disposable, empty database. It initializes its own tenant identities and source history. Export `DATABASE_URL` for a non-superuser runtime role and `MIGRATION_DATABASE_URL` for the migration owner; use `scripts/grants.sql` after the first migration. Then:

```sh
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo build --locked
python3 tests/e2e.py
```

Python 3.10+, `psql`, and `openssl` are required. The inference gateway and OIDC signing key are test fixtures, not production credentials. A real 60-second lease-recovery test intentionally contributes about one minute to runtime. Optional `CONTEXTMESH_TEST_LOG_DIR` preserves process logs for debugging. CI uploads only process logs on failure, not signing keys or source fixture files.

These tests establish service orchestration, persistence, access boundaries, and operational behavior. They do not establish extraction accuracy with a real model, information-theoretic safety of an operator-approved policy, Okta production-tenant connectivity, or company-wide throughput. Those require evaluation with your actual gateway, identity configuration, and representative workload.
