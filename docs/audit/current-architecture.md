# Current Architecture — Verified Baseline (Phase 0)

This document describes the EURUS architecture as verified in source at v0.4.0b1, focused on the subsystems touched by the multimodal foundation batch. All class names and paths were read directly from `src/eurus/`.

## 1. Layer map

```
gateways (telegram | web | cli | tui)
    ↓ objective text
runtime (AgentRuntime → AgentEngine → Agent loop)
    ↓ Message[] history                AgentEvent stream
providers (Router → Provider adapters → HTTP transports)
    ↓ StreamEvent
events (AgentEvent live | RuntimeEvent durable | DomainEvent bus)
    ↓
storage (SQLite: runs, events, checkpoints, sessions, media, artifacts)
```

## 2. Agent execution path (verified)

1. Entry: `AgentRuntime.run(session_id, text)` (`runtime/application.py`) — per-session lock, LRU agent cache, optional run-slot semaphore.
2. `AgentEngine.run(objective, owner_id, run, budget, cancellation, on_event)` (`runtime/engine.py:332`) — creates `Run` record, wires budget tracker, cancellation watchers, checkpointing.
3. `Agent.run(text)` (`agent/loop.py:413`) — appends `Message.user(text)`; per step: request model via `Router.stream(messages, tools)`; consume `StreamEvent`s; emit `AgentEvent.text_delta` chunks; execute tool calls through `ToolExecutionBoundary` (approval, mutation locks, fencing); append tool messages; completion gate.
4. Events flow: engine buffers (`deque(maxlen=2000)`) + persists `RuntimeEvent`s (monotonic `sequence_no`, `UNIQUE(run_id, sequence_no)` with retry) + calls `on_event`.
5. `AgentRuntime._stream_durable` forwards to a queue and publishes to `AgentEventBridge` (session fan-out).
6. Web: `WebRuntime.run_turn` → `SessionHub.publish_agent_event` → WS broadcast; Telegram: `BackgroundTaskManager._forward_progress` throttled edits; TUI/CLI render directly.

## 3. Message and content model (pre-batch)

- `Message(role, content: str, tool_calls, tool_call_id, name)` — frozen dataclass, `src/eurus/models.py:39`.
- Validation: role whitelist; tool messages require `tool_call_id`; only assistant may carry `tool_calls`.
- Persistence: `SessionStore.save_messages` serializes `message.to_dict()` into `messages.payload` (JSON); full-text search tables index `message.content`.
- Providers convert to wire formats in `openai_compat.py:43-47`, `anthropic.py`, `gemini.py` — all treat `content` as a plain string.

## 4. Event systems (pre-batch)

| System | Class | Persistence | Identity | Backpressure | Retry |
| --- | --- | --- | --- | --- | --- |
| Agent streaming | `AgentEvent` | none (in-flight) | none | via bridge queues | loop-level model retry |
| In-process fan-out | `AgentEventBridge` / `SessionEvent` | none | session_id, sequence, task_id | bounded queue, drop-oldest | none |
| Run event log | `RuntimeEvent` + `EventStore` | SQLite `eurus_runtime_events` | run_id, sequence_no, payload_version, event_id | cursor tailing, limit 10000 | sequence integrity retry |
| Domain events | `DomainEvent` + `EventBus` + `DomainEventStore` | SQLite `eurus_domain_events` + outbox + deliveries | event_id, correlation_id, causation_id, dedup_key, owner_id | batch limits | delivery attempts ≤5, exponential backoff, dead-letter, stale-delivering recovery |

Domain event delivery handlers: `submit_job` (automation), `trigger_schedules`, `notify`. Owner isolation via `subscription_accepts` (owner match or global `''`/admin `'*'`).

## 5. Provider layer (pre-batch)

- `Router(providers, chain, cooldowns, first_token_timeout)` — tries `ModelRef("provider/model")` candidates in order, skipping cooldown-penalized providers; records `AttemptRecord`s.
- Provider registry from `catalog.py` BUILTIN_PROVIDERS + TOML `[providers.<name>]` extras.
- Model discovery (live): `providers/discovery.py` queries `/models` endpoints; used by TUI picker and web discover endpoint.
- Capabilities: `capabilities.py` — per-protocol constant dict, always text-only. Consumed by `gateway/web/service.py:list_providers` (API view) and `office/qa/visual.py:vision_supported`.

## 6. Media and artifacts (pre-batch)

- `MediaStore.save(session_id, filename, data, declared_mime)` — sniff magic bytes, 20 MB cap, 0600 file under `<EURUS_HOME>/media/<session>/<id>.<ext>`, row in `media_files`.
- Send path: `GatewayService.send_message` resolves `attachments` (same-session enforced, ≤8), appends `compose_attachment_block()` text to the user text.
- Artifact system: `ArtifactStore.publish(...)` (`multiagent/artifacts.py`) + `register_tool_artifact` (`tools/artifacts.py`) → `eurus_agent_artifacts` rows + runtime events `artifact.registered`; web endpoints `/api/artifacts*`.

## 7. Storage and migrations

- SQLite via `storage/sqlite.connect/managed`; WAL; migrations in `storage/migrations.py` — versioned `eurus_schema_migrations`, `LATEST_SCHEMA_VERSION = 15`, `apply_migrations(connection)` applies pending versions transactionally.
- Sessions DB (`sessions.py`) holds `messages` (JSON payloads), `session_message_search`, FTS.

## 8. Security posture (verified mechanisms to preserve)

- Web: bearer token (0600 store), origin/host allowlist, per-token rate limit, WS auth; `check_websocket`.
- Media: filename sanitization, safe id regex, magic-byte sniffing, size cap, same-session ownership on send, content served with auth, `attachment` disposition for HTML.
- Storage: 0600/0700 permissions, claim-token fencing for deliveries, `sqlite3` parameterized statements throughout.
- Tools: risk approval broker, mutation locks, execution envelopes, boundary events.
- Egress: `network/policy.py` resolver-based SSRF guard; local-only enforcement in provider factory.

## 9. Where the multimodal foundation attaches

- Phase 1 → `models.py` (content blocks) + session payload compatibility.
- Phase 2 → `agent/events.py` (streaming kinds), `events/store.py` (domain event types + schema version field), no new bus.
- Phase 3 → `providers/capabilities.py` (per-model registry, lookup, routing validation) with the existing API view preserved.
- Phase 4 → `media.py` (ingestion/registration/ownership/dedup/limits) + new `media/` processing package with optional-dependency lazy adapters.
