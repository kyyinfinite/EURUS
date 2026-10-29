# Phase 4 Report — Maintainability and packaging

## Files changed

- `src/eurus/agent/loop.py` — split into cohesive modules and decomposed into
  named steps; behavior unchanged (the whole pre-existing suite passes
  untouched, plus new tests):
  - `src/eurus/agent/budget.py` (new) — `IterationBudget`, `ProgressTracker`,
    `normalize_text`, `PROGRESS_WINDOW`.
  - `src/eurus/agent/history.py` (new) — `repair_history`, `CANCELLED_RESULT`.
  - `loop.py` re-exports both (plus an explicit `__all__`) so every existing
    `from eurus.agent.loop import ...` keeps working.
  - `_run_events` is now an orchestrator over named steps: `_prepare_run`,
    `_plan_initially`, `_run_iteration_step` (which chains `_request_step`,
    `_response_step`, `_completion_step`, `_tools_step`) driven by a small
    mutable `_RunState`. Event ordering and terminal handling are identical;
    verified by the full characterization suite.
- `src/eurus/tui/fullscreen.py` — `FullscreenRenderer`, `attach` and
  `pane_width` moved to `src/eurus/tui/fullscreen_renderer.py` (new); the names
  are re-imported so `from eurus.tui.fullscreen import FullscreenUI, attach,
  INTERRUPT` still works. `FullscreenUI`, key bindings and overlays are
  unchanged.
- `src/eurus/tui/session.py` — the theme/font/glyph/undo command handlers moved
  to `src/eurus/tui/appearance.py` (new) as functions taking the session;
  `InteractiveSession` keeps thin delegates (including the private
  `_theme_menu`/`_theme_switch`/`_font_install`/... names used by tests and
  internals). Split verified by the TUI suites.
- Renamed phase-numbered files (module + 30 test files), all references
  updated; no test content changed:
  - `src/eurus/coding/phase5_tools.py` → `src/eurus/coding/compat_tools.py`
    (function `phase5_tools()` → `compat_tools()`; it was a zero-importer
    compatibility shim whose own docstring already pointed at
    `eurus.coding.workflow_tools`).
  - tests: `test_agent_phase1.py` → `test_agent_tool_safety.py`,
    `test_automation_phase3.py` → `test_automation_worker.py`,
    `test_context_engine_phase2.py` → `test_context_engine.py`,
    `test_delegation_phase5.py` → `test_delegation_tools.py`,
    `test_gateway_phase5_resilience.py` → `test_gateway_resilience.py`,
    `test_gateway_phase6.py` → `test_gateway_access_policy.py`,
    `test_memory_phase3.py` → `test_memory_providers.py`,
    `test_orchestration_phase4.py` → `test_orchestration.py`,
    `test_phase10_observability.py` → `test_observability.py`,
    `test_phase11_resources.py` → `test_resources.py`,
    `test_phase1_runtime_unification.py` → `test_runtime_unification.py`,
    `test_phase2_durable_tasks.py` → `test_durable_tasks.py`,
    `test_phase3_execution.py` → `test_tool_execution.py`,
    `test_phase4_learning_loop.py` → `test_learning_loop.py`,
    `test_phase6_operator.py` → `test_operator_controls.py`,
    `test_phase6_task_recovery.py` → `test_task_recovery.py`,
    `test_phase7_security_recovery.py` → `test_security_recovery.py`,
    `test_phase7_shared_runtime.py` → `test_shared_runtime.py`,
    `test_phase7b_runtime_agents.py` → `test_runtime_agents.py`,
    `test_phase8_approval_security.py` → `test_approval_security.py`,
    `test_phase8_eval_store.py` → `test_eval_store.py`,
    `test_phase8_learning.py` → `test_skill_candidates.py`,
    `test_phase8_plugins.py` → `test_plugin_loader.py`,
    `test_phase8_trajectory.py` → `test_trajectory_store.py`,
    `test_phase9_gateway_router.py` → `test_gateway_router.py`,
    `test_phase9_office_plugin.py` → `test_office_plugin.py`,
    `test_reminders_phase3.py` → `test_reminders.py`,
    `test_scheduler_phase7.py` → `test_scheduler.py`,
    `test_security_phase3.py` → `test_security_hardening.py`,
    `test_skill_learning_phase4.py` → `test_skill_learning.py`.
  - `include_phase4`/`include_phase5` keyword arguments on
    `default_registry` are public API, not modules, and stay (documented).
- Web search/fetch made optional:
  - `pyproject.toml` — `ddgs` and `trafilatura` removed from core
    `dependencies`; new extra `web = ["ddgs>=9.14.0", "trafilatura>=2.2.0"]`;
    both stay in `dev` so the test suite keeps exercising them.
  - `src/eurus/agent/web_tools.py` — imports were already lazy; the missing-
    dependency error now says `pip install 'eurus-agent[web]'`. Verified
    locally with the packages uninstalled: the core registry imports and
    `web_search` returns the clear error; `web_fetch` keeps its stdlib
    HTML-parser fallback.
  - `README.md` — extras table documents `.[web]`.
  - `.github/workflows/ci.yml` — smoke job now asserts the web tools register
    without the extra AND that `ddgs`/`trafilatura` are absent from the plain
    core install.
- httpcore private-API isolation:
  - `src/eurus/security/http_adapter.py` (new) — the only module touching
    `httpcore._backends`; exposes `HTTPCORE_BACKEND_AVAILABLE`,
    `require_backend_seam()`, `ValidatingBackend`, `ValidatingTransport`.
    Its docstring documents precisely why public httpx/httpcore APIs cannot
    provide connect-time IP pinning with original-host SNI/Host (rewriting the
    URL to the pinned IP changes the SNI and breaks certificate verification;
    httpx has no transport hook between DNS resolution and TCP connect).
  - `src/eurus/security/net_guard.py` — no longer imports private httpcore
    modules; keeps all URL/hostname validation and re-exports
    `ValidatingBackend`/`ValidatingTransport` for compatibility. DNS-rebinding
    protection, pinned-IP connection attempts and unix-socket refusal are
    unchanged.
  - `tests/test_net_guard.py` — added
    `test_httpcore_backend_seam_version_guard`, which fails with upgrade
    instructions when httpcore moves the private seam.
- `.github/workflows/ci.yml` — Python `"3.14"` added to the test matrix;
  deterministic-security job list updated for the renamed
  `test_approval_security.py` and extended with `test_file_tools.py`,
  `test_security_hardening.py`, `test_tui_config.py`.

## Files added

- `src/eurus/agent/budget.py`, `src/eurus/agent/history.py`
- `src/eurus/tui/fullscreen_renderer.py`, `src/eurus/tui/appearance.py`
- `src/eurus/security/http_adapter.py`
- (test additions live inside renamed existing files, listed above)

## Files removed

- `src/eurus/coding/phase5_tools.py` (renamed to `compat_tools.py`)

## Tests added

- `tests/test_net_guard.py::test_httpcore_backend_seam_version_guard` —
  version guard for the isolated httpcore backend seam.
- The phase's own requirement for the refactor was "characterization tests must
  still pass unchanged": the entire pre-existing suite runs unmodified against
  the split modules (1380 passed, 1 intentional skip from Phase 3).

## Commands run (real output summary)

Environment: Python 3.12.16, Linux container. Python 3.14 itself is NOT
installed here (see NOT VERIFIED); it was added to the CI matrix only.

- `ruff check .` → `All checks passed!`
- `ruff format --check .` → `337 files already formatted`
- `mypy` → `Success: no issues found in 192 source files`
- `pytest -q` → `1380 passed, 1 skipped in 162.98s`
- `python scripts/check_docs_links.py` → `OK: all internal links resolve across
  12 files`
- `python -m build --wheel` → built successfully; wheel metadata shows ddgs and
  trafilatura only under `extra == 'web'` (and `dev`), not in core
  requirements; `src/eurus/coding/compat_tools.py` present,
  `phase5_tools` absent.
- Local simulation of the new CI smoke step (ddgs/trafilatura uninstalled):
  registry import OK, `web_search` returns the clear
  `pip install 'eurus-agent[web]'` error, `find_spec` for both packages is
  `None`.

## Design decisions (ambiguous/safest choices)

1. The module splits preserve every public name through re-exports, so no
   import anywhere needed to change and the characterization suite ran
   unchanged (its passing is the phase's acceptance criterion).
2. `_run_events` decomposition uses a `_RunState` holder with an explicit
   `terminal` flag instead of reordering control flow; every `finish` path,
   event order and budget check is byte-for-byte the same sequence as before.
3. `compat_tools` keeps a thin compatibility wrapper (renamed) rather than
   deleting it, because the phase asked for a rename, not a removal; the
   phase-numbered function name inside it was renamed too.
4. For net_guard, the "document why impossible" path of the requirement was
   taken for the TLS/SNI part: public httpx/httpcore APIs cannot pin the
   connect IP while keeping original-host SNI. All private-API usage now lives
   in one adapter with a version-guard test that names the upgrade action.
5. `include_phase4`/`include_phase5` registry kwargs were kept: renaming them
   would break public callers, which no phase authorizes.

## NOT VERIFIED

- Python 3.11, 3.13 and 3.14 interpreters (only 3.12.16 available locally);
  the 3.14 CI job was added but not executed here.
- GitHub Actions runs of the updated workflows.
- Real terminal rendering of the refactored TUI modules (verified through the
  headless prompt_toolkit harness and unit suites).
- Real httpcore upgrade scenarios (the guard test documents the failure path
  but upstream behavior can only be verified when httpcore changes).

## Fix — stale bytecode flake (CI Python 3.11)

- Cause: `edit_file_replace_all` rewrites `config.py` with a same-length string ('old.example' -> 'new.example') within the same second as the first check run; the `.pyc` written by the first run is accepted as fresh (mtime is stored in whole seconds, size is equal), so the second run imports the old code.
- Fix: `run_validation` and `scrubbed_env` set `PYTHONDONTWRITEBYTECODE=1`, so checks and host shell commands never reuse a stale `.pyc` after an agent edit.
- Added `tests/test_stale_bytecode.py` (pins the rewritten file's mtime to the original to force the condition).
- NOT VERIFIED: pytest/mypy/ruff were not run for this fix in this environment (dependencies not installed); the stale-pyc mechanism itself was reproduced with plain Python.
