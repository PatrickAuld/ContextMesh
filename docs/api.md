# API and harness integration

All `/v1/` routes require `Authorization: Bearer TOKEN`. Tenant, person, groups, and delegated agent identity come from authentication. Request bodies cannot select a tenant or person. Successful JSON responses are HTTP 200. Input validation errors use 400/422, missing or unreadable objects 404, conflicts 409, capacity limits 429. Data responses use `Cache-Control: no-store`.

## Contract map

| Method / path | Input | Result / access |
|---|---|---|
| GET `/health` | none | Liveness; unauthenticated |
| GET `/ready` | none | Database readiness; unauthenticated |
| GET `/v1/identity` | none | Resolved identity |
| POST `/v1/events` | `source`, `external_id`, positive `revision`, `text`; optional `context`, `classification`, `read_groups` | Durable event ID and duplicate flag |
| GET `/v1/events` | Query parameters `search`, `after` | Owner source search, 100 rows/page |
| GET `/v1/events/{id}` | none | Current readable source; owners may inspect old revisions |
| POST `/v1/events/{id}/classification` | `classification`, optional `read_groups` | Owner reclassification of all source revisions; dependent policies disabled |
| POST `/v1/events/{id}/redact` | `{}` | Owner redaction of all source revisions and derived content |
| POST `/v1/query` | `query`; optional `context`, `entities`, `graph_id`, `purpose`, `agentic`, `max_chars` | Structured context packet and receipt |
| POST `/v1/wiki` | Same as query | Markdown view plus underlying packet |
| GET `/v1/receipts/{id}` | none | Owner or originating person; references filtered by current visibility |
| GET `/v1/graphs` | none | Graph versions, active flag, state, queue counts; configuration only for owners |
| POST `/v1/graphs` | `name`, `config` | Owner rebuild from current evidence |
| POST `/v1/graphs/{id}/promote` | `{}` | Owner atomic active-graph switch once caught up |
| POST `/v1/graphs/{id}/archive` | `{}` | Owner stops maintenance; active graph cannot be archived |
| POST `/v1/graphs/{id}/retry` | `{}` | Owner retries failed jobs |
| GET / POST `/v1/policies` | Policy below | Owner inspect/approve release rules |
| POST `/v1/policies/{id}/revoke` | `{}` | Owner disables a release rule |
| POST `/v1/agents` | `name`, optional `ttl_seconds` (60–86400; default 3600) | Person creates delegated bearer token; returned once |
| POST `/v1/agents/{id}/revoke` | `{}` | Owning person or owner revokes token |
| POST `/v1/principals/status` | `subject`, `disabled` | Owner suspends/reactivates a known identity |
| GET `/v1/audit` | Query parameters `after`, optional `target` UUID | Owner audit entries, 200 rows/page |
| GET `/v1/lineage/{id}` | Claim or event ID | Owner source, quote, actor, graph configuration, job provenance |
| GET `/v1/status` | none | Owner tenant queue counts and gateway configuration status |
| GET `/v1/jobs` | none | Owner pending/running/failed job summaries, up to 200 |
| GET `/v1/metrics` | none | Owner Prometheus text metrics for this tenant |
| GET `/v1/redactions` | `after` sequence | Owner content-free redaction ledger, 200 rows/page |

## Source and query semantics

`context` is a JSON object supplied by the integration. Stable team, project, and entity identifiers improve retrieval. `entities` is a list of canonical identifiers such as `type:Exposure`; source names and workspace paths alone cannot establish those semantics. Supply explicit query entities separately from context when possible.

`classification` defaults to `internal`, readable inside the tenant. `restricted` sources require a matching `read_groups` membership or owner access. An empty restricted group list is owner-only. Only owners may retroactively change classification. A non-owner correction cannot downgrade an existing restricted source.

Curation validates exact quote provenance and claim structure. Applicability is an explicit JSON containment predicate over query context; dependencies compare exact strings under `context.versions`. Missing or changed versions return `use: revalidate`. Multiple selected claims in a declared `slot` are surfaced as a potential conflict; there is no general-purpose semantic contradiction proof.

Source text is limited to 64 KiB, context to 16 KiB, and HTTP bodies to 128 KiB. Query text is limited to 8 KiB; `max_chars` ranges from 1,000 to 64,000. Search is bounded to three rounds and 60 candidates per round. An empty result means no eligible memory was selected, not proof that no relevant knowledge exists.

## Graph configuration

```json
{
  "name": "curator-v2",
  "config": {
    "mode": "llm",
    "model": "company-curator-model",
    "temperature": 0,
    "instructions": "Prefer procedures with explicit applicability and exact dependency versions.",
    "extractor_version": "claims-v1"
  }
}
```

`mode` is `literal` or `llm`. The gateway must be configured for `llm`. If model is omitted, the service resolves and records the configured default when the graph is created. The versioned extractor validates at most 32 claims per source. `literal` mode stores an observation and source-supplied entity identifiers without semantic inference.

## Release policy

```json
{
  "name": "approved-capacity-guidance",
  "purpose": "capacity-planning",
  "audiences": ["engineering"],
  "instruction": "Select conservative when the evidence calls for preserving the existing budget envelope; otherwise abstain.",
  "outputs": {
    "conservative": "Plan within the current approved capacity envelope."
  },
  "evidence": ["REPLACE_WITH_SOURCE_EVENT_UUID"]
}
```

Policy creation is approval. `audiences: ["*"]` explicitly permits all people within the tenant. Use exact evidence UUIDs and up to 16 approved outputs. Calling `/v1/query` with the matching `purpose` evaluates up to four matching policies. Unknown model keys, failures, and abstentions release no guidance. Reclassification, correction, or redaction invalidates policies naming the changed evidence; create a new policy after review.

## Harness pattern

1. Authenticate the person with an Okta access token. Create a delegated token for each agent/session; keep its TTL short.
2. Capture visible messages, tool results, and measured outcomes as source events with stable IDs and revisions. Do not capture provider-hidden reasoning or blindly recapture ContextMesh-injected text.
3. Before relevant prompts or tool actions, query with the current goal and stable semantic identifiers. Insert returned context as sourced data, retaining citations and revalidation/conflict flags.
4. Capture meaningful outcomes and corrections. Let the service maintain every non-archived graph asynchronously.
5. Treat 409 `context_changed_retry` as a fresh lookup request; a policy, source, identity, or active graph changed during retrieval.

MCP exposes tools but does not guarantee that a host calls them automatically. Harness hooks or the host's own integration policy determine when capture and retrieval happen. No bot or Slack adapter is included.

## MCP

Run the stdio adapter with a delegated token in its environment:

```sh
export CONTEXTMESH_URL=https://contextmesh.company.example
export CONTEXTMESH_TOKEN=your-delegated-token
contextmesh mcp
```

The adapter implements JSON-RPC initialization, tool discovery, `memory_insert`, and `memory_extract`. Configure your host to invoke the binary and provide environment variables using that host's syntax. Protocol version: `2025-03-26`. The adapter reports service failures to the host; MCP capture itself does not use the durable outbox.

## Rust client and durable capture

```rust
use contextmesh::{client::Client, events::Insert};
use serde_json::json;
use std::path::Path;

async fn step() -> anyhow::Result<()> {
    let memory = Client::new(
        "http://127.0.0.1:8787",
        &std::env::var("CONTEXTMESH_TOKEN")?,
    )?.with_outbox(Path::new("./memory-outbox"))?;
    memory.remember(&Insert {
        source: "harness".into(),
        external_id: "message-1".into(),
        revision: 1,
        text: "Compare exposure within matched cohorts.".into(),
        context: json!({"entities": ["type:Exposure"]}),
        classification: "internal".into(),
        read_groups: vec![],
    }).await?;
    let packet = memory.extract(json!({
        "query": "Build the exposure investigator",
        "entities": ["type:Exposure"]
    })).await?;
    println!("{}", packet["context_text"]);
    Ok(())
}
```

An outbox binds its directory to the endpoint and credential hash. Pending source payloads are private local files, atomically persisted before sending. Transient failures remain pending; explicit flush retries them. Permanent errors are moved to `.rejected` for operator repair. Server idempotency tolerates duplicate transmission. Rotating the credential creates a different outbox identity; drain or deliberately migrate the old outbox first.

```sh
contextmesh capture --outbox ./memory-outbox event.json
contextmesh flush --outbox ./memory-outbox
```

Inspect `sent`, `pending`, and `rejected` in the result. Local outbox retention belongs to the harness owner; server redaction cannot erase copies already held by a client.
