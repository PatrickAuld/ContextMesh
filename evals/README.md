# Runnable evaluations

These evaluations exercise the compiled service over HTTP with PostgreSQL. They are deterministic evidence-selection checks, not measurements of inference quality or downstream answer accuracy.

`balanced.py` generates independent fictitious worlds with positive, negative, authorization, tenant, explicit correction, redaction, and lexical-distractor cases. It compares ContextMesh with no-memory, a dependency-free lexical baseline, and a gold-evidence upper-bound control. The lexical baseline is labeled `bm25_structured` because it applies the synthetic suite's visibility and scope oracle before ranking; it is not a plain production BM25 system. ContextMesh receives no gold labels.

The service contract no longer has conflict slots, dependency-version status, graphs, or receipts. The runner does not reconstruct those semantics. Version-like and conflicting source texts remain ordinary retrieval cases and are not treated as special product capabilities.

The 16,000-unit limit uses ContextMesh's documented conservative UTF-8 byte budget for `context_text`; baseline packing counts source characters. These generated worlds normally fit, so the comparison is not a binding-budget claim. No reader model or model judge runs.

Reports contain evidence recall, precision, exact-set match, and false activation. Bootstrap intervals resample whole generated worlds with a fixed seed. Eight default worlds are a regression smoke test, not a power analysis or evidence of product superiority. Seeds are public and none are held out. Hard violations cover unauthorized or redacted content, unknown records, duplicate records, superseded text, and missing evidence on designated positive probes.

The deterministic gateway checks transport and curator input-manifest conformance. It does not establish entailment, extraction accuracy, implicit discovery, learned temporal reasoning, or negative transfer. Public retrieval adapters and their limits are documented in [PUBLIC.md](PUBLIC.md).

Run locally with a compiled binary and disposable owner/runtime database roles:

```sh
python3 -m unittest discover -s evals -p 'test_*.py'
python3 evals/balanced.py --seed 20260908 --worlds 8 --output eval-results/balanced.json
```
