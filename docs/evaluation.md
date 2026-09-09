# Evaluation strategy

## Immutable records release

The current implementation uses one record log and `/v1/records` + `/v1/context`. This document also retains broader research criteria; graph lifecycle, entity-slot conflicts, automatic version-applicability reasoning, federation, and persisted receipts described below are evaluation targets, not current API promises. An adapter must mark unsupported operations unavailable rather than synthesize behavior or claim a pass.

For this release, append/correction use immutable record IDs and explicit `supersedes`; lineage includes the mechanically attached complete model input manifest as well as source supports. Context packets themselves provide selected record references; the external evaluation runner records requests/results rather than requiring a server receipt. Replace graph rebuild checks with service/worker restart, index reconstruction where implemented, immutable-history inspection, and deletion replay checks. Semantic version applicability requires explicit scoped evidence or a downstream reader and must not be inferred from metadata by the evaluation adapter.

Report deterministic storage/lineage/access conformance separately from retrieval performance and real-model extraction quality. Compare raw capture and curated operation as separate operating points. The first-party suite retains paired worlds and BM25/no-memory/oracle controls; public evidence retrieval retains the pinned LoCoMo corpus and full-history control. No fixture-generated note should be presented as evidence that a real model extracts memory accurately.


Research and proposal · September 5, 2026

Runnable first tier · September 8, 2026: [paired deterministic evaluation](../evals/README.md),
[public retrieval adapters](../evals/PUBLIC.md), and [GitHub Actions execution](../evals/CI.md).
This implements evidence-stage evaluation and service invariants; the real-model
and downstream-task comparisons below remain a roadmap, not completed results.

## Decision

ContextMesh should use two complementary evaluation programs:

1. **Public benchmark adapters** establish external comparability against memory systems, retrieval baselines, and agent harnesses.
2. **ContextMesh Eval Suite (CMES)** gates releases on the properties that public benchmarks do not test: evidence fidelity, identity isolation, applicability and authority, valid-time semantics, replayability, redaction, bounded process-based reads, and downstream task improvement.

Public benchmarks are useful but insufficient. A 2026 review of 52 memory-augmented systems found that 62% of benchmark/system pairs rely on LoCoMo or LongMemEval, only 21% report any efficiency metric, and none combine broad task coverage with comprehensive efficiency reporting. It also shows that a memory substrate can lead on one regime and be dominated in another. ContextMesh should therefore publish a scorecard, not a single “memory accuracy” number. [Harness the Memory](https://arxiv.org/abs/2608.15008)

The principal release question is:

> Given the same downstream model, task, source history, and budget, does ContextMesh cause the agent to make better decisions without exposing, fabricating, misapplying, or silently using invalid knowledge?

## Public evaluation landscape

### Recommended benchmark portfolio

| Priority | Benchmark | What it measures | Why it matters to ContextMesh | Limitation |
|---|---|---|---|---|
| P0 | [LongMemEval](https://arxiv.org/abs/2410.10813) | Information extraction, multi-session reasoning, temporal reasoning, knowledge updates, abstention | Standard external comparison for changing long-term memory | Dialogue QA; weak on procedures, provenance, access control, and action |
| P0 | [MemoryAgentBench](https://arxiv.org/abs/2507.05257) | Accurate retrieval, test-time learning, long-range understanding, selective forgetting under incremental ingestion | Exercises memory operations rather than a static long-context prompt | Aggregates several task families; does not represent enterprise identity or authority |
| P0 | [MEME](https://arxiv.org/abs/2605.12477) | Exact recall, aggregation, tracking, deletion, cascade, and absence over a deterministic dependency DAG | Direct match for dependent claims, update propagation, history retention, and uncertainty | Synthetic and relatively small; no provenance or authorization scoring |
| P0 | [RECON](https://arxiv.org/abs/2607.16716) | Chain reconstruction, invalidation cascades, source conflicts, counterfactuals, and parallel temporal constraints | Closest public match to ContextMesh’s provenance graph and process-based reads | Long-context narrated cases, not a multi-tenant write/read service |
| P0 | [LongMemEval-V2](https://arxiv.org/abs/2605.12493) | Static state, dynamic state, workflow knowledge, environment gotchas, and premise awareness over 25M–115M-token trajectory histories | Best external test of whether accumulated work experience becomes useful organizational knowledge | Context-gathering/QA formulation; does not score capture fidelity, policy, or action directly |
| P0 | [HaluMem](https://arxiv.org/abs/2511.03506) | Hallucination during memory extraction, updating, and memory-based QA | Localizes corruption to the curation stage instead of blaming final retrieval | User-conversation domain; its taxonomy must be mapped to ContextMesh commits |
| P1 | [MemoryArena](https://arxiv.org/abs/2602.16313) | Interdependent multi-session tasks across web navigation, constrained planning, search, and formal reasoning | Tests the full memory-agent-environment loop and whether prior outcomes alter future actions | More expensive and stochastic than evidence-level tests |
| P1 | [Mem2ActBench](https://arxiv.org/abs/2601.19935) | Proactive memory use for tool choice and parameter grounding | Measures action application when the prompt does not explicitly request recall | Personal-assistant/tool domain rather than organizational work |
| P1 | [DreamBench-SWE](https://arxiv.org/abs/2608.20664) | Multi-session software tasks with non-inferable prior evidence and executable hidden oracles | Strong fit for the initial coding-agent market and resistant to answer-judge ambiguity | New benchmark with limited independent replication |
| P1 | [ImplicitMemBench](https://arxiv.org/abs/2604.08064) | One-shot procedural learning, priming, and conditioned first decisions after interference | Useful for implicit procedure activation and “do this automatically next time” behavior | Some cognitive constructs are not organizational-memory requirements |
| P2 | [LoCoMo](https://aclanthology.org/2024.acl-long.747/) | Long conversational QA, event summarization, temporal/causal reasoning, multimodal dialogue | Widely reported comparison point | Mostly static personal conversation; high scores do not predict agentic utility |
| P2 | [BEAM](https://arxiv.org/abs/2510.27246) | Ten memory abilities over coherent conversations up to 10M tokens | Scale and degradation curve | Expensive; primarily context-scale stress rather than enterprise semantics |

### Supporting, non-memory evals

These should contribute scorer implementations or adversarial cases, not become headline ContextMesh benchmarks:

- [AgenticRAGTracer](https://arxiv.org/abs/2602.19127) provides hop-level ground truth for diagnosing bounded search and graph traversal rather than relying only on a final answer.
- [KILT](https://aclanthology.org/2021.naacl-main.200/) evaluates downstream correctness together with evidence provenance.
- [ALCE](https://aclanthology.org/2023.emnlp-main.398/) evaluates citation completeness and correctness; its entailment-style citation metrics are useful for ContextMesh packets.
- [RAGTruth](https://aclanthology.org/2024.acl-long.585/) provides word- and response-level hallucination annotations for grounded generation.
- [Ragas](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/) provides useful implementations of context precision, context recall, and faithfulness. These are component metrics, not sufficient release criteria.
- [AgentDojo](https://arxiv.org/abs/2406.13352) supplies a realistic prompt-injection evaluation pattern that jointly scores utility and attacker success.
- [PoisonedRAG](https://www.usenix.org/conference/usenixsecurity25/presentation/zou-poisonedrag) demonstrates retrieval-corpus poisoning. Its threat construction should be adapted to malicious memories, assistant echoes, and federated results.

### What no public benchmark covers adequately

No benchmark above jointly evaluates:

- tenant noninterference across retrieval, graph traversal, source reads, feedback, caches, generated views, and rebuilds;
- actor-bound discoverability, organization hierarchy, or group changes;
- authority and applicability as separate dimensions from similarity;
- exact evidence spans through extraction, correction, compaction, and retrieval;
- valid time versus recorded time and reproducible “as of” reads;
- retroactive release-policy decisions and manual interventions;
- redaction that makes content irretrievable without erasing the audit structure;
- replayable curator decisions and deterministic projection rebuilds;
- source-bound federation with current actor credentials;
- consistent capture and injection behavior across agent harnesses;
- benefit over no memory without regressions caused by irrelevant context;
- launch and company-scale economics, freshness, fairness, and failure recovery.

Those are CMES’s reason to exist.

## Comparison systems and ablations

Every benchmark run should hold the downstream answer/action model, system prompt, tool set, retry budget, and context limit fixed. Compare:

1. **No memory** — current task input only.
2. **Full available history** — only where it fits; this is a reader/context control, not an oracle.
3. **BM25** — raw-event lexical retrieval.
4. **Dense retrieval** — raw-event embedding search.
5. **Hybrid retrieval + reranker** — a strong conventional RAG baseline.
6. **File memory** — append-only raw history plus a tool-using search agent.
7. **Representative memory products** — initially Mem0 OSS and Graphiti; add a third system only if its architecture is materially different and reproducibly deployable.
8. **ContextMesh retrieval-only** — curated assertions without graph expansion or search planning.
9. **ContextMesh contextual** — applicability, temporal constraints, and bounded graph expansion.
10. **ContextMesh full** — contextual retrieval, bounded planning, federation, and context compilation.
11. **Gold-evidence oracle** — the downstream model receives the minimal sufficient evidence. This measures remaining reader/task difficulty.

Do not put incomparable vendor-reported scores in the same ranking. If the memory system changes its internal model, reader model, context budget, or retries, report it as a separate operating point with total ingest and query cost.

## Common evaluation protocol

The benchmark corpus should remain implementation-neutral. Each system adapter implements the following logical operations:

```text
Reset(namespace)
Append(event) -> event_id
Await(watermark)
Query(principal, objective, task_context, budget) -> context_packet
Append(outcome_record_with_inputs)
Redact(source_or_span)
Snapshot()
Rebuild(snapshot)
```

The runner captures four artifacts for every case:

1. accepted source events and ingest acknowledgements;
2. committed memory state or the closest inspectable equivalent;
3. retrieved evidence/context packet plus the runner-recorded request and source IDs;
4. final answer, tool trajectory, and environment outcome.

This allows the same case to be scored at write, retrieve, compile, and act stages. Final-answer-only scoring is explicitly insufficient.

Use a language-neutral, versioned JSONL case format. A Python adapter layer is reasonable because most public suites are Python, while ContextMesh itself remains accessible through its HTTP API. [Inspect AI](https://inspect.aisi.org.uk/) is the best default orchestration layer: it cleanly separates datasets, solvers, scorers, tools, agents, and logs, and already exposes a large public eval registry. Do not encode CMES ground truth inside Inspect-specific Python objects.

## ContextMesh Eval Suite

### 1. Evidence capture and ingestion

Scenarios:

- duplicate delivery, retry storms, and repeated full-history backfills;
- late and out-of-order events with separate event time and arrival time;
- branched sessions, replies, tool results, document revisions, and code changes;
- process failure between append, curation, commit, outbox publication, and projection update;
- producer identity and tenant claims that conflict with model-suggested metadata.

Criteria:

- exactly-once logical event identity under at-least-once delivery;
- source bytes, stable IDs, revisions, lineage, principal, and timestamps preserved;
- no model output can select its tenant or increase source authority;
- acknowledged watermarks are monotonic and queryable;
- recovery produces the same committed event set without silent loss or duplication.

Primary metrics: logical duplicate rate, source-field fidelity, lost-event rate, out-of-order correctness, ingest acknowledgement latency, and time to searchable/curated state.

### 2. Curation and evidence fidelity

Scenarios:

- a source produces zero, one, or several atomic assertions;
- entailed paraphrases versus plausible but unsupported extrapolations;
- instructions, observations, decisions, hypotheses, and outcomes with identical wording;
- copied answers and paraphrased assistant echoes that must not count as corroboration;
- failed proposals, curator-version changes, and re-curation of the same history.

Criteria:

- every committed assertion is attributable to an exact source span or an explicit derived-decision record;
- atomic claim boundaries match the gold decomposition;
- intent, actor, entities, applicability, authority, versions, and validity intervals are preserved independently;
- unsupported claims are rejected, not merely assigned lower confidence;
- repeated echoes preserve lineage and do not raise independent-evidence counts;
- curator commits are immutable, versioned, and replayable.

Primary metrics: assertion precision/recall, exact-span precision/recall, entailment precision, metadata field accuracy, false-corroboration rate, and rejected-proposal reason accuracy.

### 3. Temporal state, corrections, and dependency closure

Scenarios:

- updates with different valid and recorded times;
- point-in-time queries before and after late-arriving corrections;
- equal-authority conflicts, higher-authority corrections, and still-valid independent support;
- dependency changes with replacement rules, without replacement rules, and across multiple hops;
- retroactive release-policy labels and manual interventions;
- old query receipts examined after a correction.

Criteria:

- current-state and `as_of(valid_time, recorded_time)` answers are both correct;
- old claims remain historically attributable but are not silently used as current;
- unresolved conflicts are surfaced; recency alone never resolves equal-authority disagreement;
- invalidation reaches exactly the dependent conclusions and leaves independently supported conclusions intact;
- missing replacement rules produce uncertainty rather than a guessed value;
- retroactive policy decisions affect future reads, preserve prior receipts, and identify the intervening actor and reason.

Primary metrics: temporal-state accuracy, correction uptake, stale-use rate, conflict-surfacing recall, cascade precision/recall by hop depth, unsupported-certainty rate, and receipt historical fidelity.

### 4. Entity resolution and contextual applicability

Scenarios:

- similar names belonging to different people, teams, repositories, symbols, models, and experiments;
- renamed entities, aliases, reversible merges, and later splits;
- the same procedure with different project, environment, version, actor, or team conditions;
- direct lexical prompts and indirect prompts that require recognizing an implicit subtask;
- relevant factual memories competing with less popular but action-critical procedures.

Criteria:

- embedding similarity may nominate an alias but never proves equality;
- canonical external IDs dominate name similarity when available;
- merge/split decisions preserve provenance and can be reversed;
- only claims whose hard applicability conditions are satisfied enter guidance;
- missing dependency/version data is represented as uncertainty;
- procedural knowledge is retrieved under semantic cue–trigger mismatch without increasing false activation on control tasks.

Primary metrics: entity-link precision/recall, catastrophic-merge rate, split recovery, applicability precision/recall, procedure activation recall, false activation rate, and relevant-evidence recall at fixed token budgets.

### 5. Identity, authorization, and noninterference

Use a synthetic identity provider that implements the same claims contract as production Okta. Test users, nested groups, managers, teams, disabled users, group changes, service actors, and agents acting on behalf of humans without requiring real Okta accounts.

Scenarios must probe every read path:

- direct retrieval, semantic retrieval, graph neighbors, source-body reads, corrections, feedback, federation, wiki views, exports, caches, and projection rebuilds;
- tenant-ID and actor-ID confusion, forged model-supplied identity, stale group membership, cache-key collisions, identifier enumeration, and timing/error side channels;
- an agent delegated by one person attempting to reuse another person’s packet or credentials.

Criteria:

- an unauthorized artifact is never returned to the model, not merely removed from the final response;
- tenant is always an authenticated hard boundary;
- actor and group changes take effect according to a measured, documented propagation bound;
- cache and federation credentials are scoped to tenant, actor, context, entity/version set, and projection revision;
- denial does not reveal source text or sensitive metadata.

Primary metrics: unauthorized-artifact exposure count at every trace stage, attacker success rate, legitimate-task success under defenses, stale-membership exposure window, and false-denial rate.

The fixed adversarial release corpus requires zero observed cross-tenant exposures. Always report the one-sided 95% upper confidence bound; zero failures in a finite test is not proof of zero risk. With zero failures in `n` independent probes, the approximate upper bound is `3/n`.

### 6. Redaction and retention

Scenarios:

- redact a complete source, one evidence span, and all content belonging to a principal;
- redact while curation or a query is in flight;
- redact after cache population, wiki generation, snapshot, rebuild, and feedback;
- retry and reorder the same redaction request.

Criteria:

- source text and text-derived assertions become irretrievable through every product interface, index, cache, generated view, federation cache, and rebuilt projection;
- opaque tombstones and audit topology may remain, but cannot reconstruct the content;
- redaction is idempotent and wins over delayed ingestion or rebuild events;
- pre-redaction receipts retain identifiers and historical attribution without retaining redacted text.

Primary metrics: residual retrievability count, redaction completion time, delayed-resurrection count, affected-projection coverage, and false deletion of unrelated evidence.

### 7. Process-based reads and context compilation

Scenarios:

- questions with obvious starting entities, ambiguous starting entities, and no answer;
- multi-step searches that require reformulation, graph traversal, source inspection, or federation;
- relevant evidence beyond the initial retrieval neighborhood;
- adversarial distractors that are semantically closer than the applicable evidence;
- budgets exhausted before, exactly at, and after sufficient evidence is found.

Criteria:

- the system finds minimal sufficient evidence rather than maximizing retrieved text;
- search steps remain within declared call, latency, and token budgets;
- packets separately represent applicable guidance, evidence, conflicts, version mismatches, missing dependencies, and external snippets;
- every delivered record has attributable provenance and the evaluation runner retains its exact context request/result;
- the system abstains or returns missing evidence instead of manufacturing closure.

Primary metrics: evidence-atom recall, context precision, minimal-evidence coverage, hop completion, citation precision/recall, packet-schema completeness, budget violations, query latency, and tokens returned.

### 8. Downstream procedural transfer and negative transfer

Create executable task families for coding, incident response, data analysis, API migration, and quantization. Each family contains:

- a memory-needed task whose hidden oracle depends on prior, non-inferable evidence;
- a paraphrased or indirect trigger that never names the stored procedure;
- a near-neighbor control where applying the procedure would be wrong;
- a stale-version variant;
- a conflicting-team or authority variant;
- a failed prior approach that should prevent repetition;
- a memory-independent task used to detect distraction regressions.

Criteria:

- ContextMesh improves first-attempt task success over no memory and simple retrieval on memory-needed tasks;
- irrelevant knowledge does not reduce success on memory-independent tasks;
- incompatible procedures are blocked or explicitly qualified;
- retrieved failed outcomes change exploration without becoming universal prohibitions;
- task-level success is attributable to a query receipt and the evidence actually shown to the agent.

Primary metrics: executable task success, first-action correctness, procedure-use recall, false procedure activation, stale-procedure use, repeated-failure rate, negative-transfer rate, retries, wall time, tokens, and total inference cost.

### 9. Federation and harness conformance

Run the same canonical scenarios through the HTTP/SDK path, MCP, inference-proxy path, and each supported coding harness integration.

Criteria:

- semantically equivalent visible events produce equivalent canonical captures;
- unsupported hidden reasoning is never assumed to have been captured;
- actor-bound source queries use the requesting actor’s credentials and preserve source identity/revision;
- timeouts and partial source failures are explicit in the packet;
- injection occurs at the documented hook boundary and never leaks between sessions;
- integration-version changes fail contract tests before release.

Primary metrics: normalized-event equivalence, missing/extra capture fields, packet equivalence, credential-principal correctness, source freshness, timeout behavior, and per-harness task success.

### 10. Replay, rebuild, reliability, and economics

Scenarios:

- rebuild every projection from an event snapshot and curator commit log;
- crash/restart and lease expiry at every worker transition;
- stale projection and cache invalidation during corrections;
- single-instance pilot load and multi-tenant company-scale load;
- hot tenants competing with low-volume tenants;
- degraded or unavailable embedding, model, graph, object, database, and federated-source dependencies.

Criteria:

- a frozen snapshot plus frozen curator version produces the same committed assertions and projection checksums;
- an old query receipt remains explainable after rebuild;
- failure modes are bounded, observable, retry-safe, and do not weaken identity boundaries;
- per-tenant quotas and fair scheduling prevent starvation;
- the fast path remains available when deeper curation or federation is delayed;
- storage, ingest, query, token, and inference costs are attributable by tenant and operating mode.

Primary metrics: replay divergence, projection checksum mismatch, recovery point/time, curation lag, invalidation lag, p50/p95/p99 read and write latency, throughput, backlog age, fairness, storage amplification, tokens, model calls, and cost per 1,000 events/queries.

Load profiles should be parameterized rather than hard-coded. The initial profile must cover a single user with 1–5 concurrent agents and the pilot profile must sustain dozens of requests per second. Company-scale tests should model thousands of engineers and tens of thousands of asynchronous agents, with the actual RPS and history-size assumptions versioned alongside results.

## Dataset construction criteria

CMES cases should be generated from a deterministic world model, not invented directly as prose:

1. Build a typed event/provenance graph with identities, groups, entities, dependencies, authority, applicability, valid/recorded times, revisions, releases, corrections, and redactions.
2. Compute gold active claims, proof traces, authorized visibility, expected packets, and task outcomes from that graph.
3. Render the graph into Slack-like conversations, documents, code changes, experiment logs, and tool traces. An LLM may vary language but cannot define the underlying facts, topology, permissions, or answer key.
4. Validate rendered evidence against the structured world and reject ambiguous cases.
5. Generate counterfactual siblings by changing exactly one factor: actor, tenant, time, version, authority, entity ID, correction, or dependency.

Required properties:

- fictitious names and values to minimize parametric-knowledge leakage;
- positive, negative, abstention, and adversarial controls in every family;
- lexical and semantic cue variants, including indirect task language;
- difficulty axes for history size, distractor density, hop depth, update depth, entity ambiguity, tenant count, version distance, and source latency;
- train/dev/test splits by time and entity lineage, not random question splits;
- a permanently held-out challenge set whose generator seeds and rendered cases are inaccessible to tuning;
- frozen source snapshots plus versioned models, prompts, embeddings, rerankers, tokenizers, tools, and policies;
- real pilot traces promoted only after de-identification, human labeling, and explicit authorization.

## Scoring and statistical rules

### Prefer deterministic scorers

Use exact graph state, source IDs/spans, authorization decisions, JSON schema, tool calls, code tests, environment state, and projection checksums whenever possible. Use model judges only for semantic equivalence or open-ended quality that deterministic scorers cannot resolve.

Before a model judge is trusted, calibrate it on a stratified human-labeled set, publish agreement and disagreement by category, and pin the judge version and prompt. A target of at least 0.8 agreement or Cohen’s kappa is reasonable; below that, retain human adjudication rather than laundering uncertainty into a numeric score.

### Report a vector, not a composite

The release scorecard has four independent surfaces:

- **Correctness:** capture fidelity, curation entailment, temporal/dependency correctness, retrieval, and task success.
- **Safety:** unauthorized exposure, poisoning/injection success, stale or inapplicable use, and redaction.
- **Efficiency:** latency, tokens, calls, storage, throughput, freshness, and cost.
- **Auditability:** provenance completeness, receipt coverage, replay parity, and failure explainability.

A weighted aggregate may be used for experiment ranking, but it cannot offset a safety or auditability gate.

### Experimental rigor

- Use paired cases across systems and ablations.
- Run at least three repetitions for stochastic agent tasks; preserve every trajectory.
- Report effect sizes and 95% confidence intervals, not only point estimates.
- Use paired bootstrap or randomization tests for continuous scores and McNemar-style tests for paired binary outcomes.
- Correct for multiple comparisons across eval families.
- Predeclare the primary metric, non-inferiority margin, minimum detectable effect, and exclusion rules before examining the held-out set.
- Measure both ingest and query cost. A system that moves reasoning into curation has not made it free.
- Publish the complete operating point: memory implementation, hardware, source history size, model versions, context budget, top-k, planning/tool limits, retries, and cache state.

## Release gates

### Hard invariants

A build cannot ship if any fixed conformance or adversarial case shows:

- cross-tenant retrieval, traversal, source read, feedback, cache, federation, or generated-view exposure;
- redacted text remaining retrievable or reappearing after rebuild;
- model-controlled tenant selection or authority escalation;
- silent use of a known version-incompatible instruction;
- a committed assertion without valid provenance under the case’s contract;
- a projection rebuild that diverges from the frozen event/commit log;
- a context packet whose selected records lack resolvable immutable input lineage.

### Measured quality gates

Set numeric thresholds after the first stable baseline run, then freeze them before optimization. A release must:

- show a statistically supported improvement over no memory and simple retrieval on the primary memory-needed task set;
- be non-inferior to no memory on memory-independent controls within a predeclared small margin;
- improve or preserve the accuracy–latency–cost Pareto frontier relative to the prior release;
- meet the integration-specific context, latency, freshness, and cost budgets;
- show no statistically or operationally meaningful regression in any public P0 benchmark category;
- retain acceptable calibration: confidence must fall when evidence is missing, conflicting, stale, or unauthorized.

Do not invent universal targets for task success, recall, or latency. The first benchmark cycle exists to establish those baselines. Safety and structural invariants can be exact; model-mediated quality must be measured and compared statistically.

## Execution sequence

### Phase 1 — Adapter and baseline

- Implement the common adapter and immutable result manifest.
- Add no-memory, full-history, BM25, dense, hybrid, ContextMesh retrieval-only, ContextMesh full, and gold-evidence modes.
- Run LongMemEval, MemoryAgentBench, MEME, and a small HaluMem slice.
- Establish ingest/query cost and latency accounting.

### Phase 2 — Architecture gates

- Build deterministic CMES generators for identity, temporal/dependency, evidence fidelity, redaction, and replay.
- Add 100% contract gates in pull requests and a larger adversarial corpus nightly.
- Run every public case at both evidence-packet and downstream-answer stages.

### Phase 3 — Work transfer

- Add LongMemEval-V2, MemoryArena or Mem2ActBench, and DreamBench-SWE.
- Build company-representative executable coding, incident, analysis, migration, and quantization tasks.
- Evaluate indirect triggering, negative transfer, and version compatibility.

### Phase 4 — Scale and pilot

- Add BEAM-scale histories and parameterized concurrency/load tests.
- Shadow real agent traffic with frozen retention and source allowlists.
- Sample receipts for human review; turn confirmed failures into held-out regression cases.
- Gate broader rollout on task benefit, safety invariants, bounded cost, and operational recovery.

## Minimum result artifact

Every published run should contain:

- dataset and split version;
- source snapshot hash and generator version;
- all system/model/prompt/index/policy versions;
- comparison mode and budgets;
- aggregate and per-category metrics with confidence intervals;
- ingest, query, and downstream costs separately;
- latency and context-size distributions;
- safety-event counts and confidence bounds;
- raw per-case scores, query receipts, and trace references;
- excluded cases with reasons;
- an explicit comparability flag for every cross-system result.

That artifact becomes the durable evidence for release decisions. A dashboard is a projection of it, not the record of truth.
