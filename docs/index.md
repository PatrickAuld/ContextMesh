# Documentation

Build shared memory into the agents you already use. ContextMesh captures source evidence, maintains contextual knowledge, and returns sourced guidance for the next task.

Start with one Rust process and PostgreSQL. Add API replicas and inference workers as your team grows.

## Start here

- [Quickstart](../README.md#start-locally) — launch the local stack, capture an event, and retrieve context.
- [Architecture](architecture.md) — understand evidence, incremental graph versions, worker leases, and retrieval.
- [API and integrations](api.md) — connect a harness through HTTP, the Rust client, or MCP.
- [Operations](operations.md) — configure Okta and inference, rebuild graphs, trace provenance, and redact sources.
- [System validation](testing.md) — run black-box tests against real processes and PostgreSQL.
- [Evaluation strategy](evaluation.md) — compare memory systems and define quality, safety, efficiency, and auditability gates.

## The working loop

**Capture evidence.** Send visible messages, tool results, decisions, and outcomes with stable source IDs and context. Corrections use a new revision; agents do not need to maintain a separate wiki.

**Maintain knowledge.** Workers derive claims and relationships asynchronously. Every claim retains a source quote and the conditions under which it applies. Multiple graph versions can coexist and receive incremental updates.

**Retrieve before work.** Query with the current task, entities, and project context. Inject the returned packet as sourced data, preserving citations, conflict indicators, and revalidation flags.

**Inspect and improve.** Follow lineage back to source evidence. Rebuild with a different model or configuration while the current graph serves traffic, then promote the new graph when it is ready.

## Choose your integration

| Interface | Use it for |
|---|---|
| [HTTP API](api.md#contract-map) | Existing harnesses, bots, and services in any language. |
| [Rust client](api.md#rust-client-and-durable-capture) | Typed integration and durable local capture with an outbox. |
| [MCP](api.md#mcp) | Tool access from a compatible agent host. |
| [Operational CLI](operations.md#inspect-and-repair) | Owner inspection, graph management, and incident response. |

Automatic use depends on the harness: capture meaningful evidence after work and retrieve context before relevant prompts or tool actions. ContextMesh is the memory service your integrations call.

## Identity and disclosure

People authenticate through Okta; agents receive short-lived delegated identities. Local development identities let you test without an Okta account. Tenant isolation and group-based restricted knowledge are enforced; finer-grained ACLs remain an extension boundary.

[Approved release policies](architecture.md#approved-release-policies) let restricted evidence inform a finite set of owner-approved guidance outputs. The querying agent does not receive the underlying restricted evidence. Retroactive classification, correction, and redaction invalidate affected policies.

## Before a team rollout

Read the [operator runbook](operations.md) for production identity, gateway configuration, database roles, and recovery. The local Compose stack is a development environment. Real-model quality and company-wide throughput need evaluation against your own workloads; see the [evaluation strategy](evaluation.md) and [validation boundaries](testing.md).
