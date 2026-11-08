# Batch 3 Test Baseline

Date: 2026-11-08
Environment: Python 3.12.16, node v24.22.2, ffmpeg 7.1.5.

## Python

| Gate | Command | Result |
| --- | --- | --- |
| Lint | `ruff check src/eurus tests` | PASS |
| Format | `ruff format --check src/eurus tests` | PASS (700 files) |
| Types | `mypy src` | PASS (425 files, strict mode) |
| Tests | `pytest -q` | 3756 passed, 1 skipped, 361s |

Categorization: zero existing failures, zero regressions, zero environment failures. The 1 skip is the browser integration marker (no Chromium in CI-style runs). 6 pytest warnings are cosmetic (`@pytest.mark.asyncio` on sync functions in `test_mcp_resilience.py`, pre-existing, untouched).

## Frontend

| Gate | Command | Result |
| --- | --- | --- |
| Install | `npm ci` | 529 packages |
| Types | `npm run typecheck` | PASS |
| Tests | `npm run test` | 13 files, 84 tests, PASS |
| Build | `npm run build` | PASS (chunk-size warning pre-existing) |
| Lint | n/a | No ESLint config exists in the repo; typecheck is the enforced gate |

## Multimodal test inventory (existing)

| File | Tests | Coverage |
| --- | --- | --- |
| `test_content_blocks.py` | 36 | block validation, roundtrips, projections |
| `test_media_infrastructure.py` | 48 | store lifecycle, sniffing, limits, ffprobe (skip-aware) |
| `test_message_blocks_integration.py` | 10 | agent history, session roundtrip, checkpoints |
| `test_model_capabilities.py` | 38 | registry semantics, routing, TOML config |
| `test_multimodal_events.py` | 27 | event vocabulary, validation, publisher |
| `test_multimodal_integration.py` | 1 | web upload→message→routing |
| `test_multimodal_intelligence_integration.py` | 2 | batch 2 end-to-end |
| `test_vision_intelligence.py` | 21 | asset resolution, provider conversion, fallback |
| `test_telegram_multimodal.py` | 27 | ingest, albums, sender, retry |
| `test_audio_intelligence.py` | 39 | STT/TTS shapes, pipeline, speak tool |
| `test_web_gateway_media.py` | 26 | upload API, content serving |

Frontend: `lib/{media,chat,ws,thinking,gatewayBatch,artifacts}.test.ts`, `store/{gateway,ui}.test.ts`, plus component/page tests — none cover message rendering, media blocks, or composer yet.

## Batch 3 test debt to close

- Phase 8: video metadata, frame extraction, timestamp mapping, sampling, corrupt/oversize/missing-ffmpeg/missing-audio handling, provider mismatch, cancellation, limits, Telegram/web integration.
- Phase 9: generation tools, provider selection, unsupported capability, job lifecycle, polling, cancellation, timeout, cost caps, artifact storage, duplicate prevention, delivery.
- Phase 10: block renderer, legacy compatibility, image/audio/video/artifact rendering, composer, upload progress, streaming events, error states, responsive behavior, typecheck+build.
- Phase 11: media indexing, per-modality retrieval, dedup, re-index, deleted asset handling, owner isolation, retention, provenance, failure recovery.
- Phase 12: session creation/auth, audio in/out, streaming events, interruption, reconnection, tool calling, approval, timeout, owner isolation, provider failure, cleanup.

Regression policy: no existing test is deleted or weakened; new features are additive; failures must be root-caused before any fix.
