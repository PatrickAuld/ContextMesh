# Immutable memory implementation validation

Validated locally on 2026-09-08, branch `simplify-immutable-records`.

## Implemented

- One immutable record model for captured conversation and curated notes; explicit input, support, and supersession links.
- Three data APIs: append records, read a record, and prepare context. Legacy event and graph modules and routes are removed.
- Harness transcript capture with stable source IDs, revisions, role/channel metadata, lineage preservation, and a durable ordered outbox.
- Durable leased curation over bounded conversation windows and relevant notes, with complete actual input manifests and exact supporting quotes.
- Transitive authorization, historical reads, current-record selection, privacy invalidation, and stale-identity rejection through canonical record helpers.
- Breaking pre-release migration. Existing knowledge tables are discarded; no compatibility conversion is provided.

## Executed checks

| Check | Result |
| --- | --- |
| Rust unit tests, `cargo test --locked` | 5 passed |
| Offline evaluator tests, unittest discovery | 19 passed |
| Formatting and strict Clippy across all targets | Passed |
| Locked Rust build | Passed |
| Python compilation | Passed |
| Documentation build and local link validation | Passed: 8 generated pages, 219 links/anchors |
| Git whitespace/error check | Passed |

The Rust tests cover transcript identity/lineage and outbox batch bounds, retry classification, and async lock contention. The offline evaluator tests validate scoring and adapters; they do not run the memory service against PostgreSQL.

## Review

Implementation was divided among Luna and Sol agents and independently reviewed with the thermo-nuclear code-quality review skill. Review fixes include authorization on idempotent retry, stale identity epochs, supersession traversal, redaction races, restricted policy evaluation, byte-bounded capture, and nonblocking outbox lock acquisition. No remaining code-quality blocker was identified. Required PostgreSQL validation is still outstanding.

Evaluation fixes separate actual supporting evidence from the full influence manifest, detect duplicates by immutable record ID, and give the service and lexical baseline equivalent scope filters and query information.

Canonical availability performs bounded recursive database checks per candidate. Its throughput still needs measurement under representative workloads.

## Not executed

| Evaluation | Status |
| --- | --- |
| Real PostgreSQL black-box suite (10 scenarios) | Pending |
| Paired generated-world retrieval comparisons | Pending; no quality scores available |
| Pinned public benchmark retrieval comparison | Pending; no quality scores available |
| Real-model extraction and downstream answer quality | Not measured |

No usable local PostgreSQL instance was available. GitHub Actions provisions PostgreSQL with separate migration and runtime roles and runs the system and retrieval suites. Automatic approval review rejected the branch push because implementation authorization did not explicitly cover publishing repository contents to GitHub. The branch remains local pending explicit push and CI authorization.

The deterministic inference fixture tests orchestration and lineage. It does not establish real-model extraction quality. Even a successful retrieval evaluation would not establish downstream answer accuracy.
