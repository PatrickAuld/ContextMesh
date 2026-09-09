# ContextMesh documentation

ContextMesh captures conversations from agent harnesses, maintains useful notes, and prepares shared context for future work. One immutable record model connects original content and derived knowledge through explicit lineage.

Start with the [quickstart](../README.md), then use the [API and integration guide](api.md) to wire capture and context preparation into your harness.

| Guide | Covers |
|---|---|
| [Architecture](architecture.md) | Record log, curator, context builder, lineage and access |
| [API and integrations](api.md) | Atomic append, inspection, task context, transcript adapter, MCP |
| [Operations](operations.md) | Identity, inference, queues, audit, release policies and redaction |
| [System validation](testing.md) | Real-process PostgreSQL regression tests |
| [Evaluation strategy](evaluation.md) | Fair baselines, evidence retrieval, quality and isolation gates |

## The integration boundary

The host captures messages without requiring the agent to choose a memory tool. Before starting or resuming work, it requests relevant context and injects the returned sourced text. Explicit notes and corrections use the same append API. MCP tools remain useful for optional searches and deliberate updates.

## The trust boundary

An attributed record says who supplied content, not that the content is true. Generated notes retain the complete model input manifest and supporting source quotes. Every transitive input participates in authorization. Repetition does not create independent evidence and project membership labels do not grant access.

## Pre-release replacement

The immutable records release removes the earlier event/claim/graph lifecycle and its public endpoints. Migration 0003 is intentionally destructive to old knowledge tables; it does not offer a compatibility conversion. Start from a fresh database or export needed earlier material before applying it.
