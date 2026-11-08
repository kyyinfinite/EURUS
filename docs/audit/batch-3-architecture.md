# Batch 3 Architecture Audit

Date: 2026-11-08
Scope: Critical architecture questions from the Batch 3 specification (0.4) plus the frontend audit (0.5), answered against actual code.

## 1. Critical Architecture Questions

| Question | Answer | Evidence |
| --- | --- | --- |
| Can AgentEngine handle multimodal messages without a separate path? | YES | `Agent.run(text, blocks=())` → single `Message.user(text, blocks)` in history; blocks flow through the same loop every turn; no modality-specific engine exists |
| Are content blocks preserved across the conversation lifecycle? | YES | `ContextWindow.fit` preserves messages; `SessionStore.save_messages` serializes blocks into `messages.payload`; restart-safe via durable task `state_json` in gateway manager |
| Is provider routing based on actual model capabilities? | YES | `CapabilityRegistry.lookup` precedence: exact (provider, model) → family prefix → provider declaration → protocol default; `Router._routed_candidates` filters by capability ∧ wire modality, degrades to text bridge |
| Can media assets be accessed securely? | YES | sha256 asset ids, 0600 files, 0700 dirs, path-containment check (`TRAVERSAL_BLOCKED`), owner isolation on get, session scoping |
| Does event streaming support media lifecycle? | PARTIAL | media.received / processing / transcription / failure flow end-to-end; 5 event types (`MEDIA_VALIDATION_*`, `MEDIA_PROCESSING_STARTED`, `MEDIA_TRANSCRIPTION_DELTA`, `VIDEO_FRAME_EXTRACTED`, `MEDIA_ANALYSIS_COMPLETED`) have no emitters — Batch 3 hooks |
| Do Telegram and Web use a unified message contract? | YES | both build `GatewayMessage(platform, ..., blocks=())` → `router.handle`; both ingest via `MediaStore` + `run_processing_pipeline` |
| Can artifacts be sent back through the gateway? | YES on Telegram (run-id lookup → `send_bytes`), YES on web (`/api/artifacts/{id}/content`) | but nothing publishes artifacts from tools in production today |
| Can media be reused in conversation context? | YES for images | cross-turn image context verified (user blocks re-resolved per request, most-recent-first cap 16); audio/video reuse only via transcript/text bridge |
| Are media errors handled without crashing the agent loop? | YES | validation/limit/ownership errors map to `MediaError` codes; ingest failures degrade to error notes in message text; provider asset resolution failures fall back to text bridge with warning |
| Do memory and media storage have consistent ownership and retention? | PARTIAL | both are SQLite with owner_id columns; media retention via `purge_expired(retention_days)`, memory via `expires_at` + `enforce_budget`; but memory has zero awareness of media assets — no media rows are ever indexed |

## 2. Frontend Audit (0.5)

Rendering pipeline today:

1. REST `GET /api/sessions/{id}/messages` returns `MessageView` with plain string `content` only — no `blocks` field (pydantic `extra="forbid"`).
2. `parseMessageContent()` (`lib/media.ts`) regex-parses `[attach id=… kind=… name=… mime=… size=…]` markers produced server-side.
3. `MessageRenderer.tsx` maps segments to `MediaBlock` (ImageThumb/AudioPlayer/VideoBlock/DocumentPreview) with fullscreen `ImageViewer`.

What the frontend does NOT know about (all backend contract exists):

- `MessageView.blocks` — not exposed by REST.
- 7 multimodal agent event kinds (`content_block_*`, `media_input_analyzing`, `audio_output_*`, `artifact`) — untyped, unrendered; the hub already serializes `block`, `asset_id`, `artifact_id`, `artifact_status` in `agent_event.view`.
- `[voice:<id>]` markers — render as literal text on web (Telegram resolves them).

Composer: already strong — multi-attachments, drag & drop, clipboard paste, MediaRecorder voice notes (60s), per-file progress/cancel/retry, 20MB cap, capability warning. Does not render attachments in the optimistic user bubble.

Chat state: Zustand (`store/gateway.ts` `useGateway` + `bindSocket`), TanStack Query for REST, `GatewaySocket` (`lib/ws.ts`) with subscribe/heartbeat/reconnect, rAF-batched ingest.

Streaming UX: `LiveTurnCard` accumulates text only; run completion triggers a 20×350ms refetch poller.

Design language: warm paper/copper light theme, Tailwind utility classes, Inter + JetBrains Mono, full local UI kit in `components/ui/`.

Frontend test infra: Vitest + jsdom + @testing-library/react, 13 files, 84 tests. No tests exist yet for MessageRenderer, MediaBlocks, ChatComposer, ConversationView.

Dead/dormant frontend code: `chat/media.tsx` (405 lines, unimported duplicate); client-side artifact system (`lib/artifacts.ts`, `ArtifactCard`, `ArtifactPane`) rendered nowhere — natural seam to revive for server ArtifactBlocks.

## 3. Architecture Constraints for Batch 3

1. `ToolOutput` is text-only (`text, is_error, status`); tool→model feedback is plain text by construction; `Message` blocks are user/assistant-only.
2. The established tool-produced-media convention is the marker pattern (`[voice:<id>]`) + gateway-side resolution + `event_sink` events. Image/video generation should follow it or extend it, not invent a parallel channel.
3. `ToolContext` has no database/media/artifact handles; tools either get stores injected at construction (speak pattern) or reach `paths.database_path()` themselves.
4. `engine._bind_run_context` is the single place production ToolContext identity fields get stamped (run_id, owner_id, session_id, run_store, event_sink).
5. Memory lives in `sessions.py` schema (`memories` + FTS5), NOT in `storage/migrations.py`; media metadata (transcripts) lives in `media_files.metadata_json`.
6. Web runtime never registers `speak` — web users get literal markers. Any generation tool must be registered on all three runtimes (TUI/Telegram/Web) or deliberately scoped.
7. `media_parts.WIRE_MODALITIES_BY_PROTOCOL` = openai {text,image}, anthropic {text,image,document}, gemini {text,image,document} — audio/video blocks never reach provider wire; only transcripts/summaries do. Video understanding must either extend wire modalities (Gemini native video) or use frames+transcript.

## 4. Risks

- **R1** Gemini native video upload requires inline bytes ≤ 15MB (current inline cap) or Files API — long videos need the frames+transcript fallback; capability check must be per-model.
- **R2** Video generation providers operate asynchronously (jobs, polling) — must not block the agent loop; needs background polling in the tool layer or a job runner.
- **R3** Frontend block rendering is a large surface — must stay additive (`MessageView.blocks` optional) to avoid breaking the 84 green tests and legacy messages.
- **R4** Memory indexing of media must respect the existing FTS5 + owner-isolation contract; large transcripts (20k chars) exceed memory `content` 5000-char cap — needs summarization or truncation strategy.
- **R5** Realtime voice requires ephemeral credentials (never expose server keys to browser) and session governance; only one provider should be implemented first (OpenAI Realtime) per spec.
- **R6** ffmpeg subprocess invocation must be shell-free (argument lists, no shell=True) with timeout and cleanup to avoid resource exhaustion.

## 5. Decisions

- D1: Implement video frame extraction with optional ffmpeg detection (graceful degradation, mirroring the existing ffprobe pattern).
- D2: Generation tools follow the speak pattern (factory-injected MediaStore + marker + events) and also publish artifacts via the existing ArtifactStore for durable delivery.
- D3: Extend `MessageView` with optional `blocks` (additive); frontend renders blocks first, falls back to marker parsing.
- D4: Media memory uses derived text (captions/transcripts/summaries) stored as memories with provenance referencing asset ids — never binary media in the memory store.
- D5: Realtime voice: OpenAI Realtime first, behind the existing protocol abstraction, web-gateway WebSocket bridge, ephemeral tokens.
