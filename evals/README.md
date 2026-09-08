# Runnable evaluations

The [evaluation strategy](../docs/evaluation.md) is the broader roadmap. This
directory implements its first, zero-provider-cost execution tier. GitHub
Actions runs the compiled service against PostgreSQL and preserves JSON results.

## Balanced first-party design: CMES paired v1

The question is: **does the service return the evidence that applies, preserve
uncertainty, and suppress invalid evidence under a controlled change?** This is
an evidence-stage experiment, not a measurement of an LLM's extraction or task
completion abilities.

`balanced.py` generates eight independent fictitious worlds by default. Each
world has 21 query cases, with counterfactual siblings that change one relevant
condition. Source insertion order and numeric values vary with the seed. All
systems see the same source history and query metadata; gold labels never enter
the query or curator prompt.

| Family | Useful-memory case | Counterfactual/control |
|---|---|---|
| Recall | Direct question; indirect question with a supplied entity | Unanswerable query; lexically similar draft |
| Applicability | Production procedure | Staging procedure; missing project |
| Versions | Exact dependency version | Changed or missing version requires revalidation |
| Conflicts | Both independently sourced values | Must expose the conflicting slot, not choose a winner |
| Authorization | Authorized group member and owner | Same-tenant nonmember; other-tenant canary |
| Corrections | Original route | Revised route replaces current evidence |
| Redaction | Two independent sources | Remove one; retain the other; repeat after rebuild |

Every world is evaluated in four modes: ContextMesh, no memory, a BM25 baseline,
and gold evidence. The BM25 baseline receives the same structured visibility,
applicability, version metadata, and entity-overlap priority as ContextMesh's deterministic curator. It
is deliberately labeled `bm25_structured`; it is not an unaided text-only RAG
system. The gold-evidence mode is an upper-bound control, not a competitor.
The baseline implementation does not inspect `expected` except in gold mode.

The context ceiling is 16,000 characters. These small worlds fit below that
ceiling, so differences are evidence selection rather than context truncation.
ContextMesh's packet-metadata accounting differs from the baseline's source-text
accounting; this experiment does not claim a comparison at a binding token budget.
No reader model, retries of generated answers, or model judge is involved.

### Scoring and gates

- Evidence recall and precision are separate metrics. An empty result on a
  positive case scores zero for both. Negative cases have no recall denominator
  and instead measure false activation. Exact evidence-set match is also reported.
- Version flags, conflict slots, source revisions, exact quotes, receipt links,
  source-read permissions, redaction, and rebuild parity have deterministic
  oracles. Missing required evidence on the explicit-entity contract probes is
  a failure, so a deny-all service cannot pass the gate.
- Ordinary retrieval distractors lower precision; they do not count as a security
  breach. Unauthorized, redacted, inapplicable, or superseded evidence and stale
  guidance marked applicable are hard failures. Quality cannot offset a hard
  failure.
- Report per-family results and paired recall differences. Bootstrap resampling
  uses entire worlds, retaining sibling dependence, with 1,000 seeded replicates.
  These intervals describe the generated suite, not a production population.
  Eight worlds are a smoke experiment, not a power analysis or evidence of
  statistically established product superiority. We do not apply an independent
  Bernoulli `3/n` safety bound to correlated synthetic probes.

The frozen corpus, hash, seed, generator version, source-to-event mapping, commit,
fixture hash, graph configurations, raw packets, receipts, timings, per-case
scores, and violations accompany the report. Failures preserve partial artifacts
and exit nonzero. Seeds are public regression inputs; **none is a held-out set**.
Freeze a separate challenge generator and primary metric before tuning quality.

### Boundaries

The fixture supplies correct structured claims through the same HTTP gateway
contract as real inference. This isolates the service from model variance; it
does not establish semantic entailment, extraction accuracy, implicit entity
discovery, learned temporal reasoning, or downstream negative transfer. The
version checks cover the current API's exact dependency contract, not bitemporal
`as_of` semantics. Rebuild checks compare semantic evidence, not randomly generated
claim IDs. Existing `tests/e2e.py` remains the broader race/recovery/OIDC suite.

Public retrieval adapters and their comparability limits are described in
[PUBLIC.md](PUBLIC.md). CI commands and artifact instructions are in [CI.md](CI.md).
Dense retrieval, rerankers, learned curation, calibrated reader/judge scoring,
MemoryAgentBench, MEME, HaluMem, and downstream task benchmarks remain subsequent
work; this implementation does not fabricate scores for them.

## Local invocation

With the compiled binary and disposable database configured as in
[system validation](../docs/testing.md):

```sh
python3 -m unittest discover -s evals -p 'test_*.py'
python3 evals/balanced.py --seed 20260908 --worlds 8 --output eval-results/balanced.json
```
