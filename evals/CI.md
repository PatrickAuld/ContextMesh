# Evaluation CI

`evals.service.Service` starts a real API process and worker against PostgreSQL with fresh tenant IDs and development credentials. `DATABASE_URL` is the restricted runtime role and `MIGRATION_DATABASE_URL` is the schema owner. Failed-run process logs are copied to `CONTEXTMESH_TEST_LOG_DIR`.

```python
from evals.service import Service

with Service() as cm:
    record_id = cm.insert("Release Friday", "release-1")
    cm.await_idle()
    packet = cm.query("release")
    assert packet["records"]
```

`Service(mock=True)` opts into the deterministic local inference gateway from `tests/e2e.py`. It validates request shape and produces mechanically supported derived records. The default starts without a gateway; capture and context still work, and jobs finish with the documented no-gateway outcome.

The workflow builds once, provisions separate PostgreSQL owner/runtime roles, runs offline evaluator unit tests, runs the balanced service evaluation, and always uploads results and process logs. The public job runs seeded LoCoMo retrieval cases against fresh managed tenants alongside no-memory, lexical, and full-history controls. Its pinned input is SHA-256 verified. Artifacts record source hashes, selected IDs, exclusions, budgets, packets, errors, and aggregates. They do not report answer accuracy because no answer reader runs.
