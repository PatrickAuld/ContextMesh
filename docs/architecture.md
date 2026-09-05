# Architecture

## Deployment shape

ContextMesh is one Rust package with narrow modules and three runtime modes. `run` combines API and workers for initial adoption. `serve` and `worker` support separate process pools against the same PostgreSQL database. No Kafka, Redis, dedicated graph database, or workflow orchestrator is required.

PostgreSQL owns source evidence, source revisions, graph metadata, projected claims and edges, leased jobs, identity bindings, release policies, receipts, and audit records. Transactions tie acceptance to work scheduling and tie derived claims to job completion. Full-text and entity-array indexes provide candidate retrieval; explicit relations retain source lineage.

This is horizontal application and worker scaling, not a distributed PostgreSQL writer. The initial target is dozens of people and dozens of requests per second. Larger deployments should measure worker throughput, connection demand, query latency, and tenant lock contention. The current code has not been load-qualified for tens of thousands of simultaneous requests. Thousands of mostly idle/asynchronous agents are distinct from that request rate.

## Evidence and projections

An event is an attributed source assertion, not an assertion that its content is true. Its identity is `(tenant, source, external_id, revision)`. Same-identity retries must have the same payload. New revisions are monotonic and preserve previous source content. Only the current, non-redacted revision participates in retrieval and rebuilds.

Claims contain a source quote, intent, stable entities, applicability predicates, version dependencies, and an optional conflict slot. Explicit edges reference a claim and therefore an event. Associations can be regenerated without changing source history. Workers never decide tenant identity or disclosure labels.

Graph configuration records extraction mode, resolved model, temperature, instructions, and extractor version. Configurations are immutable. Create another graph to change them. Runtime gateway credentials and addresses stay outside graph metadata; operators must preserve gateway routing/model-version identity when reproducibility matters. A gateway alias is not an immutable model binary.

Creating a graph records an input sequence watermark under the same tenant lock used for ingestion. Workers backfill up to 500 inputs per transaction. Events accepted after graph creation are enqueued directly for every maintained graph. Uniqueness on `(tenant, graph, event)` removes overlap. A graph becomes ready after backfill and derivation finish. Promotion requires no pending, running, or failed jobs and atomically switches the tenant's active graph. Archived graphs remain inspectable, stop receiving work, and cannot be promoted without rebuilding.

Rebuilds run extraction again over eligible evidence, possibly with different models or instructions. Existing graph decisions remain inspectable. This release does not contain a separate deterministic decision-replay importer or an A/B testing product.

## Durable work

Workers claim jobs with PostgreSQL `FOR UPDATE SKIP LOCKED`. Each attempt receives a unique lease token and a 60-second lease. Inference requests have a 40-second deadline and occur outside transactions. Completion checks the lease token, deadline, source currentness/redaction, and graph state before committing claims and marking the job complete together.

Crashes leave leases available for reclamation. Five failed attempts move a job to `failed`; retry delays grow exponentially and owner actions can retry a graph's failed jobs. Expired final attempts become failed rather than remaining stuck forever. Poisoned inputs do not block other jobs. Graph promotion rejects incomplete work.

Worker processes rotate configured tenants between jobs. This provides basic fairness, not hard per-tenant resource reservations. API query concurrency is capped at 32 per process; ingestion rejects a tenant backlog of 100,000 pending/running jobs. At most eight graph versions are maintained simultaneously. Scale workers when curation backlog grows, and account for work amplification from multiple maintained graphs.

## Retrieval

1. Authenticate the person or delegated agent; determine tenant and current groups.
2. Select an explicit graph or the tenant's active graph and record the security epoch.
3. Retrieve up to 60 authorized full-text/entity candidates per search. Enforce source currentness, redaction, group visibility, and applicability before providing evidence to a planner.
4. Expand entity associations. With `agentic: true`, a configured model may reformulate the query or select entity identifiers for up to two additional searches.
5. Mark changed or unknown exact-version dependencies `revalidate`. Surface multiple claims in declared conflict slots rather than silently choosing one.
6. Optionally evaluate matching approved release policies in a separate privileged inference call.
7. Bound selected memory and guidance content, recheck the security epoch, and commit a content-free query receipt.

The planner only sees evidence the requesting identity may read. A release evaluator sees only the evidence explicitly named by an approved policy and cannot pass its free-form response through. The final packet separates directly readable memories from approved guidance.

Retrieval uses PostgreSQL full-text indexes and explicit entity association. This release does not include an embedding service, ANN index, or arbitrary external-index federation. The retrieval and inference modules are the extension boundaries for those capabilities. The `max_chars` budget covers serialized selected memories and approved guidance; envelope, trace, and duplicated `context_text` are additional response bytes. It is not a tokenizer limit.

## Identity and future authorization

A tenant maps to one configured OIDC issuer; tokens must match its configured audience and RS256 JWKS. Okta custom authorization-server access tokens carry the user's `sub` and groups. Identity-provider groups are used as supplied; ContextMesh does not invent management hierarchy or maintain a competing organization chart. Distinct organizations should use distinct issuer configurations in this version.

Agents receive short-lived opaque credentials bound to a tenant and person. Each request resolves the person's stored groups and enabled status. Delegated agents cannot delegate again or perform owner operations, even when the person belongs to the owner group. Refreshing the person's OIDC session updates their groups; local suspension is immediate. Okta suspension/group changes are not discovered until another token is processed or the operator synchronizes status. There is no SCIM/event-hook synchronization in this release.

Every tenant-data transaction sets `app.tenant_id` locally. Tables force row-level security and SQL also scopes tenant IDs. Transaction-local state cannot leak between pooled connections. Owner migration credentials are separate from runtime credentials. The runtime must not be a superuser or possess BYPASSRLS. See [PostgreSQL row security](https://www.postgresql.org/docs/current/ddl-rowsecurity.html).

Future ACL work can replace `Identity::can_read` and the matching SQL predicates with action/resource policies. Preserve those boundaries on search candidates, graph traversal, sources, receipts, planner inputs, releases, and audit. Raw access, permission to compute over evidence, and permission to disclose derived outputs are separate capabilities.

## Approved release policies

An owner approves a policy containing exact evidence IDs, an intended purpose, audience groups, a selection instruction, and a finite map of allowed output keys to allowed output text. A dedicated inference call may select a key or abstain. Only an exact approved text is returned; unknown keys, provider errors, and malformed output abstain. Source IDs, policy internals, and restricted reasoning are not included in the caller's packet.

This deliberately supports bounded guidance such as an approved capacity envelope. It does not claim arbitrary generated prose can safely hide sensitive inputs. Selection among outputs itself conveys information; approving the policy explicitly authorizes that disclosure. Owners must consider repeated queries and combinations of outputs. Different authorized agents can be introduced behind this policy boundary later without granting raw access to the calling agent.

Corrections, retroactive classification, and redaction disable affected policies. Reapproval creates a new policy with current evidence. A policy revocation changes the security epoch immediately.

## Redaction and audit

Redaction applies to all revisions of a source, across every graph. The transaction clears raw body/context/hash, deletes derived claims and edges, cancels queued/in-flight jobs, clears dependent release payloads, disables those policies, and increments the tenant security epoch. A tombstone prevents reusing that source identity. Rebuilds and workers check current redaction state; receipts resolve only currently visible surviving claim IDs.

Query completion compares the epoch sampled before retrieval with the epoch under the final tenant lock. A changed epoch returns `409 context_changed_retry`. Queries starting after committed redaction cannot retrieve the removed payload. Data already delivered to clients or an inference gateway cannot be recalled; operators own those systems' retention. An HTTP response committed before redaction may still be in transit. This is an application retrievability guarantee, not physical-media erasure or a universal network-delivery guarantee.

The audit log records actor, delegated agent, action, target IDs, timestamps, and content-free operational metadata. It rejects update/delete, and the runtime is not granted those privileges. Owner lineage lookup connects a claim to its exact quote, source revision, graph configuration, and job. Redacted payloads are intentionally unavailable even to owners.

Backups may retain historical bytes. Restore procedures must preserve and reapply later redactions before opening service access; see the operator runbook. An independently restored database from before a redaction cannot infer that a later deletion happened.
