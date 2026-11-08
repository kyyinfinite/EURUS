# Technical Debt Register — Baseline (Phase 0)

Debt items verified in source at v0.4.0b1. Each item lists impact and disposition for Batch 1: items marked **addressed** are fixed by this batch; **preserved** items are intentionally out of scope (documented, not hidden).

## 1. Name and abstraction collisions

| ID | Item | Evidence | Disposition |
| --- | --- | --- | --- |
| TD-1 | `RuntimeEvent` defined twice: real class `runtime/models.py:58` and alias `RuntimeEvent = SessionEvent` at `runtime/event_bridge.py:18` | `gateway/manager.py:23` imports `SessionEvent` explicitly to dodge the trap | Preserved (renaming would churn ~30 import sites across unrelated modules; Batch 1 keeps the alias but does not add new uses of the ambiguous name) |
| TD-2 | Two `FailureKind` enums (`agent/evaluation.py:20`, `runtime/recovery.py:17`) | same name, different taxonomies | Preserved (unrelated to multimodal scope) |
| TD-3 | Two "artifact" concepts: backend `ArtifactStore` vs frontend `ChatArtifact` (client-side code-block previews) | no linkage | Preserved (converged in a later batch via `ArtifactBlock` groundwork laid in Phase 1) |

## 2. Event system fragmentation

| ID | Item | Evidence | Disposition |
| --- | --- | --- | --- |
| TD-4 | Four event mechanisms, no shared envelope/identity | see audit doc §2.3 | Partially addressed: Phase 2 adds typed multimodal event kinds to the existing `AgentEvent` and typed domain event constants + `schema_version` on domain events; no second bus created |
| TD-5 | `AgentEvent` has no id/timestamp/session/correlation identity — every layer re-attaches it | `agent/events.py:24` | Preserved envelope shape (compatibility); new multimodal events carry identity via existing wrappers |
| TD-6 | Domain events lack `schema_version` | `events/store.py:68` | **Addressed in Phase 2** (additive column + default on read) |
| TD-7 | `events/bus.py` imports automation/workflow layers (inverted layering) | bus.py imports | Preserved (structural refactor out of scope; new multimodal event types do not worsen it) |
| TD-8 | Engine persists runtime events synchronously on the event loop; `EventStore.append_async` unused | `runtime/engine.py` record_event | Preserved (perf item; not required for foundation correctness) |

## 3. Capability registry debt

| ID | Item | Evidence | Disposition |
| --- | --- | --- | --- |
| TD-9 | Per-protocol constant capabilities; `multimodal` always false | `providers/capabilities.py` | **Addressed in Phase 3** (per-model typed registry + lookup + routing validation, preserving API view shape) |
| TD-10 | `office/qa/visual.py:vision_supported` permanently disabled; base64-as-text hack | visual.py:157 | **Addressed in Phase 3** (registry now reports real image-input capability; hack remains but is no longer forced off) |
| TD-11 | No static per-model metadata anywhere (models only live-discovered) | `providers/discovery.py` | **Addressed in Phase 3** (declarative capability catalog per provider+model with override chain) |

## 4. Media debt

| ID | Item | Evidence | Disposition |
| --- | --- | --- | --- |
| TD-12 | sha256 stored but no dedup lookup | `media.py:225` | **Addressed in Phase 4** (`find_by_sha256` + dedup policy on ingest) |
| TD-13 | No media processing lifecycle (validation/processing states, errors) | `media.py` | **Addressed in Phase 4** (`media_assets` lifecycle model + typed errors) |
| TD-14 | No metadata extraction (duration/resolution/dimensions) | absent | **Addressed in Phase 4** (optional-dependency adapters with lazy init) |
| TD-15 | Limits hardcoded (`MAX_MEDIA_BYTES`, 12k inline) scattered | `media.py:17,100` | **Addressed in Phase 4** (central configurable `MediaLimits`) |
| TD-16 | `sniff_mime` returns `application/octet-stream` for undetermined ZIP containers, then `save` maps it through `EXT_BY_MIME` → KeyError risk | media.py:171 + 224 | **Addressed in Phase 4** (signature validation tightened; octet-stream rejected explicitly) |
| TD-17 | `import datetime as _dt` inside function body; duplicate `from contextlib import suppress` import | media.py:231, 304 | **Addressed in Phase 4** (module rewritten cleanly without comments) |

## 5. Duplication

| ID | Item | Evidence | Disposition |
| --- | --- | --- | --- |
| TD-18 | text-reset accumulation re-implemented 4× | engine.py, web/runtime.py, child_runtime.py, manager.py | Preserved (streaming contract untouched in Batch 1) |
| TD-19 | `TaskExecutionController` duplicates loop plan-step retry logic | agent/execution.py vs loop.py | Preserved (out of scope) |
| TD-20 | Frontend has two media component sets (`components/media/MediaBlocks.tsx` vs `components/chat/media.tsx`) | frontend tree | Preserved (frontend out of scope for Batch 1) |

## 6. Test debt

| ID | Item | Evidence | Disposition |
| --- | --- | --- | --- |
| TD-21 | 6 `PytestWarning`s for sync tests marked asyncio | `test_mcp_resilience.py` | Preserved |
| TD-22 | No tests exercise `capabilities_for_protocol` / `capability_view` | grep over tests | **Addressed in Phase 3** (new registry fully tested) |
| TD-23 | Media tests cover store + gateway but no processing/lifecycle | `test_web_gateway_media.py` | **Addressed in Phase 4** |
| TD-24 | Engine event-write latency untested (sync SQLite on loop) | — | Preserved |

## 7. Security observations (all preserved mechanisms, none weakened by Batch 1)

- `sniff_mime` trusts extension fallback when magic bytes unknown (TD-16 fix tightens this in Phase 4).
- Media content endpoint serves any authenticated principal any media id in the DB (session ownership enforced only at send time). Phase 4 adds an explicit ownership check helper used by new APIs without changing the existing endpoint contract.
- No raw binary ever enters message payloads today — preserved as an invariant in Phase 1 (blocks reference assets only).
