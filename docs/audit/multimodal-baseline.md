# Multimodal Baseline Audit — Batch 1, Phase 0

Audit date: 2026-11-07
Auditor role: Principal Python Architect / Senior AI Agent Engineer
Repository state: `eurus-agent v0.4.0b1` (tag `stabilized`), Python 3.12.16, Node 24.22.2
Method: direct source inspection of `src/eurus` (407 files) and `tests/` (266 files), plus executed quality gates. README claims were cross-checked against source; findings below cite the source of truth.

## 1. Baseline quality gate execution

| Command | Scope | Result | Notes |
| --- | --- | --- | --- |
| `ruff check src/eurus tests` | 682 files | PASS | 0 violations |
| `ruff format --check src/eurus tests` | 682 files | PASS | all files formatted |
| `mypy src` | 407 files | PASS | strict mode, 0 issues |
| `pytest -q` | 266 test files | **3424 passed, 1 skipped** | full run: 342.11 s |
| `npm run lint` | frontend | **N/A** | no `lint` script exists; no ESLint config in repo |
| `npm run typecheck` | frontend | PASS | `tsc --noEmit` clean |
| `npm run test` (vitest) | frontend | PASS | 84 tests passed |
| `npm run build` | frontend | not executed in Phase 0 | `typecheck` covers `tsc --noEmit`; build exercised only if frontend files change (none planned in Batch 1) |

Environment notes (honest disclosure):

- The first `pytest -q -x` run failed 1 test (`test_research_compat.py::test_web_fetch_engine_path_contract`) because the optional dev dependency `trafilatura` was not installed in the audit environment; the code path correctly fell back to a URL-title heuristic. After installing the declared dev extras (`aiogram`, `trafilatura`, `ddgs`, `keyring`, `cryptography`, `websockets`, `ruff`, `mypy`), the full suite passes. This was an environment gap, not a repository regression.
- 6 pytest warnings exist (`PytestWarning: asyncio mark on non-async function` in `tests/test_mcp_resilience.py`) — pre-existing, cosmetic, not fixed in this batch to keep the diff scoped.
- 1 skipped test is the browser-integration marker (requires real Chromium; skipped when unavailable) — expected behavior.

## 2. What exists today (verified in source)

### 2.1 Agent core

- `Agent` (`src/eurus/agent/loop.py:177`) — the single agent loop. `run(text: str)` yields `AgentEvent`s. Plan → step → model request (stream or chat) → tool execution → completion gate. One conversation state: `self.history: list[Message]`.
- `AgentEngine` (`src/eurus/runtime/engine.py:123`) — durable wrapper: run records, checkpointing (every 4 steps + around mutating tools), event sequencing with `UNIQUE(run_id, sequence_no)` retry, budget/cancellation watchers.
- `AgentRuntime` (`src/eurus/runtime/application.py:27`) — session pool with LRU agent cache; bridges live streaming and durability.

### 2.2 Message model

- `Message` (`src/eurus/models.py:39`) — frozen dataclass, `role: Literal["system","user","assistant","tool"]`, **`content: str`**, `tool_calls`, `tool_call_id`, `name`. Text-only by construction; serialization via `to_dict`/`from_dict` (JSON, stored in `messages.payload`).

### 2.3 Event system (four distinct mechanisms — see technical-debt doc)

- `AgentEvent` (`src/eurus/agent/events.py:24`) — in-loop streaming protocol, 12 kinds, no identity fields.
- `AgentEventBridge` (`src/eurus/runtime/event_bridge.py:31`) — in-process fan-out, bounded queues, drop-oldest backpressure.
- `EventStore` + durable `RuntimeEvent` (`src/eurus/storage/events.py:44`, `src/eurus/runtime/models.py:58`) — per-run append-only log with `payload_version`, cursor tailing API.
- `EventBus` + `DomainEventStore` (`src/eurus/events/bus.py:51`, `src/eurus/events/store.py:113`) — domain events, outbox pattern, subscriptions, deliveries with retry/backoff/dead-letter, owner isolation, dedup keys.

### 2.4 Provider layer

- `Provider` ABC (`src/eurus/providers/base.py:161`) with `chat()`/`stream()`; `ChatRequest(model, messages, tools, ...)`, `StreamEvent(kind: text|tool_call|usage|done)`.
- Adapters: `openai_compat.py` (openai/groq/nvidia/openrouter/deepseek/mistral/together/xai/ollama), `anthropic.py`, `gemini.py`, `genai_sdk.py`, `groq_sdk.py`.
- `Router` (`providers/router.py:45`) — fallback chain with cooldowns, attempt records, first-token timeout.
- Capability registry exists but is **degenerate**: `providers/capabilities.py` maps every protocol to the same `TEXT_ONLY_PROFILE` dict. No per-model data, no lookup, no routing awareness. `capability_view().multimodal` is always `False`.

### 2.5 Gateways

- `GatewayAdapter` protocol (`gateway/contracts.py`) with `GatewayMessage(platform, user_id, conversation_id, message_id, text)`.
- Telegram: aiogram long polling → `GatewayRouter` → `BackgroundTaskManager` durable tasks; live streaming via message edits; rate limits; approval keyboards.
- Web: FastAPI (`gateway/web/app.py`) — REST + WebSocket `/api/ws` (the only streaming channel; no SSE server). `SessionHub` broadcasts `agent_event`/`run_event`/`run_status`/`approval_*` frames with WS send timeouts as backpressure.

### 2.6 Media (today)

- `MediaStore` (`src/eurus/media.py:189`) — SQLite `media_files` + per-session 0600 files, 20 MB cap, magic-byte `sniff_mime`, images/audio/video/pdf/office/text kinds, sanitize + safe ids.
- Web upload endpoints (`POST /api/media`, `GET /api/media/{id}/content`, ...), `SendMessageRequest.attachments` (max 8).
- **The gap**: `compose_attachment_block()` (media.py:335) flattens attachments into text markers/paths appended to the user message string. The model never receives image/audio parts. Frontend already renders media blocks for the user and shows a "text-only today" notice.
- Separate artifact system: `multiagent/artifacts.py` `ArtifactStore` (durable tool outputs) + `tools/artifacts.py` registration hooks.

### 2.7 Frontend

- React 18 + TS + Vite + zustand + TanStack Query. Message shape `MessageView{sequence, role, content, tool_calls, tool_call_id, name}` — mirrors backend.
- Media rendering components already exist (`components/media/MediaBlocks.tsx`, composer attachments with XHR upload). No frontend changes are required or planned in Batch 1.

## 3. Incomplete / broken / risky (verified findings)

1. **Message architecture is text-only**: `Message.content: str`; every provider renders it as a plain string on the wire. No place to attach parts.
2. **Capability registry is a stub**: per-protocol constant, always text-only; `office/qa/visual.py:vision_supported()` is permanently `False`, so its visual QA falls back to a base64-as-text hack that cannot work.
3. **Four event mechanisms with no shared envelope**: `AgentEvent` (no identity), `SessionEvent` (session/task), `RuntimeEvent` (run-scoped, `payload_version`), `DomainEvent` (correlation/causation/dedup/owner, no schema version, no session/run ids).
4. **`RuntimeEvent` name collision**: `runtime/models.py:58` class vs `runtime/event_bridge.py:18` alias `RuntimeEvent = SessionEvent`.
5. **Media processing is absent**: no metadata extraction (duration/resolution), no thumbnails, no dedup (sha256 stored but unused for lookup), no processing lifecycle, no resource governance beyond the 20 MB upload cap, no processing errors taxonomy.
6. **Engine writes runtime events synchronously on the event loop** (`runtime/engine.py` `record_event` inline SQLite) while `EventStore.append_async` exists unused — latency hazard for streaming.
7. **Layering inversion**: `events/bus.py` imports `automation.engine` / `workflows.definition` — the event system is not a leaf module.
8. Text-accumulation-with-`text_reset` idiom duplicated in four independent accumulators (engine, web runtime, child runtime, telegram manager).
9. **No SSE endpoint**; WS-only streaming (fine, but must be preserved — Batch 1 must not break WS frame contracts).
10. Hub-local per-run sequence numbers (web runs) vs durable `sequence_no` (external runs) are two orderings in one channel.

## 4. Compatibility risks for the multimodal batch

| Risk | Mitigation plan |
| --- | --- |
| `Message.content: str` consumed in ~25 modules | Keep `content` as the text projection; add `blocks` alongside. All existing consumers keep working. |
| `messages.payload` JSON round-trip in SessionStore | Payload gains optional `blocks` key; `from_dict` tolerates absence → full backward compatibility; no DDL migration needed (JSON column). |
| WS `agent_event` frame shape is consumed by frontend store | Additive-only changes; existing kinds unchanged. |
| Providers render `content` per protocol | Phase 1 keeps providers text-only (capability routing is Phase 3; actual parts-on-the-wire is Batch 2+). |
| `GET /api/providers` returns `capabilities` dict with camelCase keys | New registry must keep emitting the same view shape for the frontend (`ModelCapabilities` TS type). |
| Telegram/Web gateways submit `objective: str` | Unchanged in Batch 1 (multimodal gateway is a later batch). |

## 5. Test counts (baseline, before any change)

- Python: 3424 passed, 1 skipped, 6 warnings, 266 test files.
- Frontend: 84 vitest tests passed.
- Coverage areas relevant to this batch: `test_event_bus.py` (16), `test_web_gateway_media.py` (283 lines), `test_providers.py`, `test_openai_compat.py`, `test_gemini.py`, `test_anthropic.py`, `test_router.py`, `test_router_cooldown.py`, `test_model_picker.py`, `test_agent.py`, `test_core_engine.py`, `test_engine_run_characterization.py`, `test_runtime_*`, `test_stabilization_*`.

## 6. Conclusion

The baseline is green: lint, format, strict types, and the full Python suite pass. The repository is well-engineered but structurally text-only in exactly the four places this batch must evolve: the message model, the event vocabulary, the capability registry, and the media pipeline. The existing MediaStore, event bus, and gateway contracts are solid foundations to extend rather than replace.
