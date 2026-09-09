# System validation

The primary suite is `tests/e2e.py`. It talks to compiled Rust service processes over HTTP and stdio, uses real PostgreSQL, and controls inference through a local OpenAI-compatible gateway. It does not substitute an in-memory database.

CI provisions PostgreSQL 16 and separate migration/runtime roles. Formatting, strict Clippy, compilation, and Rust unit tests precede process-level tests. The suite targets record identity, atomic append, lineage, scope isolation, supersession, privacy invalidation, delegated authentication, curation publication, and process recovery. The exact scenarios and assertions are executable in the test source; missing behavior must not be inferred from a successful compilation.

Use a disposable database, export `DATABASE_URL` for the non-superuser runtime role and `MIGRATION_DATABASE_URL` for the migration owner, apply migrations and `scripts/grants.sql`, then:

```sh
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo build --locked
cargo test --locked
python3 tests/e2e.py
python3 -m unittest discover -s evals -p 'test_*.py'
python3 evals/balanced.py --output eval-results/balanced.json
```

Python 3.10+, `psql`, and `openssl` are required. Gateway behavior and OIDC signing keys are fixtures. `CONTEXTMESH_TEST_LOG_DIR` preserves process logs for failure investigation; do not publish keys or private transcripts.

Balanced evaluations compare paired generated worlds against no-memory, BM25, and oracle controls. Public runs compare evidence retrieval using a pinned dataset. Those results do not establish final-answer accuracy or real-model extraction quality. Keep infrastructure conformance, retrieval results, and actual gateway-model evaluations separate. See [evaluation strategy](evaluation.md) and [CI setup](../evals/CI.md).
