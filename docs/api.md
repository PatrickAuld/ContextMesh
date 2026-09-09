# API and harness integration

The data API has three operations: append records, inspect a record, and prepare context. All `/v1/` routes require `Authorization: Bearer TOKEN`. Tenant and author come from authentication. Responses use `Cache-Control: no-store`.

## Append records

`POST /v1/records` accepts an ordered batch:

```json
{
  "records": [
    {
      "id": "1d5f325a-8158-4f5b-abfe-cd47391163eb",
      "content": "Use matched cohorts when comparing exposure.",
      "scope": {
        "conversation": "investigation-1",
        "project": "discovery",
        "visibility": "internal",
        "groups": []
      },
      "inputs": [],
      "supports": [],
      "supersedes": [],
      "metadata": {"role": "user", "channel": "message", "source_id": "turn-1"}
    }
  ]
}
```

The response contains `records: [{id, duplicate}]`. The whole batch commits atomically. Retrying an identical record ID/payload for the same person succeeds; changing the payload under that ID conflicts. Use a new ID for corrections. Inputs can reference existing authorized records or earlier entries in the batch; forward references and cycles are rejected.

`scope`, `inputs`, `supports`, `supersedes`, and `metadata` are optional. Default scope is personal. Visibility is `personal`, `internal`, or `restricted`; restricted visibility uses identity-provider group names. Conversation/project fields are retrieval labels, never permission grants. Metadata is an untrusted object preserving source roles, tool IDs, and other harness context.

A derived record's `inputs` lists all supplied source records. `supports` contains `{record_id, quote}` entries, each referring to an input and an exact substring of its content. `supersedes` is a subset of inputs and requires authority over the target record. This distinguishes an explicit correction from a conclusion that merely depends on older evidence. Model and extraction metadata are attached by the curator; clients cannot claim server-generated derivation metadata.

The server accepts up to 256 records per batch, bounded record payloads, and a 2 MiB HTTP request body. The transcript adapter uses smaller batches. Bounds are enforced before mutation. Split long transcripts into ordered batches rather than increasing message sizes indefinitely.

## Read a record

`GET /v1/records/{id}` returns the immutable record plus authenticated `author`, optional `agent_id`, `recorded_at`, `sequence`, and optional `derivation`. Inputs and supporting references retain lineage back to original content. Follow input IDs through the same endpoint to inspect ancestry.

Visibility covers the entire input ancestry. An unreadable, missing, or redacted record is unavailable. Historical superseded records remain inspectable when authorized. A bounded traversal cannot establish access by inspecting only part of the lineage.

## Prepare task context

`POST /v1/context`:

```json
{
  "task": "Continue the exposure regression investigation",
  "scopes": [{"project": "discovery"}],
  "starting_records": [],
  "max_tokens": 2048
}
```

The result contains:

| Field | Meaning |
|---|---|
| `context_text` | Sourced text to inject into the next model request |
| `records` | The selected immutable records and provenance |
| `guidance` | Any selected, explicitly approved release outputs |
| `watermark` | Record-log watermark observed for this request |
| `truncated` | A retrieval or output bound limited the result |

`scopes` filters conversation/project labels. `starting_records` supplies explicit known entry points. Optional `purpose` selects eligible approved release policies. The request does not create a persistent context session or receipt.

The current budget is a conservative UTF-8 byte cap on `context_text`, named `max_tokens` for harness configuration. It is not a model-specific tokenizer measurement; response metadata and the separate structured `records` are not covered. Inject `context_text` once, not both textual and structured copies. Bounds and provenance permit later tokenizer-specific budgeting without changing record history.

A `409 context_changed_retry` means a relevant security/history change occurred during preparation; issue a fresh context request. An empty result means no eligible relevant record was selected within the bounds, not that no knowledge exists.

## Deterministic harness hooks

1. Authenticate the person and create a short-lived delegated agent token.
2. At start/resume, request context with the current task and project/conversation filters.
3. Capture original messages, tool results, and exposed reasoning or explicit notes with stable IDs. Preserve references to injected memory used by derived notes.
4. Persist batches in the local outbox before transmission. Retry in order.
5. Capture corrections and outcomes as new records; background curation maintains notes.

No model tool selection is required for capture. There is no assumption that a provider exposes hidden reasoning. Opaque provider continuation state is not searchable record content. Readable reasoning summaries and explicit scratchpad notes can be captured with their correct source/channel labels.

See the [JSONL transcript adapter](../examples/integrations/README.md) for executable capture and context commands. An existing harness invokes those commands at its message and start/resume boundaries. This repository does not install native lifecycle hooks into every third-party CLI automatically.

## Optional MCP

`contextmesh mcp` exposes `memory_append` and `memory_context` over stdio for explicit model-selected operations. The same HTTP contracts apply. Set `CONTEXTMESH_URL` and a delegated `CONTEXTMESH_TOKEN` in the host environment. MCP availability alone does not guarantee automatic capture or context injection.

## Administrative controls

| Endpoint | Purpose |
|---|---|
| `GET /v1/identity` | Resolved caller identity |
| `POST /v1/agents` | Create delegated credential |
| `POST /v1/agents/{id}/revoke` | Revoke credential |
| `POST /v1/principals/status` | Suspend/reactivate a principal |
| `POST /v1/records/{id}/classification` | Change access metadata using `{visibility, groups}` |
| `POST /v1/records/{id}/redact` | Irreversibly make a record and its derivatives unavailable |
| `GET /v1/redactions` | Content-free deletion ledger |
| `GET /v1/audit` | Content-free operational history |
| `GET /v1/status`, `/v1/jobs`, `/v1/metrics` | Curation health and operational queue state |
| `GET /v1/policies`, `POST /v1/policies` | Inspect or approve bounded disclosure rules |
| `POST /v1/policies/{id}/revoke` | Revoke a disclosure rule |

Administrative actions require a human administrator unless the endpoint explicitly permits the owning person. A delegated agent cannot acquire administrator privileges. See [operations](operations.md) for release and recovery behavior. Legacy events, query/wiki, graph lifecycle, and receipt endpoints have been removed.
