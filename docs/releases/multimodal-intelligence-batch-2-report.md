# Multimodal Intelligence Report — Batch 2 (Phases 5–7)

Report date: 2026-11-07
Repository: `eurus-agent` v0.4.0b1 (multimodal foundation → multimodal intelligence)
Scope: Phase 5 (vision intelligence), Phase 6 (Telegram multimodal gateway), Phase 7 (audio & voice intelligence). Batch 1 (Phases 0–4) was audited and verified before any Batch 2 work started.

---

## 1. Implemented features

1. **Vision intelligence (Phase 5)** — image input reaches vision-capable models as native provider parts: OpenAI `image_url` data URLs, Anthropic image + base64 PDF document blocks, Gemini `inline_data` parts (HTTP and google-genai SDK protocols, plus the Groq SDK which inherits the OpenAI wire). Capability routing is now enforced inside `Router.chat/stream` (per-model registry capabilities intersected with per-protocol wire modalities); text-only models never receive image payloads, and requests with no capable model degrade explicitly to the Batch 1 text bridge with a warning. Multiple images, mixed text+image messages, cross-turn image context, GIFs (OpenAI/Anthropic), HEIC/HEIF (Gemini), and PDF documents (Anthropic/Gemini) all work; the agent emits `media_input_analyzing` for incoming media.
2. **Telegram multimodal gateway (Phase 6)** — photos, image documents, voice notes, audio files, videos, video notes, and documents are downloaded asynchronously, validated, stored in the single MediaStore, and submitted as `GatewayMessage.blocks`; albums (media groups) are debounced into one message. Blocks persist in the durable task state, so the runner feeds them to the same AgentEngine. Outgoing media uses the proper Bot API method per MIME (photo/voice/audio/video/document) with the 50 MB cap, careful retry (network/429 only — no accidental double sends), and automatic delivery of run artifacts after each task.
3. **Audio & voice intelligence (Phase 7)** — `[audio]` configuration with an OpenAI-compatible STT client (multipart `/audio/transcriptions`) and TTS client (binary `/audio/speech`). Transcription runs at ingestion for Telegram voice notes and web uploads; transcripts are stored as media metadata, copied onto `AudioBlock.transcript`, and inlined into the text bridge so text-only chains can answer voice messages. The `speak` tool turns text into stored speech assets and returns a `[voice:<asset_id>]` marker that the Telegram runner resolves into a real voice message (OGG/Opus) or audio file, stripping the marker from the delivered text. A realtime-voice abstraction (streams, session lifecycle, interruption, reconnection) is defined but deliberately not implemented.

## 2. Actual architecture and flow

```
Telegram Update / Web upload
  → gateway adapter (download, size pre-check, timeout)
  → MediaStore.save (signature sniff, dedup, owner stamp) → processing pipeline
  → [audio] STT at ingestion (transcript → metadata → AudioBlock.transcript → text bridge)
  → MEDIA_RECEIVED + kind events, MESSAGE_RECEIVED with correlation
  → GatewayMessage(blocks) → GatewayRouter → BackgroundTaskManager (blocks in task state)
  → runner → AgentRuntime.run(blocks) → AgentEngine → Agent loop
  → Router._routed_candidates (capability + wire-modality filter)
  → provider payload builders resolve assets via MediaStoreAssetResolver
    (OpenAI image_url / Anthropic image+document / Gemini inline_data)
  → streamed answer (+ [voice:<id>] marker when the speak tool ran)
  → Telegram runner wrapper: artifact delivery, voice-marker resolution,
    send_voice/send_audio, marker stripping → final text answer
```

Design invariants kept: one AgentEngine, one MediaStore, one event system, one capability registry, one gateway message contract; no path bypasses the agent loop; binary never enters message JSON or event payloads (base64 exists only inside provider HTTP requests).

## 3. Files created

Source (8 modules):
`src/eurus/providers/media_parts.py`, `src/eurus/audio/__init__.py`, `src/eurus/audio/config.py`, `src/eurus/audio/stt.py`, `src/eurus/audio/tts.py`, `src/eurus/audio/pipeline.py`, `src/eurus/audio/speak_tool.py`, `src/eurus/audio/realtime.py`, `src/eurus/gateway/telegram/media.py`.

Tests (4 files, 89 tests):
`tests/test_vision_intelligence.py` (21), `tests/test_telegram_multimodal.py` (27), `tests/test_audio_intelligence.py` (39), `tests/test_multimodal_intelligence_integration.py` (2).

Documentation:
`docs/architecture/multimodal-intelligence.md`, this report.

## 4. Files modified

- `src/eurus/providers/base.py` — `Provider` accepts an optional `asset_resolver`.
- `src/eurus/providers/openai_compat.py` — `message_to_wire` renders `image_url` parts; resolver-aware `_payload`/`_call`/`_stream`.
- `src/eurus/providers/anthropic.py` — image and base64 PDF document blocks in `convert_messages`.
- `src/eurus/providers/gemini.py` — `inline_data` parts in `convert_messages`/`build_payload`.
- `src/eurus/providers/genai_sdk.py` — shares the Gemini conversion; SDK protocol gains vision.
- `src/eurus/providers/groq_sdk.py` — forwards the resolver (inherits OpenAI wire rendering).
- `src/eurus/providers/router.py` — `_routed_candidates` enforces capabilities + wire modalities; text-bridge fallback strips blocks.
- `src/eurus/providers/model_capabilities.py` — `required_modalities_for_messages` (duck-type tolerant).
- `src/eurus/providers/factory.py` — `build_router` injects a lazy `MediaStoreAssetResolver`.
- `src/eurus/providers/httpx_transport.py` — additive `post_multipart` + `post_binary` (same UA rotation and error mapping).
- `src/eurus/agent/loop.py` — `media_input_analyzing` events; ToolContext session stamping.
- `src/eurus/gateway/contracts.py` — `GatewayMessage.blocks` (additive, default empty).
- `src/eurus/gateway/router.py` — media-only messages accepted; blocks forwarded on submit.
- `src/eurus/gateway/manager.py` — `submit(blocks=...)` persists block dicts in the durable task state.
- `src/eurus/gateway/telegram/adapter.py` — media handlers, ingestion wiring, event publication, media-group buffering, artifact + voice delivery runner wrapper.
- `src/eurus/cli/gateway.py` — runner passes blocks to `runtime.run`; audio clients built and closed; `speak` tool registered when TTS is configured.
- `src/eurus/gateway/web/service.py` — audio uploads transcribed through the same pipeline.
- `src/eurus/config.py` — `[audio]` section + `EURUS_STT_*`/`EURUS_TTS_*` env overrides.
- `src/eurus/media/signatures.py` — flac/aac allowed; M4A-family and declared audio/webm stay in the audio kind.
- `src/eurus/media/store.py` — attachment blocks carry transcript + duration; text bridge inlines transcripts.
- `docs/configuration-reference.md`, `docs/known-limitations.md`, `docs/implementation/multimodal-roadmap.md`, `eurus.example.toml`.

## 5. Database migrations

**None required.** Durable task blocks ride the existing `state_json` column (additive key, legacy rows unaffected); media transcripts ride the Batch 1 `metadata_json` column; audio events are rows in the existing `eurus_domain_events` table (new event types are data, not schema). Verified by the full suite, including the migration-safety and legacy-payload tests from Batch 1.

## 6. Dependencies

**None added.** `pyproject.toml` is untouched. STT/TTS use the existing `httpx` transport; Telegram media uses the existing optional `aiogram` extra; audio metadata still uses the optional Pillow/ffprobe adapters. No AI models are downloaded automatically; every media library stays optional and lazily imported.

## 7. Supported providers and models

| Capability | Providers (protocol) | Declared example models |
| --- | --- | --- |
| Vision input (images) | openai, groq/openrouter/ollama/mistral/xai via openai protocol, anthropic, gemini (HTTP + genai-sdk) | gpt-4o/4.1/o-series, claude-3/sonnet-4/opus-4/haiku-4, gemini-1.5/2.x, pixtral, grok-2-vision/grok-4, llama-3.2-vision, qwen-vl, llava, minicpm-v, gemma3 |
| PDF document input | anthropic, gemini | claude vision family, gemini family (OpenAI chat-completions cannot render PDFs — routed around) |
| Speech-to-text | openai protocol only | openai/whisper-1, openai/gpt-4o-transcribe(-mini), groq/whisper-large-v3 |
| Text-to-speech | openai protocol only | openai/tts-1, tts-1-hd, gpt-4o-mini-tts (mp3/opus/aac/flac/wav; opus = Telegram voice) |
| Audio input to chat models | gemini family declares it; this batch routes audio through STT transcripts for all chains | — |

Undeclared models remain conservatively text-only; `[providers.<name>.capabilities]` TOML overrides still apply.

## 8. Tests and actual results

| Gate | Baseline (after Batch 1 audit) | Final |
| --- | --- | --- |
| `ruff check src/eurus tests` | PASS | **PASS** |
| `ruff format --check src/eurus tests` | PASS (697 files) | **PASS** (710 files) |
| `mypy src` (strict) | PASS (416 files) | **PASS** (425 files) |
| `pytest -q` | 3667 passed, 1 skipped | **3756 passed, 1 skipped** (+89 new, 0 regressions) |
| `python scripts/check_no_comments.py` | PASS | **PASS** (0 violations across 820 files) |
| Frontend lint/typecheck/build | not run — **no frontend file changed** in this batch (content diff is empty; only file-mode noise from the baseline extraction) | not required |

Per-phase evidence (recorded in `docs/implementation/multimodal-roadmap.md`): Phase 5 — 21 phase tests + 173 provider/agent regressions, full suite 3688; Phase 6 — 26 phase tests + 186 gateway/media regressions, full suite 3714; Phase 7 — 39 phase tests + 213 regressions, full suite 3753; final integration — 2 end-to-end tests plus an honest video-availability note test, final suite **3756**. Two regressions found during the phases (duck-typed history messages without `blocks`; synchronous `GeminiProvider._payload` test callers) were fixed in the implementation, not the tests.

## 9. Known limitations

1. Images are re-uploaded inline on every conversation turn that still references them (stateless provider semantics); bounded by the 16-part most-recent-first cap. No Files API or upload caching yet.
2. OpenAI chat-completions wire cannot render PDFs; document-only messages route to Anthropic/Gemini when available, otherwise the PDF rides the text bridge.
3. HEIC/HEIF render for Gemini only; GIF for OpenAI/Anthropic only (skipped elsewhere).
4. Streaming transcription is not implemented (`MEDIA_TRANSCRIPTION_DELTA` exists in the vocabulary without an emitter); timestamps/speaker labels only when the provider returns them.
5. Realtime voice is abstraction-only; no transport in this batch.
6. Telegram bot download cap is 20 MB; larger files are rejected before download (local Bot API servers would lift this — not assumed).
7. Album captions come from the first message; media groups buffer for 1.5 s.
8. Voice replies rely on the model echoing the `[voice:...]` marker (the tool instructs it); if omitted, the text answer still arrives without audio.
9. TTS is OpenAI-protocol only; Gemini/Anthropic native speech APIs are not wired.
10. The web gateway stores and transcribes audio but does not yet expose audio playback in the frontend (Batch 3).

## 10. Security considerations

1. Owner isolation on every stored asset (Telegram scope keys; `get_owned` authorization checks asserted by tests).
2. Size limits enforced before Telegram downloads begin; download, transcription, and synthesis are all timeout-bounded with capped retries.
3. Binary never enters message JSON, task state, or event payloads — asset references only; base64 exists solely inside provider HTTP requests (asserted by tests).
4. Path containment re-verified on every media read; path traversal blocked at the store.
5. Outgoing media retries only provably non-delivered requests (network/429), preventing accidental duplicate sends; speech text is capped and outputs size-checked.
6. Capability mismatches fail loudly at startup (ConfigError) instead of silently degrading; media ingestion failures become explicit user-visible replies, never agent-loop crashes.
7. The zero-comments policy was re-verified across all 820 source files.

## 11. Deferred to Batch 3

- Video intelligence (frame extraction → vision analysis, video summarization beyond metadata).
- Image and video generation.
- Frontend multimodal experience (block rendering, audio playback, attach hints from `model_capabilities`).
- Multimodal memory (retrieval over transcripts/images).
- Realtime voice sessions (transport implementation over the prepared abstraction).
- Files API / upload caching for long image conversations; discovery-driven capability refinement; scheduling of `purge_expired`.

## Final assessment

EURUS is now a multimodal agent, not a text agent with storage: images reach vision models as native parts under enforced capability routing, Telegram is a full multimodal frontend in both directions, and voice notes become reasoning context with spoken replies. Every feature is real code with tests — 89 new tests on top of the 3667-test Batch 1 suite, zero regressions, all quality gates green, zero comments, zero new dependencies.
