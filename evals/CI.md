# Evaluation CI

The evaluation harness runs the compiled ContextMesh binary as real API and
worker processes against PostgreSQL.  It does not substitute an in-memory
database and does not require a provider key.

`evals.service.Service` is the reusable fixture.  A context manager creates
two fresh UUID tenants, dev-only owner/user/guest/finance/outsider credentials, a
temporary config, one API process, and a worker process.  `DATABASE_URL` is
used by the runtime and `MIGRATION_DATABASE_URL` by the migration command.
Rows from another run cannot be read because all API queries are tenant
scoped; a random tenant is also convenient when several evaluations share a
database.  The fixture does not delete rows during teardown, which preserves
failed-run evidence for database inspection.

```python
from evals.service import Service

with Service() as cm:                 # literal graph; no inference gateway
    event_id = cm.insert("Release Friday", "release-1")
    cm.await_idle()
    packet = cm.query("release")
    assert packet["memories"]
```

The public attributes are `url`, `tenant_id`, `other_tenant_id`, `namespace`,
`graph_id`, `owner`, `user`, `guest`, `finance`, and `outsider` (the corresponding
`*_token` aliases are also available).  `request(path, body=None, token=None,
expected=200)` returns decoded JSON and raises `RequestError` when the status
is unexpected.  Use `request_raw` for authorization probes where the status
must be inspected.  `insert` returns the event UUID; `insert_response` returns
the full acknowledgement.  Graph helpers include `graphs`, `create_graph`,
`graph_edges`, `promote_graph`, `archive_graph`, and `retry_graph`.

`Service(mock=True)` is an explicit conformance mode.  It imports the
deterministic `tests/e2e.py` `Gateway`, starts it locally, and configures the
initial graph for deterministic curation.  It is useful for tests that need
claims with entities, applicability, dependencies, slots, and relations.  It
must remain opt-in: normal evaluations use the literal graph and never send
source text to an external model.

The `evaluations.yml` workflow runs on pushes to `main`, pull requests, and
manual dispatch.  It creates isolated owner/runtime PostgreSQL roles, builds
once, runs the Python unittest suite, and writes `eval-results/balanced.json`.
Results and process logs are uploaded with `if: always()` and a short summary
is appended to the GitHub job summary.  Provider credentials are not present
in the workflow environment.

The public job reuses the compiled binary and runs eight seeded LoCoMo cases
on every push/PR, in fresh literal-mode services, alongside no-memory, BM25,
and full-history controls. The upstream data URL is pinned to commit
`3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376` and checked against SHA-256
`79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`.
It evaluates text evidence retrieval, not LoCoMo final-answer accuracy.

Manual dispatch can disable the public job or supply another official LoCoMo
or LongMemEval JSON URL and matching SHA-256. The case limit is 1–64. The runner
records source hashes, selected case IDs, exclusions, budget, raw scores, and
packets. Full-history cases that do not fit are explicitly unavailable, not
silently truncated. No provider credentials are used. The downloaded corpus is
not republished as an artifact; result files can include selected source excerpts.
