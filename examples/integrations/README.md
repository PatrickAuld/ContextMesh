# Harness transcript integration

The harness can stream a complete JSONL transcript to ContextMesh. Each line is one message:

```json
{"id":"turn-1","revision":1,"role":"user","channel":"chat","content":"What is our deploy process?"}
{"id":"turn-2","revision":1,"role":"assistant","channel":"chat","content":"It runs from the release pipeline."}
```

Capture is automatic and sends `/v1/records` batches of at most 64 records. Each line requires a stable source `id` and positive `revision`; the conversation identity is required with `--conversation`. Role, channel, and tool call IDs are retained as untrusted metadata.

```sh
contextmesh capture --url http://127.0.0.1:8787 --token "$CONTEXTMESH_TOKEN" \
  --outbox .contextmesh-outbox --conversation conv-42 transcript.jsonl
contextmesh flush --url http://127.0.0.1:8787 --token "$CONTEXTMESH_TOKEN" \
  --outbox .contextmesh-outbox
```

The file argument may be `-` for a pipe: `cat transcript.jsonl | contextmesh capture --url http://127.0.0.1:8787 --token "$CONTEXTMESH_TOKEN" --conversation conv-42 -`.

At harness start or resume, request authorized context explicitly:

```sh
contextmesh context --url http://127.0.0.1:8787 --token "$CONTEXTMESH_TOKEN" \
  --outbox .contextmesh-outbox "Continue the deployment task" --purpose resume
```

The capture command prints only append/queue status. It does not print transcript text. MCP is available separately for clients that choose `memory_append` or `memory_context`; ordinary harnesses should use these CLI endpoints so every message is captured without a model selecting a tool.
