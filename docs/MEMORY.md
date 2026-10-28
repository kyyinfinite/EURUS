# Eurus Memory

Eurus stores long-lived memory in the existing `sessions.sqlite3` database. SQLite FTS5 is used for bounded keyword retrieval; no vector service or new dependency is required.

## Configuration

Add this section to `eurus.toml` (defaults shown):

```toml
[memory]
enabled = true
max_memories = 2000
retention_days = 0
max_content_chars = 500
```

`retention_days = 0` disables time-based expiration. The memory budget is enforced as memories are added. The configured maximum candidate size limits automatic extraction; manual writes have a separate 5,000-character hard limit.

## Lifecycle

Automatic extraction runs after a completed agent response and uses a deterministic policy over the user's own message. It does not treat file, webpage, or tool output as trusted memory policy. It currently recognizes explicit durable statements such as `Remember that ...`, preferences, project decisions, validated workflows, and explicitly labeled task outcomes. It does not store every interaction or ask an LLM to invent user facts.

Each automatically added memory records its type, confidence, source, scope, session provenance, outcome, extraction method, and timestamp. Global memories are shared intentionally; project-scoped memories are only retrieved in the same scope. Retrieval is bounded by prompt character budgets and updates usage counters. Duplicate entries are rejected. Memory content is checked for common credential patterns and selected sensitive personal-information indicators before automatic, manual, or correction writes.

Supported types are `profile`, `preference`, `project_fact`, `fact`, `episode`, `procedural`, `agent_note`, and `lesson`. Procedural memories require explicit validated-workflow language; task episodes require explicit outcome language. The runtime does not automatically promote arbitrary model-generated advice into procedural memory.

## Maintenance and privacy

`MemoryManager.statistics()` returns counts by type. `purge_expired()` removes expired records, and `enforce_budget()` applies retention and count limits. `correct()` and `forget_scoped()` verify that the target memory is visible in the requested scope before changing or deleting it. SQLite triggers keep the FTS index synchronized on updates and deletes. Existing database files are migrated additively when opened.

The privacy export and purge APIs on `SessionStore` include saved memories. Purging deletes memory rows and their FTS entries through existing triggers. The application should treat memory contents as untrusted context, not executable instructions.
