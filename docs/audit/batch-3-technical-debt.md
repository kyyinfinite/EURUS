# Batch 3 Technical Debt Audit

Date: 2026-11-08
Scope: Debt items found during the baseline audit, triaged into Batch 3 blockers vs deferred.

## Blockers for Batch 3 (will be fixed during implementation)

| ID | Item | Location | Impact | Plan |
| --- | --- | --- | --- | --- |
| TD-1 | `src/eurus/media.py` top-level module shadowed by `media/` package — dead legacy MediaStore | `src/eurus/media.py` (362 lines) | Confusion risk for any code importing `eurus.media`; duplicate contract | Delete in Phase 8 (unreachable at runtime; verified import resolution prefers the package) |
| TD-2 | `VideoFrameBlock` defined but never constructed; `VIDEO_FRAME_EXTRACTED` event has no emitter | `content.py`, `events/types.py` | Phase 8 needs both | Phase 8 constructs both |
| TD-3 | 5 multimodal event types without emitters | `events/types.py` | Frontend streaming UX (Phase 10) needs real events | Emit `MEDIA_PROCESSING_STARTED`, `MEDIA_TRANSCRIPTION_DELTA` (where provider supports), `MEDIA_ANALYSIS_COMPLETED` during Phases 8–9 |
| TD-4 | No thumbnail generation despite `MAX_THUMBNAIL_DIMENSION` knob | `media/limits.py`, `media/processing.py` | VideoBlock.thumbnail_asset_id always empty; Telegram video sends without preview | Phase 8: ffmpeg-based thumbnail for videos |
| TD-5 | Frontend dead module `chat/media.tsx` (405 lines) | `frontend/src/components/chat/media.tsx` | Divergent duplicate of `media/MediaBlocks.tsx` | Phase 10: delete after verifying zero imports |
| TD-6 | Web runtime never registers `speak` tool; `[voice:]` markers surface as literal text on web | `gateway/web/runtime.py`, frontend | Web users see raw markers | Phase 10: register speak on web + frontend marker resolution |
| TD-7 | `ToolContext.artifacts` populated only in tests; `register_tool_artifact` has no production caller | `tools/base.py`, `tools/artifacts.py`, `runtime/engine.py` | Phase 9 artifact delivery needs this wired | Phase 9: wire `context.artifacts` in `AgentEngine._bind_run_context` |
| TD-8 | Optimistic user message omits attachment markers | `frontend/src/lib/chat.ts` | Attachments invisible until refetch | Phase 10 fix |

## Deferred (out of Batch 3 scope unless they block)

| ID | Item | Location | Notes |
| --- | --- | --- | --- |
| TD-9 | `HybridMemoryProvider` + `build_embedding_provider` dormant (never called in production) | `memory/manager.py`, `memory/embeddings.py` | Semantic search scaffolding exists; FTS5 is the production path. Phase 11 may activate it if a real embedding endpoint is configured, but no heavy dependency will be added |
| TD-10 | Client-side artifact system (`buildArtifact`, `ArtifactCard`, `ArtifactDrawer`) dormant | `frontend/src/lib/artifacts.ts`, `chat/ArtifactCard.tsx`, `chat/ArtifactPane.tsx` | Revived in Phase 10 for server ArtifactBlocks rather than deleted |
| TD-11 | Image re-upload per turn (provider request rebuilds data URLs from bytes each call) | `providers/media_parts.py` | Cost issue documented in known-limitations; no provider Files API integration yet |
| TD-12 | OpenAI protocol lacks PDF document wire support (Anthropic/Gemini have it) | `providers/media_parts.py` | Documented; provider API limitation |
| TD-13 | HEIC images unsupported | `media/signatures.py` | Documented limitation |
| TD-14 | Album caption handling limitation on Telegram | `gateway/telegram/media.py` | Documented |
| TD-15 | `save_messages` rewrites full history each turn | `sessions.py` | Perf debt, not a Batch 3 blocker |
| TD-16 | Frontend has no ESLint config | `frontend/package.json` | Roadmap documents lint as N/A; typecheck is the gate |
| TD-17 | Vite chunk >700kB warning on build | `frontend/vite.config.ts` | Pre-existing, non-blocking |
| TD-18 | `tests/test_mcp_resilience.py` sync functions marked asyncio (6 warnings) | `tests/` | Cosmetic warnings, existing, not touched |

## Security-relevant observations (verified, no action needed)

- Media files stored 0600 with traversal-checked paths; owner isolation enforced on reads.
- FFmpeg is invoked via argument lists today (ffprobe); Phase 8 must keep `shell=False` discipline.
- Web gateway: bearer token (0600 file), loopback binding, origin checks; realtime (Phase 12) must never leak server keys to the browser — ephemeral tokens only.
- Secrets/PII redaction exists for memory content (`security/redaction.py`); Phase 11 media-memory ingestion must reuse it.
