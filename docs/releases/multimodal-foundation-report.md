# Multimodal Foundation Report — Batch 1 (Phases 0–4)

Report date: 2026-11-07
Repository: `eurus-agent` v0.4.0b1 (stabilized baseline → multimodal foundation)
Scope: Phase 0 (baseline audit), Phase 1 (unified multimodal message system), Phase 2 (multimodal event system), Phase 3 (provider capability registry), Phase 4 (media infrastructure). Foundation only — no vision/audio/video intelligence, no generation, no frontend multimodal work (reserved for Batch 2+).

---

## 1. Baseline audit findings (Phase 0)

Executed gates on the untouched baseline: `ruff check` PASS, `ruff format --check` PASS (682 files), `mypy src` PASS (407 files, strict), `pytest -q` **3424 passed / 1 skipped** after installing the declared dev extras (the one initial failure was an environment gap — missing optional `trafilatura` — not a repository regression). Frontend: `typecheck` PASS, vitest 84 passed; no `lint` script exists.

Key verified findings:

1. **Message model is text-only**: `Message.content: str`; every provider renders it as a plain string; no place to attach media parts.
2. **Capability registry is degenerate**: `providers/capabilities.py` maps every protocol to the same text-only constant; `multimodal` is always `False`; `office/qa/visual.py:vision_supported` was permanently disabled.
3. **Four event mechanisms, no shared envelope**: `AgentEvent` (no identity), `SessionEvent`, durable `RuntimeEvent` (payload_version), `DomainEvent` (correlation/dedup/owner, no schema version, no session/run ids).
4. **Media pipeline was storage-only**: upload + magic-byte sniff + 20 MB cap existed, but attachments were flattened into text markers; no metadata extraction, no dedup, no lifecycle, no resource governance; a latent KeyError-500 bug existed for undetermined ZIP containers; limits were duplicated between `media.py` and `app.py`.
5. **Structural debt**: `RuntimeEvent` name collision, events↔automation layering inversion, sync SQLite writes on the event loop, 4× duplicated text-reset accumulation, hub-local vs durable sequence split.

Full details: `docs/audit/multimodal-baseline.md`, `docs/audit/current-architecture.md`, `docs/audit/technical-debt.md`, `docs/audit/test-baseline.md`.

## 2. Original architecture problems

- Mixed-content messages were structurally impossible (single `content: str`), forcing the text-marker bridge to be the only path.
- The event vocabulary could not express media lifecycles or content-block streaming; domain events had no schema version for future evolution.
- Model routing had zero modality awareness; multimodal requests could never be validated, only silently degraded to text.
- Media processing had no lifecycle, ownership, dedup, governance, or safe failure taxonomy; declared MIME types were trusted over file signatures.
- Limits (20 MB upload in two places, 12k inline chars) were hardcoded and scattered.

## 3. Architecture improvements

1. **Unified content model**: typed, validated, binary-proof content blocks (`src/eurus/content.py`) carried alongside `Message.content` (which remains the canonical text projection — every existing consumer keeps working; blocks auto-project into content when content is empty).
2. **Single event infrastructure preserved and extended**: 7 new streaming `AgentEvent` kinds translated by the engine into durable runtime events; 15 typed multimodal domain event contracts with strict payload validation, schema versions, dedup keys, and owner isolation; `schema_version` added to domain events via migration 16 with legacy-row tolerance.
3. **Typed per-model capability registry** with a conservative declarative catalog, family-prefix resolution, TOML + programmatic overrides, pure routing-planning functions (`plan_routing`, `capable_chain`), and a structured `UnsupportedModalityError`; `Router` now carries a registry and exposes `capabilities_for(ref)`.
4. **Governed media infrastructure**: `eurus.media` package (store, signatures, limits, errors, processing) with lifecycle states, per-session sha256 dedup, ownership stamps, path-containment checks, declared-vs-signature enforcement, centralized `MediaLimits` (env-configurable), and lazy optional-dependency adapters (Pillow/ffprobe/python-docx/openpyxl/python-pptx) under timeout/concurrency/retry governance.
5. **Compatibility layers everywhere**: duck-typed `Agent.run(text)` implementations keep working (blocks kwarg passed only when non-empty); `MessageView`, WS frames, and provider views remain shape-compatible (all additions are additive JSON); `eurus.media` module → package swap preserved every import path.

## 4. Phase 1 — Unified Multimodal Message System (summary)

`Message` gained `blocks: tuple[ContentBlock, ...]` with `TextBlock`, `ImageBlock`, `AudioBlock`, `VideoBlock`, `VideoFrameBlock`, `DocumentBlock`, `ArtifactBlock` — each a frozen dataclass with strict validation (32-hex asset refs, RFC-style MIME tokens, bounded metadata, unknown-field rejection so raw binary can never enter payloads). Serialization is additive (`blocks` key only when present; legacy payloads load unchanged). Blocks flow through the full runtime chain (`Agent.run` → `AgentEngine.run` → `AgentRuntime.run` → `WebRuntime.run_turn` → `GatewayService.send_message`), and the web gateway now constructs validated blocks from resolved attachments while keeping the text bridge for text-only models. Persistence compatibility proven by session-store round-trip and legacy-payload tests (JSON payload column → no DDL migration needed).

## 5. Phase 2 — Multimodal Event System (summary)

Durable domain events (`src/eurus/events/types.py`): `MESSAGE_RECEIVED`, `MEDIA_RECEIVED`, `IMAGE/AUDIO/VIDEO/DOCUMENT_RECEIVED`, and 9 processing-lifecycle types, each with a validated payload contract (asset refs, kinds, stages, sizes, binary rejection) and per-type schema versions. Streaming events: `content_block_started/delta/completed`, `media_input_analyzing`, `audio_output_started/delta`, `artifact` (created/ready/failed) with factories, engine translation into durable runtime events, and additive WS view serialization. `DomainEvent.schema_version` persisted via migration 16 (DEFAULT 1; readers tolerate legacy rows). Gateway emission: uploads publish `MEDIA_RECEIVED` + kind-specific events, sends publish `MESSAGE_RECEIVED` with run correlation — all off-thread, best-effort with explicit logging, deduped (`media-received:{id}`, `message-received:{source}:{id}`), owner-isolated, and side-effect-safe (the web gateway does not run the dispatch loop).

## 6. Phase 3 — Provider Capability Registry (summary)

`ModelCapabilities` (12 typed dimensions) + `CapabilityRegistry` with resolution chain: exact (provider, model) declaration → longest family prefix with segment-boundary matching → provider-level declaration → protocol default → safe text-only default. The declarative catalog covers OpenAI (gpt-4o/4.1/o-series/audio-preview/realtime/image-gen/transcribe/TTS), Anthropic (Claude 3+/4 vision+PDF), Gemini (1.5/2.x full multimodal, imagen, veo), groq (whisper), mistral (pixtral), xai (grok-2-vision/grok-4), openrouter (vendor-prefixed families), and ollama (local vision models) — conservative facts only; undeclared models never gain modalities. Routing decisions are pure, key-free-testable functions producing structured rejections with per-candidate gaps. Configuration: `[providers.<name>.capabilities]` TOML tables (snake/camel, strict validation) injected into every `Router`. `vision_supported` now performs per-model lookup (unblocking office visual QA). API compatibility: `/api/providers` view shape preserved (additive `model_capabilities`); `/api/providers/{name}/discover` gains per-model `capabilities`.

## 7. Phase 4 — Media Infrastructure (summary)

`src/eurus/media/` package: `store.py` (registration with lifecycle `registered→processing→ready/failed`, sha256 per-session dedup, `owner_id` stamps, `get_owned`/`get_for_session` authorization, `metadata_json` column, retention purge, storage-path containment), `signatures.py` (tightened magic-byte table, OOXML profile verification via `[Content_Types].xml` + part prefixes, declared/signature/extension conflict rejection, traversal-safe session components, quicktime/flac support), `limits.py` (`MediaLimits` with `EURUS_MEDIA_*` env configuration and validators for size/duration/pixels/dimension/timeout/concurrency/retries/thumbnail/retention), `errors.py` (17 typed error codes across validation/limit/ownership/processing classes), `processing.py` (`MediaProcessor` with semaphore + `wait_for` timeout + retry budget + structured outcomes; lazy Pillow/ffprobe/office adapters; `run_processing_pipeline` records lifecycle). The web gateway stamps ownership, runs the pipeline on upload, and derives its HTTP upload cap from the same limits. Schema evolution is handled with PRAGMA-checked ALTERs inside the store (legacy DBs migrate transparently — proven by test).

## 8. Files created

Source (10 modules, 2750 lines):
`src/eurus/content.py`, `src/eurus/events/types.py`, `src/eurus/events/multimodal.py`, `src/eurus/providers/model_capabilities.py`, `src/eurus/media/__init__.py`, `src/eurus/media/errors.py`, `src/eurus/media/limits.py`, `src/eurus/media/signatures.py`, `src/eurus/media/store.py`, `src/eurus/media/processing.py`.

Tests (6 files, 2448 lines, 243 tests):
`tests/test_content_blocks.py` (68), `tests/test_message_blocks_integration.py` (11), `tests/test_multimodal_events.py` (41), `tests/test_model_capabilities.py` (60), `tests/test_media_infrastructure.py` (59), `tests/test_multimodal_integration.py` (1 end-to-end) — plus 3 assertions added to `tests/test_web_gateway_media.py`.

Documentation:
`docs/audit/multimodal-baseline.md`, `docs/audit/current-architecture.md`, `docs/audit/technical-debt.md`, `docs/audit/test-baseline.md`, `docs/implementation/multimodal-roadmap.md`, this report.

## 9. Files modified

`src/eurus/models.py` (Message.blocks), `src/eurus/agent/events.py` (kinds/fields/factories), `src/eurus/agent/loop.py` (blocks through the loop), `src/eurus/runtime/engine.py` (blocks + event translation), `src/eurus/runtime/application.py` (blocks through runtime incl. ephemeral path), `src/eurus/events/store.py` + `bus.py` + `__init__.py` (schema_version, exports), `src/eurus/storage/migrations.py` (migration 16), `src/eurus/gateway/web/hub.py` (additive WS fields), `src/eurus/gateway/web/runtime.py` (run_turn blocks), `src/eurus/gateway/web/service.py` (attachment blocks, event emission, processing pipeline, ownership), `src/eurus/gateway/web/app.py` (unified upload limit), `src/eurus/providers/capabilities.py` (registry delegation), `src/eurus/providers/router.py` (registry + capabilities_for), `src/eurus/providers/factory.py` (registry wiring), `src/eurus/catalog.py` (ProviderSpec.capabilities), `src/eurus/config.py` (capabilities tables), `src/eurus/office/qa/visual.py` (per-model vision detection), `tests/test_web_gateway_media.py` (2 tests adapted to the tightened validation).

Deleted: `src/eurus/media.py` (replaced by the `src/eurus/media/` package with identical public API).

## 10. Database migration details

- **Migration 16** (`eurus_schema_migrations`): `ALTER TABLE eurus_domain_events ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1` + index on `(schema_version, event_type)`; `LATEST_SCHEMA_VERSION` 15 → 16. Applied transactionally by the existing `apply_migrations`; legacy rows read back as version 1 (tested).
- **Session messages**: no DDL migration — payloads are a JSON column; the optional `blocks` key is additive and legacy payloads deserialize unchanged (tested both directions).
- **media_files** (store-managed schema, PRAGMA-checked ALTERs): added `owner_id`, `status` (default `ready` for legacy rows), `error_code`, `metadata_json`, `updated_at`, plus a `(session_id, sha256)` dedup index; fresh databases create the full schema directly (legacy-schema migration tested).
- **Run/event stores**: unchanged (new runtime event types are data, not schema).

## 11. Dependency changes

**None.** `pyproject.toml` is untouched. All media processing dependencies (Pillow, python-docx, openpyxl, python-pptx) were already optional extras (`documents`) and are imported lazily inside adapter functions; ffprobe/ffmpeg are discovered at runtime via `shutil.which` and remain entirely optional; no ML models are downloaded at install time. The base installation still requires only the existing core dependencies.

## 12. Security improvements

1. Binary payloads are structurally impossible in message blocks and event payloads (unknown-field rejection + binary scanning; asserted by tests at both layers).
2. Declared MIME types no longer override file signatures: conflicts are rejected (`SIGNATURE_MISMATCH`); fake Office files (ZIP without OOXML profile) and undetermined ZIP containers are rejected with structured errors (also fixes the previous KeyError-500 path).
3. Path traversal blocked at the media store (`safe_session_component`) and containment re-verified on every `storage_path_for` access (tested with an escaping row).
4. Asset ownership: `owner_id` recorded at ingest, `get_owned` denies foreign owners, session scoping enforced on message-send resolution (pre-existing) and via `get_for_session` (new).
5. Resource governance: bounded sizes, durations, resolutions, processing timeouts, concurrency, and retries — configurable via `EURUS_MEDIA_*`, no scattered constants; limit violations produce structured outcomes.
6. Event payloads validated (hex asset ids, constrained stages/kinds, length caps) with dedup keys preventing duplicate fan-out; owner isolation preserved on all domain events.
7. Existing mechanisms preserved: 0600/0700 file modes, parameterized SQL, gateway auth/rate limits, WS auth, redaction in views.

## 13. Test results

| Gate | Baseline | Final |
| --- | --- | --- |
| `ruff check src/eurus tests` | PASS | **PASS** |
| `ruff format --check src/eurus tests` | PASS (682 files) | **PASS** (697 files) |
| `mypy src` (strict) | PASS (407 files) | **PASS** (416 files) |
| `pytest -q` | 3424 passed, 1 skipped | **3667 passed, 1 skipped** (+243 new, 0 regressions) |
| frontend `npm run typecheck` / vitest | PASS / 84 passed | not rerun — **no frontend files changed** in this batch |
| `npm run lint` | N/A (no script exists) | N/A |

Per-phase evidence (recorded in the roadmap): Phase 1 — 104 phase tests + 272 regression tests; Phase 2 — 41 phase tests + 125 event regression tests; Phase 3 — 60 phase tests + 189 provider regression tests; Phase 4 — 59 phase tests + 275 media-adjacent regression tests; integration — 1 end-to-end flow test. Every phase ended with a full-suite run before proceeding; the final suite ran clean twice.

Honest disclosures: the 7th warning in the final run is a third-party `websockets/uvicorn` deprecation emitted by an unrelated Google-OAuth test (pre-existing library state, not repo code); 6 warnings were present at baseline. Two pre-existing gateway-media tests were adapted (not disabled) because their asserted behavior was deliberately tightened by this batch (scattered limit constant → env-configurable `MediaLimits`; declared-type conflict → rejection). All findings, commands, and counts are recorded in `docs/audit/test-baseline.md` and the roadmap.

## 14. Remaining technical debt

- TD-1 `RuntimeEvent` name collision, TD-2 dual `FailureKind`, TD-5 identity-less `AgentEvent`, TD-7 events→automation layering inversion, TD-8 synchronous engine event writes — all preserved deliberately (out of batch scope; documented in `docs/audit/technical-debt.md`).
- TD-18 text-reset accumulation duplicated in four consumers — untouched (streaming contract stability).
- TD-19 `TaskExecutionController`/loop plan-step duplication, TD-20 duplicate frontend media component sets — untouched.
- New, accepted debt: capability catalog facts are declarative data that can drift from provider reality (overridable via TOML; Batch 2 should add discovery-driven refinement); media `purge_expired` is not yet scheduled; processing cancellation abandons the worker thread result (timeout still bounds the outcome).

## 15. Known limitations

- Providers still send text-only wire payloads — the structured blocks and the text bridge coexist until Batch 2 implements multimodal provider rendering; capability routing is decided but not yet enforced inside `Router.stream/chat`.
- Transcription/analysis domain events and the transcription/audio-output streaming events are defined, validated, and tested but not yet emitted (they activate with Batch 2 intelligence).
- REST message views do not expose blocks yet (frontend multimodal experience is a later batch); the frontend continues to render attachments via the existing marker bridge.
- PDF page counting is a regex heuristic; ffprobe-based audio/video metadata requires ffprobe on PATH (returns `decoder: unavailable` otherwise).
- `MESSAGE_RECEIVED` is published only when attachments are present (text-only sends do not emit it — deliberate, to avoid double-notifying existing automation subscriptions).
- Domain event schema versions are all 1 (evolution protocol established; no v2 payload exists yet).

## 16. Recommendations for Batch 2

1. **Vision intelligence**: render `ImageBlock`s/`DocumentBlock`s as provider parts (OpenAI `image_url`/file inputs, Anthropic image blocks, Gemini `inline_data`/Files API) guarded by `plan_routing` at request time inside the Router; emit `media_input_analyzing` + `MEDIA_ANALYSIS_COMPLETED` events; keep the text bridge as the explicit fallback when no capable model exists.
2. **Telegram multimodal gateway**: extend the aiogram adapter to accept photos/voice/video/document messages, ingest them through the new media pipeline, and construct content blocks (the publisher, store, and lifecycle are ready).
3. **Audio intelligence**: wire transcription through the `MEDIA_TRANSCRIPTION_*` event vocabulary and `AudioBlock.transcript`, respecting `MediaLimits` and the existing processing governance.
4. **Frontend multimodal experience**: expose blocks in `MessageView` and render them with the existing `MediaBlocks` components; subscribe to the new `content_block_*` WS kinds in the store; show capability-aware attach hints from `/api/providers` model_capabilities.
5. **Housekeeping**: wire `purge_expired` into the supervisor; move engine event writes to `append_async` (TD-8); converge the two frontend media component sets; consider discovery-driven capability refinement.
6. **Keep the invariants**: no second event bus, no duplicate media store, binary never in payloads, unknown models never assumed multimodal, every schema change additive with legacy tolerance.

## Final verification table

| Area | Status | Test Evidence |
| --- | --- | --- |
| Baseline Audit | VERIFIED | `docs/audit/*` (4 documents); recorded command outputs; 3424-passed baseline suite |
| Unified Message | VERIFIED | `tests/test_message_blocks_integration.py` (11) incl. agent/engine/runtime/session round-trips; full suite green |
| Content Blocks | VERIFIED | `tests/test_content_blocks.py` (68) incl. validation, round-trips, binary rejection, mixed content, legacy compat |
| Event System | VERIFIED | `tests/test_multimodal_events.py` (41) incl. all 15 payload contracts, kinds, emission, correlation, dedup |
| Event Recovery | VERIFIED | `tests/test_event_bus.py` (16 pre-existing) still green + schema-version round-trip, legacy-row, migration-16 tests |
| Capability Registry | VERIFIED | `tests/test_model_capabilities.py` (60) incl. resolution chain, declarations, TOML config, isolation |
| Provider Routing | VERIFIED | `plan_routing`/`capable_chain`/`UnsupportedModalityError` tests (key-free); `tests/test_router.py` + cooldown suites green |
| Media Ingestion | VERIFIED | `tests/test_media_infrastructure.py::TestIngestion` (registration, dedup, owner, traversal) + gateway upload tests |
| Media Validation | VERIFIED | `TestSignatureValidation` (magic bytes, OOXML profiles, conflicts, empty) + gateway mismatch test |
| Media Storage | VERIFIED | `TestStorageIntegration` (legacy migration, retention, containment, delete) + `test_release_hardening.py` (file modes) green |
| Resource Limits | VERIFIED | `TestResourceGovernance` + `TestProcessingLifecycle` (timeout, concurrency, retry, structured failures) |
| Backward Compatibility | VERIFIED | legacy payload tests, duck-typed agent runs, view-shape tests, 3667-passed full suite (0 regressions vs 3424 baseline) |
| Full Test Suite | VERIFIED | `pytest -q`: **3667 passed, 1 skipped**; `ruff check` PASS; `ruff format --check` PASS; `mypy src` PASS |

## Final assessment

**The multimodal foundation is implemented, integrated, and verified — this is a foundation release, not a feature release.** EURUS can now represent, validate, persist, event, route, and govern mixed-content conversations end-to-end, with every existing text-only behavior preserved and the full original suite green alongside 243 new tests. Vision, audio, and video intelligence remain unimplemented by design: the extension points (content blocks, event vocabulary, capability routing, processing adapters) are in place so Batch 2 can add them without another architectural redesign. No claim of multimodal *reasoning* readiness is made — only of foundation readiness.
