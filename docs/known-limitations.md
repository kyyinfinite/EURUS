# Known limitations (0.4.1)

Honest inventory of what this beta does not do. Each entry is verified against the
code and tests; none are speculative.

## Security

- **Host sandbox backend is unconfined.** With `[sandbox] backend = "host"` (the
  default on Termux; the default elsewhere is `container`), the shell tool runs
  commands with your OS permissions. Approval gating and the scrubbed environment
  still apply, but there is no filesystem or network isolation. The `container`
  backend is the confined path.
- **Some coding surfaces still validate then use paths non-atomically.** The
  agent-facing file tools (`read_file`, `create_file`, `edit_file`, `apply_patch`,
  `propose_patch`, `search_code`, `document_create`, `document_replace`,
  `apply_patchset`) re-open every path component with `dir_fd` and `O_NOFOLLOW` at
  use time, so a symlink swap between validation and IO is rejected (fail-closed).
  The office renderers and the document library readers (`openpyxl`, `pptx`,
  `docx`) still follow validated paths without per-component fd checks.
- **Remote CDP attach has no egress proxy.** When connecting to an externally started
  Chromium (`browser.cdp_endpoint`), Eurus cannot inject the enforcing proxy flags;
  the request-level guard still blocks forbidden schemes, but destination addresses
  are the browser's own resolution. Use a managed launch for untrusted browsing.
- **MCP server surfaces are allowlist-gated, not approval-gated.** The built-in MCP
  server cannot run interactive approvals; mutating tools are denied to MCP clients
  unless `allow_mutating` is explicitly enabled in the server access policy.
- **Gateway media is single-owner.** Any holder of the gateway token can read any
  session's media; sessions are a UI concept, not security principals. Media IDs are
  unguessable, and the token is required.
- **JSON Schema `format` is annotation-only.** The validator does not enforce
  `format: uri` and friends, matching JSON Schema's default behaviour.

## Reliability

- **No cross-process coordination for the same workspace.** Two Eurus processes
  pointed at one workspace rely on path locks inside a process, not between processes.
- **Egress proxy stops with the manager.** The proxy lives inside the browser manager
  and is cleaned up when the manager closes; a hard process kill leaves it to OS
  socket teardown.
- **Plain-HTTP proxying closes connections after each response** (`Connection:
  close`). HTTPS traffic tunnels normally; visible only as slightly higher
  connection counts on plain-HTTP sites.

## Product

- **No multi-user mode.** One owner per installation. Memory, media, and missions are
  not separated between principals.
- **Autonomous workers cannot approve.** Jobs that require approval fail rather than
  escalate; this is deliberate.
- **Browser downloads require the managed backends.** The `container` sandbox backend
  has no download support wired today.
- **Windows is not tested.** The suite runs on Linux; macOS is best-effort.

## Multimodal (Batch 2)

- **Images re-upload inline on every turn.** Vision requests embed base64 image parts
  for each conversation turn that still references them (stateless provider APIs); the
  16-part most-recent-first cap bounds the cost, but a long image conversation pays per
  turn. No Files API / upload caching exists yet.
- **OpenAI chat-completions cannot render PDFs.** Document-only messages route to
  Anthropic/Gemini when available, otherwise the PDF rides the text bridge.
- **HEIC/HEIF images render for Gemini only.** OpenAI and Anthropic wire formats do not
  accept them; those blocks fall back to the text bridge.
- **Transcription quality is the provider's.** EURUS passes a language hint and stores
  the returned text; timestamps and speaker diarization are only kept when the provider
  returns them (OpenAI `whisper-1` plain responses carry neither).
- **Realtime voice sessions are not implemented.** `eurus.audio.realtime` defines the
  abstraction only; no provider transport exists in this batch.
- **Telegram bot API download cap (20 MB).** Larger files require a local Bot API
  server or premium limits; EURUS rejects them before downloading.
- **Album captions come from the first message only**, and media groups are buffered
  for 1.5 s before submission.
- **Voice replies depend on the model including the `[voice:...]` marker.** The speak
  tool instructs the model to echo it; if the model omits it, the text answer still
  arrives, but the audio is not delivered.
- **Streaming transcription is not claimed.** Transcription runs once at ingestion, not
  incrementally; `MEDIA_TRANSCRIPTION_DELTA` events exist in the vocabulary but are not
  emitted by the OpenAI-compatible transcriber.

## Testing

- Real-browser tests require a local Chromium (`python -m playwright install
  chromium`) and are skipped when unavailable. CI runs the full matrix on Linux only.
- No prior released version exists, so upgrade testing covers schema migration from
  the oldest supported schema and data preservation, not a live old-to-new binary
  upgrade.

## Batch 3 additions

- Gemini native video understanding is limited to inline payloads (≤10MB); larger videos fall back to extracted keyframes plus the audio transcript.
- Image editing requires an OpenAI-compatible image model (for example gpt-image-1); imagen models support generation only.
- Video generation (Veo) runs as an asynchronous job; delivery after the run completes is via events, artifacts, and (on Telegram) the chat delivery callback.
- Realtime voice is available for OpenAI realtime models on the web gateway only, uses read-only tools (mutating and approval-requiring tools are refused in voice mode), and requires the optional `websockets` dependency.
- Media memory indexes derived text (transcripts, summaries, inline document text, metadata); image captions/OCR require the `index_media_caption` path and are not auto-invoked by vision analysis.
