# Test Baseline — Phase 0

Environment: Python 3.12.16 (venv), pytest 8.x, pytest-asyncio auto mode, ruff 0.17.4, mypy 2.4.1 (strict), Node 24.22.2, vitest.

## Executed commands and results

| Command | Result |
| --- | --- |
| `ruff check src/eurus tests` | PASS — 0 violations |
| `ruff format --check src/eurus tests` | PASS — 682 files formatted |
| `mypy src` | PASS — 407 files, strict, no issues |
| `pytest -q` | **3424 passed, 1 skipped, 6 warnings** — 342 s |
| `npm run typecheck` | PASS |
| `npm run test` | PASS — 84 tests |
| `npm run lint` | not available (no script, no ESLint config) |

## Environment incidents during baseline

1. First targeted run failed `tests/test_research_compat.py::test_web_fetch_engine_path_contract` — root cause: `trafilatura` (optional dev extra) not installed; extraction fell back to URL-title heuristic. Installing declared dev extras resolved it. Full suite then green. **Not a repository regression.**
2. Dev tooling installed during audit: `aiogram`, `ruff`, `mypy`, `trafilatura`, `ddgs`, `keyring`, `cryptography`, `websockets` — all already declared in `pyproject.toml` dev/web extras. No new dependencies added to the project manifest by the audit.

## Suite composition (266 files, 3425 tests)

Key suites for this batch (names + verified intent):

- `test_event_bus.py` — domain event dedup, retry/backoff, dead-letter, outbox restart, retention (16 tests).
- `test_web_gateway_media.py` — media upload/serving/ownership/markers/inlining.
- `test_providers.py`, `test_openai_compat.py`, `test_gemini.py`, `test_anthropic.py`, `test_sdk_providers.py`, `test_openai_compat.py` — provider wire contracts and retries.
- `test_router.py`, `test_router_cooldown.py`, `test_model_picker.py` — routing and fallback behavior.
- `test_agent.py`, `test_agent_loop_advanced.py`, `test_core_engine.py`, `test_engine_run_characterization.py` — loop and engine behavior.
- `test_runtime_events_p8.py`, `test_runtime_event_bridge.py`, `test_event_buffer_bounds.py`, `test_timing_events.py`, `test_stream_lifecycle.py` — event plumbing.
- `test_stabilization_migrations.py`, `test_migration_safety.py` — migration safety.
- `test_web_gateway_api.py`, `test_web_gateway_integration.py`, `test_gateway_router.py` — gateway contracts.

## Regression policy for this batch

1. After every phase: run the phase's new tests plus directly affected suites.
2. Before final delivery: full `ruff check`, `ruff format --check`, `mypy src`, `pytest -q`.
3. New modules must be importable without optional extras installed (`python -c "import eurus.<module>"` with base deps only) — optional media deps must be lazy.
4. No existing test may be modified to accommodate new behavior unless the behavior is an explicit, justified fix (none anticipated); compatibility layers carry the load.

## Baseline assertions recorded for later comparison

- Total tests: 3425 collected (3424 passed + 1 skipped).
- No flaky failures observed across two full runs.
- Full suite wall time ≈ 5.7 minutes — full-suite re-runs after each phase are practical and mandatory for final verification.
