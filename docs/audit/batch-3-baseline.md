# Batch 3 Baseline Audit

Date: 2026-11-08
Scope: Full repository inventory, Batch 1/2 verification, baseline quality gates.
Method: Direct code inspection of every multimodal subsystem, full test suite execution, frontend build verification.

## 1. Repository Inventory

| Area | Location | State |
| --- | --- | --- |
| AgentEngine / agent loop | `src/eurus/agent/loop.py` (`Agent`, 1471 lines), `runtime/engine.py` (`AgentEngine`) | Real, single engine for all modalities |
| Runtime / orchestration | `src/eurus/runtime/`, `orchestration/` | Real |
| Provider adapters | `providers/`: `openai_compat.py`, `anthropic.py`, `gemini.py`, `genai_sdk.py`, `groq_sdk.py`, `httpx_transport.py` | Real HTTP/SDK transports |
| Capability registry | `providers/model_capabilities.py` (~66 model declarations), `capabilities.py` | Real, per-model |
| Content Blocks / Message | `content.py` (7 block types), `models.py` (`Message.blocks`) | Real |
| MediaStore / MediaProcessor | `media/store.py`, `media/processing.py`, `media/limits.py`, `media/signatures.py` | Real, SQLite `media_files` |
| Event system | `events/`: `bus.py`, `store.py`, `types.py`, `multimodal.py` | Real, 16 multimodal event types |
| Artifact system | `multiagent/artifacts.py` (`ArtifactStore`), `tools/artifacts.py` | Real store; tool-facing helper unwired in production |
| Telegram gateway | `gateway/telegram/`: `media.py` (ingest + sender), `adapter.py`, `approval.py` | Real |
| Web gateway | `gateway/web/`: `app.py` (~60 routes), `service.py`, `hub.py`, `runtime.py` | Real |
| Audio subsystem | `audio/`: `stt.py`, `tts.py`, `pipeline.py`, `speak_tool.py`, `realtime.py` | STT/TTS real; realtime = protocols only |
| Memory subsystem | `memory/manager.py` (843 lines), `lexical.py`, `embeddings.py`, `rerank.py` | Real; FTS5 only in production |
| Frontend | `frontend/`: React 18 + TS 5.7 + Vite 6 + Tailwind 3.4 + Zustand 5 | Real; marker-based rendering |
| DB models / migrations | `sessions.py` (sessions/messages/memories + FTS5), `storage/migrations.py` (runtime/multiagent/artifacts tables, 15 migrations) | Real |
| Config / deps | `pyproject.toml`, `config.py`, `eurus.example.toml`, `.env.example` | Real |
| Integration tests | 273 test files, 3757 tests | Green |

Dead code found: `src/eurus/media.py` (top-level) is shadowed by the `media/` package and unreachable at runtime. Frontend `src/components/chat/media.tsx` (405 lines) is an unimported near-duplicate of `media/MediaBlocks.tsx`.

## 2. Batch 1 Verification

| Claim | Verdict | Evidence |
| --- | --- | --- |
| Unified multimodal Message system | VERIFIED | `Message.blocks` on user/assistant roles, `text_projection()` text bridge, `to_dict()` omits empty blocks (backward compatible), persisted in `messages.payload` |
| Multimodal event system | VERIFIED | 16 event types with payload validators in `events/types.py`; `MultimodalEventPublisher`; engine translation in `runtime/engine.py` |
| Provider capability registry | VERIFIED | Per-model `ModelCapabilities` (12 booleans), family-prefix matching, TOML overrides, `plan_routing()` gating |
| Media infrastructure | VERIFIED | `MediaStore.save/get/delete/purge_expired`, magic-byte sniffing (~40 MIMEs), sha256 dedup, owner isolation, 0600 files, traversal protection, `MediaLimits` (9 env-tunable knobs) |

Gaps within Batch 1 scope: 5 event types (`MEDIA_VALIDATION_*`, `MEDIA_PROCESSING_STARTED`, `MEDIA_TRANSCRIPTION_DELTA`, `VIDEO_FRAME_EXTRACTED`, `MEDIA_ANALYSIS_COMPLETED`) have vocabulary and validators but zero emitters. `VideoFrameBlock` is never constructed anywhere in `src/`.

## 3. Batch 2 Verification

| Claim | Verdict | Evidence |
| --- | --- | --- |
| Vision intelligence | VERIFIED | `media_parts.py` renders image blocks to OpenAI `image_url`, Anthropic `image` source, Gemini `inline_data`; router filters candidates by capability ∧ wire modality; text-bridge fallback |
| Telegram incoming media | VERIFIED | photo/voice/audio/video/video_note/document, ≤8 per message, pre-download size check, 90s timeout, album collector, STT for audio |
| Telegram outgoing media | VERIFIED | `TelegramMediaSender.send_bytes` dispatches photo/voice/video/audio/document, 50MB cap, retry on network/429; `[voice:<id>]` marker resolution |
| Audio processing | VERIFIED | ffprobe metadata, transcript stored in media metadata, inlined into message text bridge |
| STT | VERIFIED | `OpenAICompatTranscriber` real multipart POST `/audio/transcriptions`, capability-gated |
| TTS | VERIFIED | `OpenAICompatSpeechSynthesizer` real POST `/audio/speech`, capability-gated, `speak` tool on Telegram gateway |
| Realtime voice abstractions | VERIFIED as abstraction | `audio/realtime.py` defines `RealtimeVoiceSession/Transport/Stream` protocols; no transport implemented (explicitly deferred to Batch 3) |

Web gateway STT works; `speak` tool is Telegram-only (web runtime never registers it). `[voice:]` markers surface as literal text on web.

## 4. Baseline Test Results (executed 2026-11-08)

Python (Python 3.12.16, repo venv):

- `ruff check src/eurus tests` — PASS
- `ruff format --check src/eurus tests` — PASS (700 files)
- `mypy src` — PASS (425 files, strict)
- `pytest -q` — **3756 passed, 1 skipped**, 361s

Frontend (node v24.22.2, npm 11.19.1):

- `npm ci` — 529 packages
- `npm run typecheck` — PASS
- `npm run test` — 13 files, **84 tests passed**
- `npm run build` — PASS (36s; pre-existing chunk-size warning, non-blocking)
- No lint script exists (no ESLint config in repo — documented as N/A in roadmap)

Environment: ffmpeg 7.1.5 and ffprobe available at `/usr/bin` (needed for Phase 8).

All baseline failures: none. Categories: no existing failures, no regressions, no environment failures. Test debt is limited to missing coverage for the new Batch 3 features.

## 5. Verdict

Batch 1 and Batch 2 are genuinely implemented, tested, and green. The Batch 3 surface (video intelligence, image/video generation, frontend block rendering, multimodal memory, realtime voice) is absent or vocabulary-only, exactly as `docs/known-limitations.md` states. No architectural blockers; implementation can proceed per the roadmap.
