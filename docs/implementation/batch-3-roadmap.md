# Batch 3 Roadmap — Phases 8–12

Date: 2026-11-08
Basis: `docs/audit/batch-3-baseline.md`, `docs/audit/batch-3-architecture.md`, `docs/audit/batch-3-technical-debt.md`, `docs/audit/batch-3-test-baseline.md`.
Baseline state: all quality gates green (3756 Python tests, 84 frontend tests, mypy strict, ruff clean, build passing). Batch 1/2 verified real.

## Phase 8 — Video Intelligence

1. Remove dead `src/eurus/media.py` (shadowed by `media/` package).
2. `media/video.py`: ffmpeg/ffprobe-based video processing — metadata (duration, resolution, fps, audio track detection), thumbnail generation (respecting `MAX_THUMBNAIL_DIMENSION`), keyframe extraction with adaptive sampling (interval, max frames, dedup, timestamp mapping, resolution cap), audio track extraction. Optional dependency detection with graceful degradation (mirrors ffprobe pattern). Safe subprocess invocation (argument lists, timeouts, temp file cleanup).
3. Integrate into `media/processing.py` pipeline: video kind → metadata + thumbnail + optional frames; emit `MEDIA_PROCESSING_STARTED`, `VIDEO_FRAME_EXTRACTED`, `MEDIA_ANALYSIS_COMPLETED`.
4. `providers/media_parts.py`: extend Gemini wire to accept video blocks (native inline video for capable models) + frame-based fallback (VideoFrameBlocks as images) + audio transcript text bridge for all protocols. Per-model capability check (`video_input`).
5. Video understanding flow: agent sees VideoBlock + frames + transcript; answers timestamp questions; scene/segment reasoning.
6. Resource governance: duration/size/resolution/frame caps in `MediaLimits`; processing timeout, concurrency, cancellation, retry; temp cleanup.
7. Tests: new `test_video_intelligence.py` (metadata, frames, timestamps, sampling, corrupt/oversize/missing-ffmpeg/missing-audio/codec, capability mismatch, cancellation, limits, Telegram/web integration).

## Phase 9 — Image & Video Generation

1. `providers/generation.py`: capability-gated generation clients — image generation via OpenAI-compatible `/images/generations` (gpt-image-1/dall-e-3) and Gemini `imagen`; image edit via `/images/edits`; video generation as async job lifecycle (submit → poll → retrieve) for capable providers (veo via Gemini). Real HTTP via existing `HttpxTransport`.
2. Agent tools `generate_image`, `edit_image`, `generate_video` following the speak pattern: factory-injected MediaStore + synthesizer, validated input schema, capability check, budget/cost caps, timeout, error handling, output saved via MediaStore, marker return (`[image:<id>]`, `[video:<id>]`), events emitted (`artifact.created/ready/failed` style via event_sink).
3. Wire `context.artifacts` in `AgentEngine._bind_run_context` (TD-7); generation outputs also published to ArtifactStore for durable delivery.
4. Gateway delivery: extend Telegram marker resolution (`[image:]`, `[video:]`) alongside `[voice:]`; web runtime registers the tools; web frontend resolves markers (Phase 10).
5. Video generation job runner: background polling task that does not block the agent loop; job dedup; cancellation; status events.
6. Tests: new `test_generation_tools.py` (request shapes, capability gating, unsupported provider, job lifecycle, polling, cancellation, timeout, cost limits, artifact storage, duplicate prevention, marker delivery).

## Phase 10 — Frontend Multimodal Experience

1. Backend pre-req: expose optional `blocks` in `MessageView` + `_message_views` (additive).
2. `lib/types.ts`: block types + extended agent event kinds; render pipeline blocks-first with marker fallback.
3. `MessageRenderer`: native block rendering (Text/Image/Audio/Video/VideoFrame/Document/Artifact) reusing existing MediaBlock components; `[voice:]/[image:]/[video:]` marker resolution; fullscreen viewer already exists.
4. Video experience: inline player, poster, duration, timestamp navigation, extracted frame strip.
5. Artifact experience: revive dormant ArtifactCard/ArtifactPane for server artifacts bound to `/api/artifacts/{id}/content`.
6. Composer: already strong — add attachment segments to optimistic message; accept .mov (quicktime sniffing exists server-side).
7. Streaming UX: handle `content_block_*`, `media_input_analyzing`, `audio_output_*`, `artifact` events in LiveTurnCard (progress chips, media placeholders, artifact cards).
8. Cleanup: delete dead `chat/media.tsx`.
9. Tests: component tests for block renderer, marker fallback, artifact rendering, composer, streaming events; typecheck+build green.

## Phase 11 — Multimodal Memory

1. `memory/media_index.py`: index derived media text (image captions/OCR via vision model on ingest, audio transcripts, video summaries with timestamps, document text excerpts, artifact metadata) into `memories` with provenance (asset_id, session_id, media kind) — never binary data.
2. Ingestion hooks: after media processing/analysis completes (pipeline + analysis events), index derived text with `remember_untrusted`-style trust model; owner-scoped.
3. Retrieval: existing `recall`/`search_memories` works on indexed text; asset references returned so the agent can re-open media (blocks reconstructable from `MediaStore` metadata); provenance displayed.
4. Lifecycle: media deletion → memory cleanup (asset reference invalidation); retention policy alignment with media retention; re-indexing on transcript updates; dedup via existing consolidate.
5. Tests: new `test_multimodal_memory.py` (indexing, per-modality retrieval, dedup, re-index, deleted asset handling, owner isolation, retention, provenance, failure recovery).

## Phase 12 — Realtime Voice Interaction

1. `audio/realtime/`: OpenAI Realtime transport first (`websocket` via existing httpx/websockets infrastructure — check dependency weight; expected `websockets` already optional for browser-cdp).
2. Session manager: create/auth (ephemeral tokens via server-side session creation endpoint — never expose server API key), audio in/out streaming, lifecycle, reconnection, timeout, cancellation, resource cleanup, concurrent session + rate limits.
3. Agent integration: realtime session drives the same AgentEngine (tool calling, approval, memory, conversation context); tool results spoken back; interruption-safe task state.
4. Web gateway: WebSocket bridge endpoint `/api/realtime/session` (token mint) + media streaming relay; frontend voice mode toggle in Phase 10 UI conventions.
5. Tests: new `test_realtime_voice.py` (session creation, auth, audio frames, streaming events, interruption, reconnection, tool calling, approval, timeout, owner isolation, provider failure, cleanup) with a mock realtime server.

## Quality gates (per phase)

Python: `ruff check`, `ruff format --check`, `mypy src`, `pytest -q`. Frontend (Phases 10, 12): `typecheck`, `test`, `build`.

## Execution order

Audit (done) → Phase 8 → gate → Phase 9 → gate → Phase 10 → gate → Phase 11 → gate → Phase 12 → gate → final integration + regression → `docs/releases/multimodal-expansion-batch-3-report.md`.
