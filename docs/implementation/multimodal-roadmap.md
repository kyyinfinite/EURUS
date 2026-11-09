# Multimodal Foundation Roadmap — Batch 1 (Phases 0–4)

This is the living progress document for EURUS's evolution into a multimodal autonomous agent.
Statuses: `NOT_STARTED`, `IN_PROGRESS`, `BLOCKED`, `COMPLETED`, `VERIFIED` (VERIFIED requires executed-test evidence recorded below).

Out of scope for Batch 1 (next batches): vision intelligence, Telegram multimodal gateway, audio/voice intelligence, video intelligence, image/video generation, frontend multimodal experience, multimodal memory.

---

## Phase 0 — Baseline Audit

Status: **COMPLETED**

- Objective: inspect the real repository, execute all quality gates, record honest results, identify architecture problems before any code change.
- Files inspected: `src/eurus/agent/` (loop, events, factory), `src/eurus/runtime/` (engine, application, event_bridge, models), `src/eurus/events/` (bus, store), `src/eurus/storage/` (events, migrations, sqlite), `src/eurus/providers/` (base, router, factory, capabilities, openai_compat, anthropic, gemini, discovery), `src/eurus/gateway/` (contracts, router, manager, telegram/, web/), `src/eurus/models.py`, `src/eurus/media.py`, `src/eurus/sessions.py`, `src/eurus/catalog.py`, `pyproject.toml`, `tests/` (266 files), `frontend/` (structure, package.json).
- Files created: `docs/audit/multimodal-baseline.md`, `docs/audit/current-architecture.md`, `docs/audit/technical-debt.md`, `docs/audit/test-baseline.md`, `docs/implementation/multimodal-roadmap.md`.
- Files modified: none (source untouched in Phase 0).
- Architectural decisions:
  1. Extend, never duplicate: no second event bus, no second media store, no second agent loop.
  2. `Message.content: str` stays as the canonical text projection; multimodal parts ride in a new optional `blocks` field — full backward compatibility for ~25 consuming modules.
  3. Session message payloads are JSON columns → additive `blocks` key needs no DDL migration; compatibility proven by round-trip tests instead.
  4. Domain events gain an additive `schema_version` column (default 1) via migration 16; readers tolerate legacy rows.
  5. Provider capabilities become per-model typed declarations layered over per-protocol defaults, exposed through the existing `/api/providers` view shape.
  6. Media processing lives in a new `eurus.media` package with optional external tools (Pillow, ffprobe/ffmpeg) loaded lazily; base install must not require them.
- Tests added: none (audit phase).
- Tests executed: `ruff check` PASS · `ruff format --check` PASS · `mypy src` PASS · `pytest -q` 3424 passed / 1 skipped · frontend `typecheck` PASS · frontend `vitest` 84 passed · `npm run lint` N/A (no script).
- Known limitations: 6 pre-existing pytest warnings (asyncio mark on sync tests) left as-is; `npm run build` not executed (no frontend changes planned).
- Completion status: **COMPLETED** (audit delivered; no source changes to verify).

---

## Phase 1 — Unified Multimodal Message System

Status: **VERIFIED**

- Objective: typed content blocks (Text/Image/Audio/Video/VideoFrame/Document/Artifact) with validation and serialization; messages support mixed content while remaining fully backward compatible for text-only consumers.
- Files inspected: `models.py`, `agent/loop.py`, `runtime/engine.py`, `runtime/application.py`, `gateway/web/service.py`, `gateway/web/runtime.py`, `gateway/web/schemas.py`, `sessions.py`, `agent/context.py`, `agent/history.py`, provider wire renderers.
- Files created: `src/eurus/content.py`; `tests/test_content_blocks.py`; `tests/test_message_blocks_integration.py`.
- Files modified: `src/eurus/models.py` (Message.blocks + factories + validation + serialization), `src/eurus/agent/loop.py` (run/_run_events/_prepare_run accept blocks), `src/eurus/runtime/engine.py` (run accepts blocks, compatibility-layer call), `src/eurus/runtime/application.py` (run/run_agent/_stream/_stream_durable accept blocks; ephemeral + durable paths), `src/eurus/gateway/web/runtime.py` (run_turn accepts blocks), `src/eurus/gateway/web/service.py` (attachments → validated content blocks), `src/eurus/media.py` (attachment_content_blocks bridge), `tests/test_web_gateway_media.py` (2 gateway-level block tests added).
- Architectural decisions:
  1. `Message.content` remains the canonical text projection; blocks ride in an optional `blocks: tuple[ContentBlock, ...]` field. When blocks are present and content is empty, `__post_init__` auto-fills content with the text projection, so search/FTS/context/providers keep working unchanged.
  2. Blocks are frozen dataclasses in a tagged-union with strict `from_dict` (unknown fields rejected → binary payloads are structurally impossible, not just discouraged); asset references must match `^[a-f0-9]{32}$` (media and artifact id format).
  3. Payload compatibility: `to_dict` omits `blocks` when empty; `from_dict` tolerates legacy payloads without the key → no DDL migration needed (JSON payload column). Verified by legacy-payload and session-store round-trip tests.
  4. Duck-type compatibility layer: the engine/runtime only pass the `blocks` kwarg when non-empty, so any existing `Agent.run(text)` implementation (including test doubles and older integrations) keeps working.
  5. Web gateway keeps the text marker bridge AND adds structured blocks to the same user message — text-only models remain fully functional until Phase 3 routing lands.
  6. `MessageView` API schema unchanged (frontend compatibility); blocks are persisted but not yet exposed in REST views (frontend experience is a later batch).
- Tests added: 68 content-block tests (validation, round-trips, binary rejection, caps, ordering, projections, asset references) + 11 pipeline integration tests (agent/engine/runtime/session/checkpoint/wire) + 2 gateway tests (blocks from attachments; unknown attachment 404) + legacy compat tests.
- Tests executed: phase suites (104 passed); targeted regression suites for touched modules (272 passed); **full suite: 3505 passed, 1 skipped, 6 warnings** (baseline 3424 + 81 new, zero regressions); `ruff check` PASS; `ruff format --check` PASS; `mypy src` PASS (408 files).
- Known limitations: blocks are not yet rendered by providers on the wire (Batch 2+); REST message views do not expose blocks (frontend batch); no per-block persistence indexing.
- Completion status: **VERIFIED** (full-suite evidence above).

---

## Phase 2 — Multimodal Event System

Status: **VERIFIED**

- Objective: typed multimodal event vocabulary layered on the existing event infrastructure (no second event bus): new `AgentEvent` streaming kinds, typed domain event contracts with payload validation, `schema_version` on domain events, engine translation, WS view serialization, and gateway emission wiring.
- Files inspected: `agent/events.py`, `events/store.py`, `events/bus.py`, `events/__init__.py`, `runtime/engine.py` (record_event translation), `gateway/web/hub.py` (_agent_event_view), `gateway/web/service.py` (send/upload paths), `storage/migrations.py`, `runtime/event_bridge.py`, `storage/database.py` (auto-migration on connect).
- Files created: `src/eurus/events/types.py` (typed vocabulary + payload validators + schema versions), `src/eurus/events/multimodal.py` (`MultimodalEventPublisher`, `safe_publish`), `tests/test_multimodal_events.py`.
- Files modified: `src/eurus/agent/events.py` (7 new kinds + block/media/artifact fields + factories), `src/eurus/events/store.py` (DomainEvent.schema_version, publish param, INSERT, row mapping with legacy fallback), `src/eurus/events/bus.py` (schema_version pass-through), `src/eurus/events/__init__.py` (lazy exports), `src/eurus/storage/migrations.py` (migration 16 + LATEST_SCHEMA_VERSION=16), `src/eurus/runtime/engine.py` (translation of 7 new kinds to durable runtime events + `_block_event_payload`), `src/eurus/gateway/web/hub.py` (_agent_event_view serializes new fields additively), `src/eurus/gateway/web/service.py` (lazy MultimodalEventPublisher; MEDIA_RECEIVED+kind-specific on upload; MESSAGE_RECEIVED on send with attachments).
- Architectural decisions:
  1. Single event infrastructure preserved: streaming events extend the existing `AgentEvent` protocol; durable domain events extend `DomainEventStore`; no new bus, no new delivery mechanism.
  2. Durable vs streaming distinction preserved: inbound + processing lifecycle are durable domain events (outbox, dedup, retry, owner isolation); content-block/audio/artifact output are streaming `AgentEvent`s translated to per-run `RuntimeEvent`s by the engine (monotonic sequence_no).
  3. New streaming kinds follow existing naming conventions (lowercase snake: `content_block_started`, `content_block_delta`, `content_block_completed`, `media_input_analyzing`, `audio_output_started`, `audio_output_delta`, `artifact` with status field). WS frames remain additive-only — legacy frontend behavior untouched.
  4. `schema_version` is additive with DEFAULT 1 (migration 16); readers tolerate legacy rows; publish auto-resolves per-type versions from `SCHEMA_VERSIONS`.
  5. Payload contracts are strictly validated (typed required/optional fields, hex asset refs, constrained stages, binary rejection) — raw media bytes can never enter event payloads.
  6. Gateway emission is best-effort with explicit logging (`safe_publish`); publishing happens off the event loop via `asyncio.to_thread`; the web gateway does not run the dispatch loop, so publication has no in-process side effects — automation subscribers pick events up through the existing outbox.
  7. Dedup keys: `media-received:{id}`, `{kind}-received:{id}`, `message-received:{source}:{message_id}` — idempotent uploads/sends.
- Tests added: 41 tests — vocabulary completeness, per-type payload validation (parametrized over all 15 types), kind-specific enforcement, asset-id/binary/stage rejection, AgentEvent factories + status validation, schema-version round-trip, explicit version override + invalid versions, migration column addition, legacy-row default, publisher generic+specific emission, dedup idempotency, message-received correlation, processing lifecycle with causation id, invalid payload rejection, safe_publish logging, event ordering/correlation, gateway upload emission (end-to-end), engine translation of all 7 new kinds (durable event types + payloads), hub view serialization (incl. legacy shape), bridge relay of block events.
- Tests executed: phase suite 41 passed; event regression suites 125 passed; **full suite: 3546 passed, 1 skipped, 6 warnings** (3505 + 41, zero regressions); `ruff check` PASS; `ruff format --check` PASS; `mypy src` PASS (410 files).
- Known limitations: `AgentEvent` identity (id/timestamp) still attached by wrappers (TD-5 preserved); domain event schema versions are all 1 (bump protocol documented, no v2 yet); transcription/media-analysis events are defined and validated but not yet emitted (emission arrives with Batch 2 intelligence phases).
- Completion status: **VERIFIED** (full-suite evidence above).

---

## Phase 3 — Provider Capability Registry

Status: **VERIFIED**

- Objective: replace the degenerate per-protocol text-only capability constant with a typed, per-model capability registry supporting lookup, modality-aware routing decisions, request-compatibility validation, structured unsupported-modality errors, provider declarations, and TOML capability configuration — all preserving the existing `/api/providers` view shape.
- Files inspected: `providers/capabilities.py`, `providers/router.py`, `providers/factory.py`, `providers/discovery.py`, `gateway/web/service.py` (list_providers, discover_provider), `office/qa/visual.py` (vision_supported), `config.py` (_build_providers), `catalog.py` (ProviderSpec), frontend `types.ts` (ModelCapabilities view shape).
- Files created: `src/eurus/providers/model_capabilities.py` (ModelCapabilities, CapabilityRegistry, MODEL_DECLARATIONS catalog, modality/routing planning, UnsupportedModalityError), `tests/test_model_capabilities.py`.
- Files modified: `src/eurus/providers/capabilities.py` (delegates to registry; adds `model_capability_view`/`model_capabilities` helpers; view shape preserved), `src/eurus/providers/router.py` (optional `capability_registry` + `capabilities_for(ref)`), `src/eurus/providers/factory.py` (`_registry_from_config` wires TOML provider capability declarations into routers), `src/eurus/catalog.py` (ProviderSpec.capabilities), `src/eurus/config.py` (`[providers.<name>.capabilities]` parsing with snake/camel keys, unknown-key and non-boolean rejection), `src/eurus/office/qa/visual.py` (per-model vision_supported — fixes permanently-disabled visual QA path TD-10), `src/eurus/gateway/web/service.py` (list_providers gains `model_capabilities`; discover_provider returns per-model `capabilities`).
- Architectural decisions:
  1. Typed `ModelCapabilities` dataclass with the 12 required dimensions (snake_case fields); `to_view()`/`from_view()` preserve the exact camelCase API shape the frontend consumes.
  2. Resolution chain per (provider, model): exact declaration → longest family prefix (segment-boundary match, so `gpt-4` does not swallow `gpt-4o`) → provider-level declaration (TOML/programmatic) → protocol default → safe text-only default. Unknown models are NEVER assumed multimodal.
  3. Declarative catalog covers well-known OpenAI/Anthropic/Gemini/groq/mistral/xai/openrouter/ollama model families using conservative, documented capability facts (e.g. gpt-4o/gpt-4.1/claude-3+/gemini-1.5+ image+document; gemini audio+video; o1-mini text-only-no-tools; whisper/tts/dall-e/imagen/veo declared with their generation/transcription profiles). Anything not declared stays text-only.
  4. Routing is a pure planning function over the chain: `plan_routing`/`capable_chain`/`missing_modalities`/`required_modalities` — testable without live API keys; the Router carries an optional registry and exposes `capabilities_for(ref)`; actual modality-aware request dispatch is deferred to Batch 2 when parts go on the wire (documented scope decision).
  5. `UnsupportedModalityError` is a structured `EurusError` carrying required modalities and per-candidate gaps — never a silent text-only fallback at the routing layer (the message-level text bridge remains the explicit compatibility path).
  6. Capability configuration: `[providers.<name>.capabilities]` TOML tables (snake or camel keys, strict validation) + programmatic `declare_model/declare_provider`; the factory injects the resolved registry into every Router.
  7. Provider usage metadata: `/api/providers` still emits the protocol-level view (frontend-compatible) plus additive `model_capabilities`; `/api/providers/{name}/discover` adds per-model `capabilities` views (additive JSON, ignored by current frontend).
- Tests added: 60 tests — key coverage, defaults, view-shape backward compatibility, view round-trips + rejection, declared-model expectations (16 parametrized), undeclared models stay text-only, family-boundary matching, longest-prefix precedence, provider-level and exact overrides, declaration validation, registry isolation (empty-declarations bug found and fixed), lookup_ref, modalities from all 7 block types, missing-modalities, plan_routing selection/failure/structured error, capable_chain ordering, custom registries, Router registry injection, vision_supported activation, TOML configuration (accept/reject/camel/legacy), default-registry stability, catalog provider whitelist.
- Tests executed: phase suite 60 passed; provider regression suites 189 passed; **full suite: 3606 passed, 1 skipped, 6 warnings** (3546 + 60, zero regressions); `ruff check` PASS; `ruff format --check` PASS; `mypy src` PASS (411 files).
- Known limitations: capability facts for openrouter/together/nvidia sub-catalogs are family-level approximations (overridable per provider/model); routing decisions are not yet enforced inside `Router.stream/chat` (Batch 2 wires modality-aware dispatch); no live API key verification of catalog facts (documented as declarative data, overridable via TOML).
- Completion status: **VERIFIED** (full-suite evidence above).

---

## Phase 4 — Media Infrastructure

Status: **VERIFIED**

- Objective: evolve the existing `MediaStore` into a governed media infrastructure — ingestion/registration/dedup/ownership/lifecycle, tightened signature validation, optional-dependency processing adapters with resource governance, configurable limits, retention, and path safety — without duplicating storage or breaking the gateway contract.
- Files inspected: `media.py` (pre-refactor), `gateway/web/service.py` (upload/send/media endpoints), `gateway/web/app.py` (upload read loop + MAX_UPLOAD_BYTES), `storage/sqlite.py` (managed/commit semantics), `paths.py`, `multiagent/artifacts.py` (artifact ids), gateway media tests.
- Files created: `src/eurus/media/__init__.py` (backward-compatible re-exports), `src/eurus/media/errors.py` (typed error taxonomy with codes), `src/eurus/media/limits.py` (`MediaLimits` + env configuration + validators), `src/eurus/media/signatures.py` (tightened magic-byte/OOXML sniffing + path-safety helpers), `src/eurus/media/store.py` (MediaStore with dedup, ownership, lifecycle, metadata columns, retention, containment checks), `src/eurus/media/processing.py` (MediaProcessor with governance + Pillow/ffprobe/office lazy adapters + `run_processing_pipeline`), `tests/test_media_infrastructure.py`, `tests/test_multimodal_integration.py`.
- Files modified: `src/eurus/media.py` **deleted** (replaced by the package; all import paths preserved), `src/eurus/gateway/web/service.py` (owner-tagged saves, processing pipeline on upload, lazy processor), `src/eurus/gateway/web/app.py` (MAX_UPLOAD_BYTES unified with `MediaLimits.from_environ`), `tests/test_web_gateway_media.py` (2 tests adapted: env-based oversize limit; declared-mismatch now rejected — deliberate security tightening).
- Architectural decisions:
  1. `eurus.media` module became a package with identical public names — zero importer changes; lazy `__getattr__` keeps `MediaProcessor` importable without heavy deps.
  2. Lifecycle model: `registered` → `processing` → `ready`/`failed` (+ `error_code`, `metadata_json` columns); schema evolution handled inside `_ensure_schema` with PRAGMA-checked ALTERs (media_files lives in a store-managed DB, not the central migrations table) — legacy rows default to `ready`, migration proven by test.
  3. Dedup: sha256 lookup scoped per session; `save` returns the existing asset (idempotent uploads); `deduplicate=False` opt-out.
  4. Security hardening: `safe_session_component` blocks traversal session ids at the store; `storage_path_for` enforces root containment; declared-MIME vs signature vs extension conflicts now REJECT (`SIGNATURE_MISMATCH`) instead of silently trusting declarations; undetermined ZIP containers raise structured errors (fixes the KeyError 500 path, TD-16); stricter MP3 sync-byte sanity; new `video/quicktime` + `audio/flac` allowed types.
  5. Resource governance centralized in `MediaLimits` (max bytes/duration/pixels/dimension, processing timeout, concurrency semaphore, retry cap, thumbnail bound, retention days) configurable via `EURUS_MEDIA_*` env vars; the web upload cap derives from the same limits (removes the duplicated 20 MB constant).
  6. Processing adapters are lazy: Pillow for images, ffprobe via subprocess (which-check + timeout + temp-file probe) for audio/video, python-docx/openpyxl/python-pptx for office metadata, stdlib regex for approximate PDF page counts; missing decoders yield `{"decoder": "unavailable"}` instead of failures; base installation requires none of them; no ML models are downloaded.
  7. Processor governance: extraction runs in `asyncio.to_thread` under a semaphore, wrapped in `wait_for(timeout)`; retryable errors retry up to `max_processing_retries`; limit violations and crashes become structured failed outcomes (`limit_exceeded`, `corrupt_media`) rather than exceptions; the pipeline records lifecycle transitions in the store.
  8. Ownership: saves record `owner_id` (web gateway stamps `eurus`); `get_owned` denies foreign owners; `get_for_session` scopes by session.
- Tests added: 59 media-infrastructure tests (ingestion/registration/dedup/owner/traversal, signature matrix incl. OOXML profiles and mismatch rejection, Pillow/ffprobe/office metadata, lifecycle transitions, timeout/limits/concurrency/cancellation semantics, optional-dependency unavailability, legacy-schema migration, retention purge, containment, delete, compat re-exports) + 1 end-to-end integration test.
- Tests executed: phase suite 59 passed; media-adjacent regression suites 275 passed; **full suite: 3667 passed, 1 skipped, 7 warnings** (3606 + 61, zero regressions); `ruff check` PASS; `ruff format --check` PASS (697 files); `mypy src` PASS (416 files). No frontend files changed (package-lock noise reverted); frontend checks not required per batch rules and were green at baseline.
- Known limitations: processing cancellation abandons the worker thread's result (Python thread semantics) — the timeout still bounds the outcome; `purge_expired` is implemented and tested but not scheduled (Batch 2 can wire it into the supervisor); thumbnails/conversion adapters are prepared (limits + workspace) but generation itself is Batch 2; PDF page counting is a regex heuristic (documented approximation).
- Completion status: **VERIFIED** (full-suite evidence above).

---

## Integration verification (end of Batch 1)

Status: **VERIFIED**

- Required flow verified by `tests/test_multimodal_integration.py` (1 test walking the whole chain): gateway input (authenticated upload) → media infrastructure (registered → processed → ready with Pillow metadata; owner recorded) → multimodal event system (MEDIA_RECEIVED + IMAGE_RECEIVED published with schema_version 1 and validated payloads) → unified message (send with attachment → user message carries validated ImageBlock + text bridge) → content-block validation (asset id, mime, ordering) → provider capability validation (text-only chain produces structured UnsupportedModalityError; vision chain routes to `openai/gpt-4o`) → media asset reference (block asset resolves to the stored, session-scoped asset) → agent runtime (run executed end-to-end; message persisted with blocks).
- Binary-free invariant asserted at the payload level (no base64/raw PNG bytes in serialized messages).
- Final gates: `ruff check src/eurus tests` PASS · `ruff format --check` PASS (697 files) · `mypy src` PASS (416 files, strict) · `pytest -q` **3667 passed, 1 skipped** (baseline 3424 + 243 new, zero regressions) · frontend unchanged (typecheck/vitest green at baseline; not rerun because no frontend files were modified).
- Batch scope respected: no vision reasoning, no Telegram multimodal, no audio/video intelligence, no image/video generation, no frontend multimodal work, no multimodal memory — extension points only (content blocks, event vocabulary, capability routing, processing adapters).

---

# Multimodal Intelligence Roadmap — Batch 2 (Phases 5–7)

## Batch 1 verification audit (before Batch 2 work)

Status: **VERIFIED**

- Code, contracts, migrations and tests were re-inspected directly (not assumed from the report): `content.py` (7 validated block types), `media/` package (store/signatures/limits/processing/errors), `providers/model_capabilities.py` (12-dimension registry + declarative catalog + routing planners), `events/types.py` + `events/multimodal.py` (15 typed event contracts), `models.py` (`Message.blocks`), `agent/loop.py` + `runtime/engine.py` + `runtime/application.py` (blocks flow), `gateway/web/service.py` (upload → blocks → events pipeline), `storage/migrations.py` (migration 16), media-file ALTER migrations inside the store.
- Baseline gates re-executed on this environment: `ruff check src/eurus tests` PASS · `ruff format --check src/eurus tests` PASS (697 files) · `mypy src` PASS (416 files) · `pytest -q` **3667 passed, 1 skipped**.
- Confirmed gaps that Batch 2 must close (consistent with the Batch 1 report's known limitations):
  1. Provider wire payloads are text-only — `openai_compat.message_to_wire`, `anthropic.convert_messages`, and `gemini.convert_messages` render `Message.content` only; no image parts are ever sent. Capability routing is computed (`plan_routing`) but NOT enforced inside `Router.chat/stream`.
  2. `GatewayMessage` carries `text` only; the Telegram dispatch handler reads `message.text` and registers only `F.text` handlers — no media can enter through Telegram.
  3. No STT/TTS provider abstraction exists anywhere in `src/` (only capability declarations for whisper/tts models in the registry).
  4. `sniff_mime` classifies all `ftyp`/EBML containers as video kinds regardless of declared audio MIME — m4a/webm audio would be misrouted.
- Conclusion: the foundation is real and green; Batch 2 proceeds on top of it without architectural redesign.

---

## Phase 5 — Vision Intelligence

Status: **VERIFIED**

- Objective: send real image input to vision-capable models and use the result inside the agent loop — image pipeline on the unified message system, per-protocol vision wire adapters, capability-enforced routing, and media-analysis events.
- Files created: `src/eurus/providers/media_parts.py` (asset resolution + per-protocol wire parts + inline caps + wire-modality map), `tests/test_vision_intelligence.py` (21 tests).
- Files modified: `src/eurus/providers/base.py` (Provider accepts an optional `asset_resolver`), `src/eurus/providers/openai_compat.py` (`message_to_wire` renders `image_url` data-URL parts; resolver-aware `_payload`), `src/eurus/providers/anthropic.py` (`convert_messages` renders image + base64-PDF document blocks), `src/eurus/providers/gemini.py` (`convert_messages`/`build_payload` render `inline_data` parts), `src/eurus/providers/genai_sdk.py` (shares the Gemini conversion, so SDK protocol gains vision), `src/eurus/providers/groq_sdk.py` (forwards the resolver; inherits OpenAI wire rendering), `src/eurus/providers/router.py` (`_routed_candidates` enforces per-model capability + per-protocol wire modalities at request time; text-bridge fallback strips blocks when nothing capable exists), `src/eurus/providers/model_capabilities.py` (`required_modalities_for_messages`, duck-type tolerant), `src/eurus/providers/factory.py` (`build_router` injects a lazy `MediaStoreAssetResolver`; aux router stays text-only), `src/eurus/agent/loop.py` (emits `media_input_analyzing` for incoming media blocks).
- Architectural decisions:
  1. Vision rendering happens in the existing provider payload builders — no new provider classes, no new agent loop; blocks ride the unified Message into each protocol's native format (OpenAI `image_url` data URLs, Anthropic `image`/`document` base64 sources, Gemini `inline_data`).
  2. Routing enforcement sits inside `Router.chat/stream`: candidates are filtered by registry capabilities AND by what the protocol can actually put on the wire (`WIRE_MODALITIES_BY_PROTOCOL`); PDF requests therefore prefer Anthropic/Gemini over OpenAI chat-completions.
  3. When no model in the chain supports the required modalities, the request degrades to the pre-existing text bridge (blocks stripped, attachment markers remain) and a warning is logged — no silent image-to-text-model sends, no hard crash.
  4. Assets resolve lazily from the single MediaStore (shared database), off-loop via `asyncio.to_thread`, bounded by `MAX_MEDIA_PARTS_PER_REQUEST = 16` most-recent-first; mime agreement between block and stored asset is enforced; per-protocol inline byte caps (OpenAI 20 MB image, Anthropic 5 MB image/20 MB PDF, Gemini 15 MB) skip oversized assets back to the text bridge instead of failing the request.
  5. `build_router` self-wires a lazy `MediaStoreAssetResolver`, so web, CLI, TUI, and Telegram paths get vision without new plumbing; tests constructing providers directly stay text-only unless a resolver is injected.
  6. Image questions (description, VQA, screenshots, UI inspection, OCR, comparison, diagram explanation) are answered by the vision model itself — no duplicate tools.
- Tests added: 21 — serialization without binary leakage, store-backed resolution, missing asset / mime mismatch / oversized / unsupported-mime fallbacks, multi-image ordering, GIF allowlist split (OpenAI+Anthropic vs Gemini), all three wire conversions, PDF protocol preference, text-only model text bridge, provider-error fallback between vision models, streaming vision, agent conversation with text+image (payload + `media_input_analyzing` + history retention), cross-turn image context, modality helpers.
- Tests executed: phase suite 21 passed; provider/agent regression suites 173 passed; **full suite: 3688 passed, 1 skipped** (3667 + 21, zero regressions); `ruff check` PASS; `ruff format --check` PASS; `mypy src` PASS (417 files).
- Fixes during the phase: duck-typed history messages (no `blocks` attribute) are tolerated by `required_modalities_for_messages` (repo convention preserved); `GeminiProvider._payload` stays synchronous for direct test callers (asset resolution moved to `_chat`/`_stream`).
- Known limitations: OpenAI chat-completions wire cannot carry PDFs (text bridge or Anthropic/Gemini routing instead); images re-upload inline on every turn of a long conversation (stateless API semantics, bounded by the 16-part cap); HEIC/HEIF only render for Gemini; no Files API / upload caching yet.
- Completion status: **VERIFIED** (full-suite evidence above).

---

## Phase 6 — Telegram Multimodal Gateway

Status: **VERIFIED**

- Objective: turn the Telegram gateway into a full multimodal interface — incoming photo/voice/audio/video/video-note/document (including media groups), validated ingestion through the shared MediaStore, blocks riding the unified GatewayMessage/task pipeline, and outgoing media delivery (images, audio, voice, video, documents, run artifacts).
- Files created: `src/eurus/gateway/telegram/media.py` (TelegramMediaRef/IngestOutcome, `TelegramMediaIngester`, `TelegramMediaSender`, `MediaGroupCollector`, `extract_telegram_media`), `tests/test_telegram_multimodal.py` (26 tests).
- Files modified: `src/eurus/gateway/contracts.py` (`GatewayMessage.blocks`, additive with default), `src/eurus/gateway/router.py` (media-only messages accepted; blocks forwarded to submit), `src/eurus/gateway/manager.py` (`submit(blocks=...)` persists block dicts into the durable task state), `src/eurus/gateway/telegram/adapter.py` (media handlers for `F.photo|F.voice|F.audio|F.video|F.video_note|F.document`, async download via `bot.get_file`/`download_file`, ingestion + MESSAGE_RECEIVED publication + media-group buffering + runner wrapper that delivers run artifacts via the sender), `src/eurus/cli/gateway.py` (runner reconstructs blocks from task state and passes them to `runtime.run`), `src/eurus/media/signatures.py` (audio mp4/webm containers no longer misclassified as video: M4A-family ftyp brands, declared `audio/mp4`/`audio/webm` honored).
- Architectural decisions:
  1. One gateway pipeline: Telegram Update → adapter dispatch → async download → validation+save (single MediaStore on the same database) → unified content blocks → GatewayMessage(blocks) → GatewayRouter → durable task (blocks in `state_json`) → runner → AgentRuntime/AgentEngine → provider routing → response. No path bypasses the AgentEngine.
  2. Media groups: album messages (shared `media_group_id`) are buffered with a 1.5 s debounce window (capped at 10 items, 256 tracked contexts) and flushed as ONE GatewayMessage carrying all blocks; single media submits immediately.
  3. Duplicate-update protection reuses the existing `claim_update` gate; owner isolation stamps `scope_key("telegram", user_id)` on every saved asset and reuses `get_owned` for authorization tests; oversize files are rejected BEFORE download using Telegram's declared file_size.
  4. Download failures, timeouts, invalid files, and unsupported types become structured ingestion errors surfaced to the user as an explicit reply — the agent loop never crashes on media problems.
  5. Outgoing media: `TelegramMediaSender` picks the Bot API method by MIME (photo/voice/audio/video/document), enforces the 50 MB upload cap, retries ONLY unambiguous non-delivery errors (network/429-with-retry-after) so files are never accidentally double-sent, and logs structured failures. Run artifacts produced during a task are delivered automatically after the runner finishes (per linked run_id, so retries deliver only their own run's artifacts).
  6. Text-only behavior is untouched: `F.text` handlers, command routing, approvals, live streaming, and all existing telegram tests pass unchanged; `GatewayMessage.blocks` defaults to empty.
- Tests added: 26 — extraction (largest photo, voice/video/video-note/image-document kinds), ingestion for photo/voice/mp3/video/pdf (blocks + owner + session + ready status + events), event publication (MEDIA_RECEIVED + kind events), download failure / invalid ZIP / oversize-before-download / timeout errors, objective text markers, router block forwarding into durable task state, unauthorized-user denial, duplicate-update dedupe, media-only message processing, group collector debounce + capacity flush, sender MIME dispatch, retry semantics (network retried, non-retryable raised once, exhausted retries raise, no accidental double send), oversized and missing-file rejection, end-to-end incoming flow, GatewayMessage backward compatibility.
- Tests executed: phase suite 26 passed; gateway/media regression suites 186 passed; **full suite: 3714 passed, 1 skipped** (3688 + 26, zero regressions); `ruff check` PASS; `ruff format --check` PASS (701 files); `mypy src` PASS (418 files).
- Known limitations: media-group caption only kept from the first message; Telegram's 20 MB bot download cap applies (larger files need a premium/local Bot API server — not assumed); document thumbnails are not generated; `send_media` for a stored voice note relies on Telegram's OGG/Opus format (native for voice notes).
- Completion status: **VERIFIED** (full-suite evidence above).

---

## Phase 7 — Audio & Voice Intelligence

Status: **VERIFIED**

- Objective: audio as a native modality — transcription at ingestion, transcripts on AudioBlocks and in the text bridge, TTS with provider capability checks, voice replies through Telegram, and a realtime abstraction prepared without overreach.
- Files created: `src/eurus/audio/__init__.py`, `src/eurus/audio/config.py` (AudioConfig + formats), `src/eurus/audio/stt.py` (Transcriber protocol, `OpenAICompatTranscriber`, `build_transcriber` with capability + protocol validation), `src/eurus/audio/tts.py` (SpeechSynthesizer protocol, `OpenAICompatSpeechSynthesizer`, `build_speech_synthesizer`), `src/eurus/audio/pipeline.py` (`transcribe_media` with TRANSCRIPTION_* events + metadata, `synthesize_speech` into the MediaStore), `src/eurus/audio/speak_tool.py` (the `speak` tool with `[voice:<id>]` marker contract), `src/eurus/audio/realtime.py` (stream/session/interruption abstraction, documented as Batch 3 scope), `tests/test_audio_intelligence.py` (39 tests), `tests/test_multimodal_intelligence_integration.py` (2 end-to-end Batch 2 flow tests).
- Files modified: `src/eurus/config.py` (`[audio]` section + `EURUS_STT_*`/`EURUS_TTS_*` env overrides), `src/eurus/providers/httpx_transport.py` (additive `post_multipart` + `post_binary` with the same UA rotation/error mapping), `src/eurus/media/signatures.py` (audio flac/aac allowed; M4A-family ftyp brands and declared audio/webm keep audio containers out of the video kind), `src/eurus/media/store.py` (attachment blocks carry transcript + duration; the text bridge inlines transcripts — text-only chains can answer voice notes), `src/eurus/gateway/telegram/media.py` (ingester accepts a transcriber and transcribes voice notes at ingestion; `deliver_voice_responses` module function), `src/eurus/gateway/telegram/adapter.py` (runner wrapper resolves `[voice:<id>]` markers, sends voice/audio, strips markers), `src/eurus/cli/gateway.py` (builds transcriber/synthesizer, registers the speak tool, passes the transcriber into polling, closes audio transports), `src/eurus/gateway/web/service.py` (web uploads transcribe audio with the same pipeline), `src/eurus/agent/loop.py` (ToolContext session stamping so the speak tool stores assets under the conversation session), `docs/configuration-reference.md`, `docs/known-limitations.md`, `docs/architecture/multimodal-intelligence.md`, `eurus.example.toml`.
- Architectural decisions:
  1. STT/TTS are OpenAI-compatible HTTP clients over the existing transport (multipart `/audio/transcriptions`, binary `/audio/speech`) — no new SDK dependency; the base install is unchanged and no model is downloaded.
  2. Factories refuse misconfigured models: capability declarations (audio_input/text_output for STT, audio_output for TTS) and OpenAI-compatible protocol are validated at startup with clear ConfigErrors; empty config disables the feature silently.
  3. Transcription happens once at ingestion (Telegram voice notes + web uploads), stored as media metadata; `AudioBlock.transcript` and the text-bridge inline keep the transcript separate from metadata while making it usable by every chain, vision or text-only.
  4. Voice replies are agent-native: the `speak` tool synthesizes, stores, emits `audio.output.ready` through the tool event sink, and returns a strict `[voice:<32-hex>]` marker; the Telegram runner resolves markers after the answer, sends OGG/Opus as a voice message and other audio as audio files, and strips the markers. Missing assets and failed sends degrade to the text answer.
  5. Retries are bounded (2) and retryable-only (5xx/429/network); speech text is capped at 4096 chars; outputs above 25 MB are rejected; transcription inputs above 25 MB are rejected before upload.
  6. Realtime voice is abstraction-only (`realtime.py` protocols + states + scope note) — no transport, honestly documented.
- Tests added: 39 audio-intelligence tests (multipart/binary request shapes, language passthrough, verbose duration parsing, unsupported format/empty/oversize rejections, retry and give-up semantics, non-retryable single-shot, transport error mapping, format→mime matrix, input validation, factory capability/protocol/unknown-provider mismatches, config parsing + validation, transcribe-media transcript/events/failure/non-audio, synthesize-speech storage, speak tool marker + event + failure, ingester voice-note transcription with transcript on block and objective, voice delivery (voice vs audio vs no-marker vs missing asset), transport post_multipart/post_binary via httpx MockTransport, attachment blocks carrying transcript/duration) + 2 end-to-end integration tests (full Telegram voice flow through the gateway into the agent with TTS reply delivery; vision flow with media_input_analyzing and inline image parts).
- Tests executed: phase suite 39 passed; regression suites (config/media/telegram/web/agent/multimodal) 213 passed; **full suite: 3753 passed, 1 skipped** (3714 + 41, zero regressions); `ruff check` PASS; `ruff format --check` PASS; `mypy src` PASS (425 files).
- Known limitations: streaming transcription is not implemented (delta events exist in the vocabulary but no emitter); speaker diarization depends on provider output; the voice marker must be echoed by the model (tool description instructs it) — a text answer always arrives regardless; TTS is OpenAI-protocol only (Gemini/Anthropic native TTS not wired).
- Completion status: **VERIFIED** (full-suite evidence above).

---

## Integration verification (end of Batch 2)

Status: **VERIFIED**

- `tests/test_multimodal_intelligence_integration.py` walks the complete chain twice: (1) Telegram voice note → download → validated storage → transcription (Indonesian) → `MEDIA_RECEIVED`/`AUDIO_RECEIVED` events → `GatewayMessage(blocks)` → durable task with blocks → runner → agent (audio modality degrades honestly to the text bridge for a vision-only chain, transcript still drives the answer) → TTS voice reply → `[voice:<id>]` marker → voice message delivery with marker stripping; (2) image question → `media_input_analyzing` → vision-capable routing → inline base64 image part in the OpenAI payload → streamed answer.
- Text-only flows, mixed text+media messages, image context persistence across turns, capability mismatch errors, and all 243 Batch 1 tests remain green alongside 89 new Batch 2 tests (21 vision + 27 telegram + 39 audio + 2 integration).
- Final gates: `ruff check src/eurus tests` PASS · `ruff format --check src/eurus tests` PASS · `mypy src` PASS (425 files, strict) · `pytest -q` **3756 passed, 1 skipped** (baseline 3667 + 89 new across the batch, zero regressions).
