# Multimodal Expansion — Batch 3 Report (Phases 8–12)

Date: 2026-11-08
Repository: kyyinfinite/eurus (delivered as eurus.zip, Batch 2 state + Batch 3 changes)

## 1. Initial Audit Results

Full audit reports: `docs/audit/batch-3-baseline.md`, `batch-3-architecture.md`, `batch-3-technical-debt.md`, `batch-3-test-baseline.md`; roadmap: `docs/implementation/batch-3-roadmap.md`.

- Batch 1 (unified Message/content blocks, event system, capability registry, media infrastructure) — VERIFIED REAL, in active runtime use.
- Batch 2 (vision intelligence, Telegram multimodal gateway, STT/TTS) — VERIFIED REAL end-to-end.
- Baseline before Batch 3: ruff clean, ruff format clean, mypy strict clean (425 files), 3756 tests passed; frontend typecheck/84 tests/build green.
- Absent or vocabulary-only at baseline: video frame extraction, thumbnails, image/video generation endpoints and tools, frontend block rendering, multimodal memory, realtime voice transport, 5 multimodal event types with no emitters, `VideoFrameBlock` never constructed, `video/quicktime` rejected by the store (MOV bug).

## 2. Batch 1 / Batch 2 Condition

Both batches were verified as genuinely implemented and green; Batch 3 built on top of them without duplicating any subsystem (no new AgentEngine, MediaStore, EventBus, provider registry, memory, or artifact store).

## 3. Technical Debt Findings

Tracked in `docs/audit/batch-3-technical-debt.md`. Blockers fixed during Batch 3: dead `src/eurus/media.py` (deleted), dead `frontend/src/components/chat/media.tsx` (deleted), MOV/quicktime store rejection (fixed), `VideoFrameBlock`/`VIDEO_FRAME_EXTRACTED` never used (now produced by Phase 8), 3 of 5 orphan event types now emitted, `ToolContext.artifacts` unwired in production (wired in engine `_bind_run_context`), web `[voice:]` markers unresolved (frontend resolves all markers). Deferred items (TD-9 … TD-18) remain documented.

## 4. Features Delivered (Phases 8–12)

### Phase 8 — Video Intelligence
- `src/eurus/media/video.py`: optional ffmpeg engine — metadata (duration/resolution/fps/codec/audio-track detection), thumbnail generation, scene-change detection (bounded to ≤900s videos), adaptive keyframe sampling (scene-prioritized, interval+max-frame caps, near-duplicate suppression, timestamp preservation), frame extraction with resolution caps, audio track extraction (16kHz mono WAV, size-capped with truncation flag).
- Integrated into `run_processing_pipeline` (thumbnail + frames + audio assets saved via MediaStore; `MEDIA_PROCESSING_STARTED`, `VIDEO_FRAME_EXTRACTED`, `MEDIA_ANALYSIS_COMPLETED` now emitted); video STT via `transcribe_video_audio` (uses extracted track or lazy extraction), transcript stored on the video asset and surfaced in `VideoBlock.transcript` + text bridge.
- Provider routing: Gemini renders native video inline (`GEMINI_VIDEO_MIMES`, 10MB inline cap, video wire modality); OpenAI/Anthropic render extracted keyframes as labeled images (`[keyframe at 12.3s]`); `required_modalities` treats a framed video as image-satisfiable, so vision models serve video messages via frames; providers without video or frames degrade to the text bridge. Telegram "video analysis unavailable" note now only appears when keyframe extraction actually failed.
- Resource governance: `EURUS_MEDIA_MAX_VIDEO_FRAMES`, `EURUS_VIDEO_FRAME_INTERVAL_SECONDS`, `EURUS_VIDEO_FRAME_MAX_DIMENSION`, `EURUS_VIDEO_SCENE_THRESHOLD`; safe subprocess invocation (argument lists, per-call timeouts, governance semaphore, temp cleanup); enrichment failures never fail ingestion.

### Phase 9 — Image & Video Generation
- `src/eurus/generation/` (new package): `config.py` (`[generation]` TOML + 9 env overrides), `clients.py` (OpenAI-compatible `/images/generations` + `/images/edits` including b64/url result handling and dall-e `response_format`; Gemini imagen `:predict`; Gemini Veo `predictLongRunning` job lifecycle — submit/poll/download/cancel with operation parsing), `tools.py` (`generate_image`, `edit_image`, `generate_video` agent tools with validated schemas, capability-gated builders, timeouts, bounded outputs, artifact publication, `media.generation.started/ready/failed` events, marker returns `[image:<id>]`/`[video:<id>]`, `GenerationJobTracker` duplicate-job prevention, `DeliverySlot` for background completion delivery).
- HttpxTransport gained hardened `get_json`/`get_bytes`; FakeTransport extended for tests.
- `context.artifacts` wired in `AgentEngine._bind_run_context` (ArtifactProvider from home) — generation outputs are also durable artifacts.
- Registered on Telegram (with chat delivery callback for background video completion) and Web runtimes; Telegram marker delivery generalized to `deliver_media_responses` (voice/image/video).
- Video generation is asynchronous: bounded in-tool wait, background completion task, events + artifact + gateway delivery when ready.

### Phase 10 — Frontend Multimodal Experience
- Backend: `MessageView.blocks` exposed in REST (additive `{type, data}` views).
- Rendering pipeline is now blocks-first (`MessageRenderer` + `groupContentBlocks`): native Text/Image/Audio/Video(+keyframe strip with timestamp jump navigation, transcript disclosure, duration)/Document/Artifact blocks; legacy `[attach ...]` marker fallback retained; `[image:]`/`[video:]`/`[voice:]` markers in assistant answers resolve to real media via `/api/media/{id}` metadata.
- Artifact cards (fetch `/api/artifacts/{id}`, download via content route, type-aware labels); live-turn multimodal activity (thinking timeline describes media analysis, generation, voice, artifact events; artifact cards render during runs); optimistic user messages carry attachment blocks; `.mov` uploads accepted; dead `chat/media.tsx` deleted.
- `lib/types.ts` extended (block types, new agent event kinds with block/asset/artifact fields), `lib/media.ts` (generated markers, media metadata cache), `lib/thinking.ts` (multimodal activity), `lib/chat.ts` (attachment blocks).

### Phase 11 — Multimodal Memory
- `src/eurus/memory/media_index.py`: `MediaMemoryIndexer` — derives searchable text from transcripts, video summaries with keyframe timestamps, inline document text, metadata; redacts secrets, rejects emails; provenance carries asset id/kind/sha256/session; `source=media:<asset_id>` gives dedup + re-index (upsert) semantics; `forget_media` cleanup; `search_media_memories` verifies asset availability and enforces owner isolation; `recall_media` agent tool returns delivery markers so found media can be re-sent to the user.
- `SessionStore` gained `find_memories_by_source` / `delete_memories_by_source`.
- Wired: Telegram + Web ingest index media after processing/STT (config-gated); Web `DELETE /api/media/{id}` also forgets its memories.

### Phase 12 — Realtime Voice Interaction
- `src/eurus/audio/realtime_openai.py`: `OpenAIRealtimeSession/Transport` over `websockets` (optional dep) — session.update (VAD, pcm16, voice, instructions, tools), audio append, interrupt (response.cancel + buffer clear), reconnection, tool-result injection, JSON event stream; `build_realtime_transport` capability-gated on `realtime_audio`.
- `src/eurus/gateway/web/realtime.py`: `RealtimeBridge` — bridge session minting (60s TTL, owner-bound), `/api/realtime/stream/{bridge_id}` WS relay (PCM16 in, forwarded transcripts/audio deltas/tool events out), duration deadline enforcement, concurrent-session limit, expiry eviction, cleanup on disconnect; tool calls execute read-only registry tools with the same ToolContext semantics — mutating/approval tools are honestly refused in voice mode. The provider API key stays server-side; the browser only receives a bridge id.
- Routes: `GET /api/realtime/status`, `POST /api/realtime/session`, `WS /api/realtime/stream/{bridge_id}` (gateway-token authenticated).
- Frontend: `VoiceConsole` (mic capture → PCM16 frames over WS, WebAudio playback of PCM deltas, live transcripts, barge-in, hang-up) + composer-area voice toggle gated on realtime status.
- Config: `audio.realtime_model/realtime_voice/realtime_session_seconds/realtime_timeout_seconds` (+ env overrides).

## 5. Actual Architecture

Unified chain preserved: Gateway → GatewayMessage → content blocks → Media Infrastructure (MediaStore/MediaProcessor/video enrichment) → Event System → Capability Registry → provider adapters (wire modality aware) → AgentEngine (single engine; generation/realtime tools ride the same ToolRegistry/ToolContext) → Memory (media index) → Artifact System → gateway delivery (marker resolution, background delivery, realtime bridge). No modality uses a separate engine.

## 6. New Files

- src: `eurus/media/video.py`, `eurus/generation/{__init__,config,clients,tools}.py`, `eurus/memory/media_index.py`, `eurus/audio/realtime_openai.py`, `eurus/gateway/web/realtime.py`
- frontend: `src/components/chat/VoiceConsole.tsx`, `src/components/chat/MessageRenderer.test.tsx`
- tests: `test_video_intelligence.py`, `test_generation_tools.py`, `test_multimodal_memory.py`, `test_realtime_voice.py`
- docs: 4 batch-3 audit reports + roadmap + this report

## 7. Modified Files (principal)

`media/{processing,limits,signatures,store}.py`, `content.py`, `providers/{media_parts,model_capabilities,gemini,genai_sdk,httpx_transport,testing}.py`, `audio/{config,pipeline}.py`, `runtime/engine.py`, `gateway/telegram/{media,adapter}.py`, `gateway/web/{schemas,service,runtime,app}.py`, `cli/gateway.py`, `sessions.py`, `config.py`; frontend `lib/{types,media,chat,thinking}.ts`, `components/chat/{MessageRenderer,ConversationView}.tsx`, `components/media/MediaBlocks.tsx`; `eurus.example.toml`.

## 8. Migrations / Dependencies / Configuration

- No schema migrations required (video enrichment uses `media_files.metadata_json`; media memory uses the existing `memories` table with the new `source` convention; REST `blocks` is additive).
- No new required dependencies; `websockets>=12` (already an optional extra) is used for realtime.
- New env/TOML knobs: video frame governance (4), generation (9), realtime (4); see `eurus.example.toml`.

## 9. Providers and Models Actually Supported

- Native video input: Gemini 1.5/2.0/2.5 family (`video_input` capability) — mp4/webm/quicktime/matroska/3gpp inline ≤10MB.
- Frame-based video: any vision model (OpenAI gpt-4o/4.1, Claude 3/4, Groq/OpenRouter/Ollama vision models).
- Image generation: OpenAI-compatible gpt-image-1 / dall-e-3 / dall-e-2 (generation + edit), Gemini imagen (`:predict`).
- Video generation: Gemini Veo via `predictLongRunning` (async job).
- Realtime voice: OpenAI realtime models (`gpt-4o-realtime*`, capability-gated).

## 10. Resource Limits and Security

Video duration/size/resolution/frame caps + processing timeouts/concurrency/retries; audio extraction size cap with truncation flag; generation prompt/output bounds, per-request output cap, job wait/timeout/poll knobs, duplicate-job prevention; realtime session duration/concurrency/TTL limits, read-only tool policy, server-only provider keys, gateway-token WS auth, owner-scoped bridge sessions; memory redaction + email rejection + owner isolation; artifact publication bounded by store limits.

## 11. Tests Executed (actual results)

Python: `ruff check src/eurus tests` PASS · `ruff format --check` PASS (711 files) · `mypy src` PASS (433 files, strict) · `pytest -q` **3897 passed, 1 skipped** (baseline 3756; +141 tests across video/generation/memory/realtime/integration). Frontend: `tsc --noEmit` PASS · Vitest **102 passed** (14 files) · `vite build` PASS. ffmpeg present in the test environment; ffmpeg-dependent tests skip gracefully where absent.

## 12. Known Limitations

- Gemini native video limited to inline bytes ≤10MB; longer videos use keyframes + transcript.
- Imagen does not support edit; `edit_image` requires an OpenAI-compatible image model.
- Realtime voice: OpenAI Realtime only; web frontend only (Telegram realtime not implemented); mutating/approval tools are refused in voice mode by design; browser capture uses ScriptProcessor (deprecated but broadly supported).
- Media memory captions/OCR are indexed only when derived text exists (`index_media_caption` API available; not auto-invoked by the vision path).
- Background video generation delivery is wired for Telegram (chat delivery) and artifact/events on web; web in-chat push of late-completing videos relies on run events + artifacts API.
- TD-9…TD-18 deferred debt items remain (dormant embedding provider, per-turn image re-upload cost, HEIC, OpenAI PDF wire gap, save_messages rewrite pattern, no ESLint config, chunk-size warning).

## 13. Deferred Work

Realtime voice for Telegram; Gemini Files API for large media (removes inline caps); streaming STT deltas (`MEDIA_TRANSCRIPTION_DELTA` remains vocabulary-only); automatic vision-caption ingestion into media memory; client-side artifact pane revival beyond ArtifactBlockCard; Batch 4 items per roadmap.

Batch 3 is complete. STOP as instructed — no Batch 4 work without new instructions.
