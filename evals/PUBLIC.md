# Public benchmark evaluation

`evals/public.py` is a small, deterministic, retrieval-stage harness for two open public memory datasets:

- **LongMemEval**: the official JSON files use `question_id`, `question_type`, `question`, `answer`, `question_date`, `haystack_session_ids`, `haystack_dates`, `haystack_sessions`, and `answer_session_ids`. The upstream schema and download instructions are in the [official LongMemEval README](https://github.com/xiaowu0162/LongMemEval#dataset-format); the cleaned data package is published on [Hugging Face](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned). The upstream code repository is MIT licensed. Verify the terms that accompany any downloaded data before redistributing it.
- **LoCoMo**: the official JSON uses samples with `conversation` (`session_N` arrays of turns containing `dia_id` and `text`) and `qa` items containing `question`, `answer`, `evidence`, and `category`. See the [official LoCoMo repository](https://github.com/snap-research/locomo) and [ACL paper](https://aclanthology.org/2024.acl-long.747/). The upstream repository does not publish a `LICENSE` file; check the data terms before using or redistributing it.

The runner accepts a local JSON path. It never downloads a large corpus or starts an inference job automatically. LongMemEval's annotated evidence is mapped to session IDs, and LoCoMo's `D<N>:<turn>` annotations are mapped to their containing session. Cases without an evidence annotation report `source_recall: null` (not zero).

## What is measured

The default modes are:

- `no-memory`: an empty retrieval packet;
- `full-history`: all history only when it fits; otherwise explicitly unavailable;
- `bm25`: a dependency-free lexical baseline with deterministic tie breaking;
- `contextmesh`: real HTTP ingestion through `/v1/events` followed by a real `/v1/query` call.

Every mode receives the same output ceiling and uses the same chunks, each at most
3,500 characters and 64 KiB. This prevents the literal curator's 4,000-character
limit from silently losing evidence. ContextMesh additionally charges packet and
quote metadata against its internal budget, so the manifest explicitly marks this
as **not a strict equal-effective-budget comparison**. Do not rank these operating
points as if identical quantities of source context were available.

Managed mode provisions a fresh tenant and literal graph for each case, waits for
durable curation to finish, and records acknowledgements, graphs, packets, and
source mappings. No provider key is needed. Direct HTTP mode requires a fresh
evaluation tenant, an owner token, and one case per invocation. An external-ID
prefix does **not** isolate queries within a shared tenant.

The score is retrieval-stage **annotated session recall**, plus context size,
combined run latency, ingest count, unavailability, and errors. Retrieving any
chunk from an annotated session counts as a source hit; it does not prove that
the supporting turn was retrieved. It does not run a reader, answer model, LLM
judge, or final-answer scorer. Every per-case artifact includes
`answer_scored: false` and `final_answer: null`; these results must not be
described as LongMemEval or LoCoMo answer accuracy. Runtime failures remain
visible and fail CI; unavailable full-history controls do not.

## Running

```sh
# Cheap local baselines; data is supplied by the operator.
python3 evals/public.py path/to/longmemeval_s.json \
  --output artifacts/longmemeval-s \
  --sample-size 32 --seed 17 --max-context-chars 16000

# LoCoMo uses the same importer; select a fixed sample reproducibly.
python3 evals/public.py path/to/locomo10.json \
  --benchmark locomo --output artifacts/locomo \
  --sample-size 10 --seed 17

# Direct HTTP mode: use a fresh evaluation tenant and one case.
python3 evals/public.py path/to/longmemeval_s.json \
  --output artifacts/cm-longmemeval --sample-size 1 \
  --mode no-memory --mode full-history --mode bm25 --mode contextmesh \
  --contextmesh-url http://127.0.0.1:8787 \
  --contextmesh-token "$CONTEXTMESH_TOKEN" \
  --namespace "public-$(date +%s)"

# In CI, start a fresh real ContextMesh service and tenant for each case.  The
# service harness reads DATABASE_URL, MIGRATION_DATABASE_URL, and optionally
# CONTEXTMESH_BINARY from the environment; this is the safest mode because a
# shared graph cannot isolate literal claims by case_id.
python3 evals/public.py --dataset path/to/locomo10.json \
  --output artifacts/cm-locomo --max-cases 8 --managed-service \
  --mode no-memory --mode bm25 --mode contextmesh
```

Use `--allow-truncated` only when the corpus owner explicitly documents that a partial history is intentional. Otherwise malformed, unsupported, missing, or marked-truncated history is rejected. LoCoMo category 5 rows use `adversarial_answer`, so they are excluded with a reason in `manifest.json`; invalid evidence IDs are also rejected and recorded there. Selection is made after a stable `(benchmark, case_id)` sort and uses the recorded seed. `manifest.json` contains the exact input SHA-256, adapter/schema versions, case-ID hash, official source links and license notes, modes, exclusions, and equal budgets. `cases.jsonl` contains one retrieval-stage record per case and mode; `aggregate.json` contains per-mode summaries.

The public adapter does not exercise redaction or final-answer execution. Use the
paired first-party evaluation for redaction and the existing system suite for
broader operational checks. Managed mode provides case isolation through fresh
tenants; use it for multi-case runs. An empty packet is recorded as an empty
retrieval result, never as proof that the answer is absent. LoCoMo runs the text
track only; image inputs are not ingested. These small seeded samples are smoke
comparisons, not held-out quality estimates or published benchmark scores.

## Verification

```sh
python3 -m unittest evals.test_public -v
python3 -m py_compile evals/public.py
```

The tests exercise both import schemas, truncation rejection, stable seeded sampling, UTF-8 chunking, all local baselines, the actual HTTP request path with a local HTTP server, and artifact fields. They do not claim that a live ContextMesh service or a downstream answer model ran.
