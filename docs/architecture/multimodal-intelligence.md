# Multimodal Intelligence Architecture — Batch 2 (Phases 5–7)

Batch 2 turns the Batch 1 multimodal foundation into working intelligence: vision input
through real provider wire formats, a multimodal Telegram gateway, and audio/voice
understanding with STT and TTS. One agent loop, one media store, one event system, one
capability registry — every modality flows through the same pipeline:

```
Unified Gateway (Telegram / Web)
  → Media download + validation + MediaStore registration
  → Unified Content Blocks (ImageBlock / AudioBlock / VideoBlock / DocumentBlock)
  → GatewayMessage(blocks) → durable task (blocks in task state)
  → AgentRuntime → AgentEngine → Agent loop
  → Router: capability + wire-modality filtering (vision/audio-capable models first)
  → Provider adapters render native media parts (image_url / image+document / inline_data)
  → Agent response (text + optional [voice:<asset>] marker from the speak tool)
  → Gateway outgoing media (voice message / audio / photo / video / document / artifacts)
```

## Vision intelligence (Phase 5)

- `src/eurus/providers/media_parts.py` resolves `ImageBlock`/`DocumentBlock` asset
  references from the shared MediaStore (lazy, off-loop, most-recent-first, at most 16
  parts per request) and renders provider-native parts:
  - OpenAI-compatible: `{"type": "image_url", "image_url": {"url": "data:<mime>;base64,..."}}`
  - Anthropic: image blocks plus base64 PDF document blocks
  - Gemini (HTTP and google-genai SDK): `inline_data` parts for images and PDFs
- Inline caps guard the request size (OpenAI 20 MB per image, Anthropic 5 MB per image /
  20 MB per PDF, Gemini 15 MB). Oversized, missing, or mime-mismatched assets fall back
  to the text bridge for that block instead of failing the request.
- `Router.chat/stream` now enforce capabilities at request time: candidates must accept
  every required modality (from `required_modalities_for_messages`) AND the protocol must
  be able to put that modality on the wire (`WIRE_MODALITIES_BY_PROTOCOL`). If nothing in
  the chain qualifies, the request degrades to the text bridge with a warning — images are
  never silently sent to text-only models.
- The agent emits `media_input_analyzing` for every media block in the incoming message;
  the engine translates it into a durable `media.input.analyzing` event.
- Vision questions (describe, VQA, screenshots, UI inspection, OCR, comparison, diagrams)
  are answered by the vision model itself — there are no duplicate single-purpose tools.

## Telegram multimodal gateway (Phase 6)

- Incoming media (photo, image document, voice note, audio file, video, video note,
  document) is downloaded asynchronously, validated and stored by the single MediaStore,
  then submitted as `GatewayMessage(blocks=...)`. Album messages (media groups) are
  buffered for 1.5 s and flushed as one message with all blocks.
- Blocks persist in the durable task state, so retries and restarts keep the media
  references; the runner reconstructs them and passes them to `AgentRuntime.run`.
- Outgoing: `TelegramMediaSender` picks the Bot API method by MIME (photo / voice /
  audio / video / document), enforces the 50 MB upload cap, and retries only
  unambiguous non-delivery errors (network, 429 with retry-after) so media is never
  accidentally sent twice. Artifacts produced during a run are delivered after it
  finishes.
- Duplicate Telegram updates are rejected by the pre-existing `claim_update` gate; every
  stored asset carries the Telegram owner scope key.

## Audio and voice intelligence (Phase 7)

- `[audio]` configuration (`stt_model`, `stt_language`, `tts_model`, `tts_voice`,
  `tts_format`, timeouts) with `EURUS_STT_*` / `EURUS_TTS_*` environment overrides.
- STT: `OpenAICompatTranscriber` posts multipart to `/audio/transcriptions`
  (whisper-1, gpt-4o-transcribe, groq whisper-large-v3, ...). The factory refuses models
  that do not declare `audio_input` + `text_output` and providers that are not
  OpenAI-compatible. Indonesian / English / multilingual is a language hint — the
  provider does the actual work.
- TTS: `OpenAICompatSpeechSynthesizer` posts to `/audio/speech` and stores the result in
  the MediaStore (`mp3`, `opus`, `aac`, `flac`, `wav`; `opus` maps to Telegram voice
  messages).
- Transcription runs at ingestion (Telegram voice notes and web uploads): the transcript
  is stored as media metadata, copied onto `AudioBlock.transcript` (kept separate from
  metadata), and inlined into the text bridge so text-only chains can still answer.
  `MEDIA_TRANSCRIPTION_STARTED/COMPLETED` and `MEDIA_PROCESSING_FAILED` (stage
  `transcription`) events are published with the validated payloads from Batch 1.
- Voice replies: the `speak` tool (registered when TTS is configured) synthesizes speech,
  stores the asset, emits an `audio.output.ready` tool event, and returns a
  `[voice:<asset_id>]` marker. The Telegram runner resolves the markers after the answer,
  sends the audio (voice message for OGG/Opus, audio file otherwise), and strips the
  markers from the text.
- Realtime voice: `src/eurus/audio/realtime.py` defines the stream/session/interruption
  surface only. No transport is implemented in this batch (Batch 3 scope).

## Supported modality per provider (what EURUS actually sends)

| Provider protocol | Text | Images | PDF documents | Audio in (inline) | Audio out |
| --- | --- | --- | --- | --- | --- |
| openai (chat completions) | yes | jpeg/png/webp/gif | no (text bridge or routing to another protocol) | via STT transcript only | TTS (`/audio/speech`) |
| anthropic | yes | jpeg/png/webp/gif | yes | via STT transcript only | — |
| gemini (HTTP + genai SDK) | yes | jpeg/png/webp/heic/heif | yes | via STT transcript only | — |
| groq (openai protocol) | yes | per-model capability | no | STT (`whisper-large-v3`) | — |

Routing picks the first chain entry whose declared capabilities cover the required
modalities AND whose protocol can render them. Undeclared models are conservatively
treated as text-only (declare `[providers.<name>.capabilities]` to override).

## Resource limits and security

- All media limits stay centralized in `MediaLimits` (`EURUS_MEDIA_*`); Telegram
  downloads are size-checked before they start and time-bounded (90 s default).
- STT/TTS requests carry their own timeouts and bounded retry budgets (2 retries,
  retryable errors only); speech text is capped at 4096 characters.
- Owner isolation: gateway media is stamped with the Telegram scope key; `get_owned`
  enforces authorization; storage paths are containment-checked on every read.
- Event payloads never carry binary; message JSON never carries base64 blobs — asset
  references only. Base64 exists solely inside the outgoing provider HTTP request.
- `speak` output and transcribed text are stored as ordinary media assets with the same
  lifecycle, retention, and dedup rules as uploads.
