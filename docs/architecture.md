# Architecture

ContextMesh has three pieces: an immutable record log, a curator that appends derived records, and a context builder that selects records for a task. PostgreSQL stores the log and operational coordination. There is no separate graph lifecycle, claim store, session state machine, or persisted context receipt.

## One record model

Conversation messages, working notes, and consolidated knowledge use the same record shape. A record has an immutable ID, authenticated author and delegated agent attribution, recording time, content, scope, inputs, supporting references, supersession references, and bounded harness metadata. Generated records additionally identify their model and extraction configuration.

A conversation is a grouping key, not a managed lifecycle. Project and conversation labels help retrieval; they never grant access. Original messages normally have no inputs. A generated note refers to every record supplied to its inference call, even if only some records support its conclusions. Citations identify exact substrings in those inputs. Quote matching establishes provenance, not truth.

Appending a correction or revised note leaves earlier records intact. Supersession requires authority over the target and is an explicit relationship; later timestamps never overwrite another person's conclusion. Competing successors can coexist. Default context excludes superseded records and stale derivations, but authorized callers can inspect historical records by ID. A replacement remains usable even though it explicitly includes the record it supersedes in its ancestry.

## Lineage and access

The input manifest is attached by the service, not chosen by the model. Model-generated supports must reference inputs and contain exact source quotes. References always target immutable record IDs. Derived records retain transitive ancestry through relational dependency links; summarizing a summary never creates independent corroboration.

Authorization checks the record and all its transitive inputs. A derived record's requested audience cannot override an input's restrictions. Scope filters select information within the caller's permissions. Bounded traversal fails closed rather than returning partially checked lineage. The same canonical availability rules govern retrieval and derivation.

Personal is the default audience. Explicit internal records are readable within the tenant. Restricted records require authorized group membership. Administrative access and delegated identities remain separate: agents inherit the acting person's current stored groups and do not gain administrative privileges.

All tenant-owned queries include an explicit tenant predicate and run in a transaction with forced PostgreSQL row-level security. Runtime roles cannot be superusers or bypass RLS. Tenant identity and authorship come from authentication, not request bodies.

## Capture and idempotency

The public append API accepts an ordered batch. All records commit together or none do. References may point to existing authorized records or earlier records in the same batch. A stable ID with the same canonical payload and person is an idempotent retry; different content conflicts. Delegated-token rotation does not change the logical person who authored the record. A redacted ID cannot be reused.

The harness persists unsent batches in its local outbox and retries using the same IDs. There is no server-side transcript synchronization protocol. Messages, tool results, explicit notes, reasoning summaries, and compaction summaries enter through the same API. Integration metadata preserves role/channel distinctions and original message identities. Previously injected memory must retain its input references rather than being recaptured as independent evidence.

## Curation

When an inference gateway is configured, source appends schedule durable curation jobs. A worker assembles chronological conversation material and relevant existing notes within explicit bounds, calls the gateway outside a database transaction, then appends derived records and completes the job atomically. Every output retains the actual input manifest and extraction configuration. Coverage metadata makes bounded processing visible; a partial window is not represented as a complete transcript.

The same operation supports extraction and consolidation: read records, produce a useful note, append it. There is no promotion pipeline between record classes. Derived outputs do not recursively schedule themselves. Without a gateway, capture and context retrieval operate over original records; the service does not pretend literal copies are model extraction.

Jobs use leases, retries, and content-free failure codes. Before publishing inference output the worker rechecks its lease and source/security validity. Queue state is operational coordination, not the semantic state of knowledge. Restarting workers does not mutate record history.

## Context preparation

`POST /v1/context` accepts a task, optional scope filters and starting records, and a budget. It selects authorized usable records, follows relevant lineage within bounds, and returns sourced context plus the selected immutable records. No durable session or receipt is created. The response exposes a watermark and truncation indication; absence of a result is not proof that no relevant knowledge exists.

Context is supplied as data to the harness, never as higher-priority instructions. Generated text and captured prompts remain untrusted. The context builder checks the tenant security epoch again before returning so concurrent redaction, reclassification, or revocation cannot silently serve an obsolete authorization snapshot.

Approved release policies remain a separate administrative boundary. A privileged evaluator may inspect exactly the approved evidence and select one of a finite set of approved output strings, or abstain. Free-form restricted output never reaches the caller. Correcting, reclassifying, or redacting supporting evidence invalidates dependent approvals.

## Privacy exceptions

Immutability has two explicit exceptions: access reclassification and redaction. Reclassification changes only access metadata. Redaction clears payloads and invalidates descendants through the complete dependency graph; stale workers and replay cannot restore them. Content-free identifiers remain as tombstones and audit evidence.

The guarantee covers service retrievability, including rebuilds and derived content. Data already delivered to clients or inference providers cannot be recalled. Backup restoration must reapply later deletion records before reopening access. See [operations](operations.md).

## Deployment

One Rust binary can run HTTP and workers together, or as separate process pools against PostgreSQL. No Kafka, Redis, dedicated graph database, or workflow orchestrator is required. Search and lineage structures are derived from the record log. There is no claim that model re-execution reproduces identical output; retain the original generated record and its derivation metadata for audit.

Initial capacity targets remain dozens of people and dozens of requests per second. Worker throughput, lineage depth, and query candidate limits need workload measurements before company-wide expansion. Thousands of asynchronous agents do not imply thousands of simultaneous inference requests.
