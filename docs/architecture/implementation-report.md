# Implementation Report — EURUS Autonomous Runtime Evolution

Final report for the eight-phase evolution. Every claim below is backed by
the test suite, the linters, or the source tree; features are labelled
**implemented + tested**, **implemented (integration-level tests)**, or
**partially supported** with the exact boundary stated.

## 1. Scope executed

All eight phases were implemented to completion:

| Phase | Delivered |
| --- | --- |
| 0 — Audit | `autonomous-runtime-audit.md` (pre-existing code inspection, dependency graph, weaknesses, decisions) |
| 1 — Centralized automation runtime | `automation/` package: engine, worker pool, executor, retry, policies, concurrency, reconciliation, notifications, supervision |
| 2 — Advanced scheduler | `cron.py` cron engine; `scheduler.py` evolved with occurrences, misfire policies, revisions, event triggers |
| 3 — Goal engine | `goals/` package: store, states, engine, agent tools |
| 4 — Workflow engine | `workflows/` package: definition + validation, store, engine (13 node types) |
| 5 — Multi-agent orchestration | `multiagent/` package: registry, orchestrator, structured communication |
| 6 — Event-driven automation | `events/` package: domain event store, bus with outbox and delivery retry |
| 7 — Learning & evaluation | `learning/evaluation.py` (evaluation engine, learning pipeline, lesson validation), trajectory wiring in the executor |
| 8 — Production reliability | `automation/supervision.py`, unified daemon, CLI management surface, retention, health |

## 2. Components reused (not rewritten)

- `runtime/engine.py` (`AgentEngine`) — the single execution path; untouched.
- `runtime/recovery.py` failure classification — reused by retry and reconciliation.
- `storage/jobs.py` lease/fencing semantics — extended (priority, source, correlation, retry scheduling, dead letters), never bypassed.
- `agent/delegation.py`, `child_runtime.py` — retained for single delegation; the orchestrator builds on the same durable-job pattern.
- `learning/*` lesson/skill machinery, `trajectory.py` redaction — wrapped, not duplicated.
- All gateways (Telegram, Web), TUI, MCP, browser, providers, memory — unchanged.

## 3. Components refactored

- `cli/schedule.py` daemon: the CLI-local closure (audit weakness #1) now
  delegates to `cli/autonomy.run_daemon`, which assembles the full stack
  through `AutomationEngine` + `AgentJobExecutor`.
- `storage/migrations.py`: v6 (prior session) re-verified; v7 added for
  workflow child references, node timestamps, assignment owner/depth, agent
  messages.
- `automation/notifications.py`: added `workflow_notification` kind.
- Bugs fixed during hardening: goal pause from planning states (state
  table), superseded-task cancellation on replan, skip propagation for
  SKIPPED workflow dependencies, event outbox clock consistency
  (fan-out/due-check now share the tick clock), `ConcurrencyGovernor`
  metrics on a slotted dataclass, `automation/concurrency.py` missing
  `datetime` import, scheduler cron-anchor variable typing.

## 4. New components

- `automation/engine.py`, `worker.py` (extended), `executor.py`,
  `retry.py`, `policies.py`, `concurrency.py`, `reconciliation.py`,
  `notifications.py`, `supervision.py`
- `cron.py`
- `goals/` (engine, store, states, tools)
- `workflows/` (engine, definition, store, states)
- `multiagent/` (registry, orchestrator, communication)
- `events/` (bus, store)
- `learning/evaluation.py`
- `cli/autonomy.py` (management commands + unified daemon)

## 5. Database changes

Migrations v6 and v7 (see `migration-guide.md`): 20 new tables
(dead letters, goals ×3, workflows ×4, specialists/assignments/heartbeats,
messages, domain events/subscriptions/deliveries/outbox, evaluations,
provenance) plus additive columns on `eurus_jobs`, workflow instances, node
states, and assignments. All changes additive, atomic, and idempotent; no
existing data touched. Safe on existing installations (verified by the
migration-safety test pattern in `tests/test_migration_safety.py` and the
restart tests).

## 6. Central Agent Engine integration

Every execution source funnels into `AutomationEngine.submit` →
`AutomationWorkerPool` → `AgentJobExecutor.execute` → `AgentEngine.run`:
CLI manual jobs, schedule occurrences, goal planning/evaluation/task jobs,
workflow node jobs, specialist assignment jobs, event-handler jobs, and
workflow compensation jobs. The engines never call models directly; they
advance persisted state machines and submit durable work. Verified by
`tests/test_e2e_scenarios.py` (all eight scenarios) and each engine's
integration tests.

## 7. Capability summary

- **Durable automation** — implemented + tested (lease fencing, retries with
  backoff/jitter, dead letters, replay, unknown-outcome handling,
  reconciliation; `test_automation_engine.py`, 22 tests).
- **Advanced scheduling** — implemented + tested (once/interval/cron/event,
  DST-aware occurrence calculation, four misfire policies, concurrency,
  windows, expiry, caps, revisions, manual trigger; `test_cron_engine.py`
  28, `test_scheduler_v2.py` 24).
- **Long-running goals** — implemented + tested (lifecycle, dependency
  execution, replanning with superseded-task cancellation, deterministic and
  agent evaluation, owner isolation, restart survival; `test_goal_engine.py`
  14).
- **Workflow orchestration** — implemented + tested (DAG validation, 13
  node types, branching with skip propagation, approvals, subworkflows,
  compensation, versioning, recovery, pause/resume/cancel;
  `test_workflow_engine.py` 23).
- **Multi-agent coordination** — implemented + tested (registry, parallel
  dispatch, capacity/depth/parent limits, failure + reassignment,
  aggregation, heartbeats, restart survival, message log;
  `test_multiagent_orchestration.py` 15).
- **Event-driven automation** — implemented + tested (outbox, dedup,
  fan-out, three handler kinds, retry/backoff/dead-letter, retention,
  restart survival; `test_event_bus.py` 15).
- **Autonomous recovery** — implemented + tested (supervisor: lease
  recovery, stuck-job cancellation, stale-assignment reconciliation,
  subsystem ticks; `test_supervision.py` 7 + scenario C).
- **Validated learning** — implemented + tested (deterministic evaluation
  dimensions, provenance with reversal, lesson validation, trajectory
  recording start/fail/finish; `test_learning_evaluation.py` 11).
- **Resource governance** — implemented + tested (global/per-owner
  concurrency governor, per-goal task caps, per-specialist caps and
  budgets, workflow node caps, retry caps, delegation depth, policy
  deadlines).
- **CLI/API/Telegram compatibility** — implemented + tested (all
  pre-existing commands work; `schedule daemon` options unchanged;
  `test_cli.py` and the full suite pass; Telegram/Web tests untouched).

## 8. End-to-end scenarios

All eight scenarios from the brief pass (`tests/test_e2e_scenarios.py`):
A autonomous goal, B persistent scheduling across restart, C long-running
automation recovery after lease expiry, D workflow failure and recovery
without repeating completed work, E three-specialist fan-out with failure
and reassignment, F event-to-execution flow, G validated reversible
learning, H policy blocking of unauthorized tools with recorded decisions.
An additional cross-restart scenario verifies goals, workflows, and
assignments survive a simulated process restart.

## 9. Tests executed and results

Command: `python -m pytest tests/ -q`

- **2,752 tests collected, 2,751 passed, 1 skipped** (browser integration,
  environment-dependent, skipped by design when Chromium is unavailable).
- New test modules added by this evolution: 178 tests across
  workflow/multiagent/events/learning/supervision/E2E/CLI suites, plus the
  prior session's 88 automation/scheduler/cron/goal tests — all re-run and
  passing after refactors.

## 10. Static analysis

- `ruff check src tests benchmarks`: **All checks passed** (zero findings;
  pre-existing violations in `coding/office_tools.py`, `scheduler.py`,
  `gateway/web/app.py` were also resolved, with a `B008` per-file ignore for
  the standard FastAPI `File(...)` idiom).
- `ruff format --check src tests benchmarks`: **584 files already
  formatted** (the tree was formatted to the current ruff to make the gate
  pass; behavior verified by the suite).
- `mypy` (strict, per project config): **Success: no issues found in 372
  source files**.
- `python -m compileall -q src`: clean.

## 11. Known limitations

1. Exactly-once external side effects are approximated (idempotency keys,
   deterministic job ids, deferred side-effect boundary, reconciliation),
   not guaranteed; uncertain outcomes are surfaced as `unknown_outcome`
   dead letters rather than hidden.
2. Specialist `model_policy` is persisted and validated but the executor
   currently routes through the configured router; per-specialist model
   selection is not yet wired into provider selection.
3. The workflow `schedule` node is one-shot (`run_at`); recurring scheduling
   stays in the scheduler subsystem and can be combined via event triggers.
4. Event retention is time-based only; there is no size-based cap.
5. Assignment heartbeats are written by dispatch and the supervisor;
   in-flight specialists do not yet emit progress beats autonomously (the
   message API exists and is used for request/result/failure/cancellation).
6. `LearningPipeline` accepts lessons from callers (e.g. the existing
   `Reflector`); automatic post-run reflection is not scheduled by the
   daemon yet — the pipeline is a library used by callers and tests.

## 12. Deliverable

Complete project archive: `eurus-autonomous-agent-complete.zip` (source,
tests, docs, dependency metadata; no caches, credentials, or runtime
databases).


---

# Stabilization Report — Autonomous Runtime Repair (final)

This section reports the stabilization pass that followed the evolution
report above. Every claim lists its verifying test or command; statuses use
the vocabulary from `autonomous-runtime.md` §9.

## S1. Defects found and repaired

| # | Severity | Defect | Root cause | Repair | Regression test |
| --- | --- | --- | --- | --- | --- |
| D1 | critical | EventBus in the daemon had no AutomationEngine; every `submit_job` subscription dead-lettered | `build_stack` constructed the bus before the engine and never injected it | composition root reordering + `EventBus(automation=...)` + `set_automation` guard | `test_stabilization_wiring.py::test_daemon_binds_automation_engine_to_event_bus` |
| D2 | critical | Side-effect boundary marked at claim time in production | executor built without `side_effect_marker_factory` so `defer_side_effect_marking` was False | marker factory wired through `_SideEffectMarker` (lease-fenced, raises before wiring) | `test_daemon_uses_deferred_side_effect_boundary`, `test_side_effect_marker_marks_within_lease`, `test_marker_fails_when_lease_lost` |
| D3 | critical | Any daemon claimed any owner's jobs | `JobStore.claim` had no owner filter; the governor did not pass one | owner-scoped claim via `ConcurrencyGovernor(owner_id=...)`; raw store claim = explicit admin mode | `test_stabilization_owners.py::test_owner_a_cannot_claim_owner_b_jobs` |
| D4 | critical | Cross-owner lease recovery | `recover_expired` unscoped; worker poll loop recovered globally every poll | owner-scoped recovery everywhere (`owner_id` param), pool passes `recovery_owner_id` | `test_owner_scoped_recovery_never_touches_other_owners`, `test_unscoped_admin_recovers_everything` |
| D5 | high | Supervisor never forwarded owner to goals/workflows/assignments | `supervise()` called tick/recover without `owner_id` | all supervisor subsystem calls forward `self.owner_id` | `test_supervisor_forwards_owner_to_goal_and_workflow_ticks` |
| D6 | high | Lease-exhausted failures bypassed dead letters/events | `recover_expired` set `failed` directly; no reconciliation step recorded letters | reconciliation records `lease_exhausted` dead letters + publishes `TASK_FAILED` | `test_lease_exhaustion_produces_dead_letter_through_reconciliation`, `test_exhausted_failure_publishes_terminal_event` |
| D7 | high | `model_policy` stored but unused | payload never carried the policy; executor always built the default router | payload propagation + `multiagent/routing.resolve_model_override` + per-model factory cache + trajectory recording | `tests/test_stabilization_routing.py` |
| D8 | high | Learning pipeline not wired | `EvaluationEngine`/`LearningPipeline` had no production call site | `learning/integration.LearningCoordinator` with durable tasks, dedup, provenance, recovery | `tests/test_stabilization_learning.py` |
| D9 | medium | Daemon swallowed every supervision exception | `contextlib.suppress(Exception)` around `supervise()` | explicit logging + bounded daemon backoff + supervisor failure tracking | `tests/test_stabilization_supervisor.py` |
| D10 | medium | Dead-letter duplicates under races | check-then-insert without a unique constraint | unique partial index (v8) + legacy dedup + `IntegrityError` handling | `test_dead_letter_creation_is_idempotent_under_races` |
| D11 | medium | Auto-retry after side-effect boundary | retry decision ignored the boundary | storage-level guard (`require_clean_side_effect`) + worker check | `test_worker_crash_during_mutating_operation_does_not_auto_retry` |
| D12 | medium | Assignment listing/capacity/recovery owner-blind | `AssignmentStore.list_assignments` had no owner filter | owner params on listing/stale/cancel + orchestrator scoping | `test_assignment_listing_and_capacity_are_owner_scoped`, `test_cross_owner_assignment_cancel_is_refused` |
| D13 | medium | Occurrence finalizer never wired | `occurrence_finalizer` param unused in `build_stack` | wired to `ScheduleStore.finalize_occurrence` | `test_occurrence_finalizer_is_wired`, `test_finalize_occurrence_completes_dispatched_occurrences` |
| D14 | medium | Event amplification loop (found during E2E) | wildcard subscriptions consumed the runtime's own `TASK_*` events, creating jobs without bound | `TASK_*` default-deny with explicit `allow_task_events` opt-in | `test_task_lifecycle_events_do_not_retrigger_subscriptions` |
| D15 | low | Fenced commits dropped silently | worker ignored `finish()` failure | warning log + `commits_fenced` metric | `test_stale_worker_cannot_commit_after_lease_expiry` |
| D16 | low | Cross-owner chain schedules / approval decisions / CLI show | owner guards missing at storage boundaries | same-owner chain reference enforcement, owner-scoped `decide_approval`, `--owner` on show/decide commands | `test_chain_schedule_requires_existing_same_owner_dependency`, owner tests |

## S2. Verification record

Commands (from the repository root, `EURUS_HOME` unset or empty):

| Command | Result |
| --- | --- |
| `python -m pytest -q` | **2850 passed, 1 skipped** (browser-dependent Chromium test), exit 0 |
| `ruff check src tests` | All checks passed, exit 0 |
| `ruff format --check src tests` | 596 files already formatted, exit 0 |
| `python -m mypy` (strict, `files=["src"]`) | Success: no issues found in 374 source files, exit 0 |
| `python -m compileall -q src tests` | exit 0 |
| `python -m eurus autonomy --help` | exit 0, all groups listed |
| `python -m eurus autonomy goals create/list/show`, `events publish/subscribe/stats`, `jobs list/dead-letters`, `health` | exit 0 |
| `python -m eurus autonomy daemon --poll-seconds 2 --workers 1` | starts, supervises, shuts down cleanly on SIGINT (exit 0) |

New test modules (97 tests):

- `tests/test_stabilization_wiring.py` — production `build_stack` composition
- `tests/test_stabilization_owners.py` — adversarial two-owner isolation
- `tests/test_stabilization_recovery.py` — crash/fencing/dead-letter injection
- `tests/test_stabilization_supervisor.py` — failure capture, backoff, health
- `tests/test_stabilization_routing.py` — model-policy routing
- `tests/test_stabilization_learning.py` — durable learning lifecycle
- `tests/test_stabilization_scheduler.py` — chain triggers, pause/resume
- `tests/test_stabilization_migrations.py` — v7→v8, dedup, interruption
- `tests/test_stabilization_e2e.py` — scenarios A–H on the production composition
- one updated event-bus test documenting the `TASK_*` opt-in policy

## S3. End-to-end scenarios verified (production composition)

A scheduled execution, B event-triggered execution (+ dedup), C goal
decomposition through completion, D workflow DAG with parallel branches +
approval + completion, D2 failure with skip propagation + compensation
enqueued, E specialist assignment with model policy + reassignment,
F crash-recovery boundaries (claim → marker → unknown outcome → dead
letter), G two-owner concurrent isolation, H shutdown/restart without
duplication or event loss — all in `tests/test_stabilization_e2e.py`.

## S4. Known limitations and deferred work

- Exactly-once side effects remain approximated (leases + fencing +
  reconciliation + dead letters), as before; `unknown_outcome` is surfaced,
  never hidden.
- Wildcard `notify`/`trigger_schedules` subscriptions keep their handler
  semantics; `notify` without a configured sink fails the delivery visibly
  (retried, then dead) rather than pretending success.
- Relative-time and conditional scheduler triggers are deferred; chain
  triggers are implemented with real semantics.
- The interactive path (Gateway → AgentRuntime → AgentEngine) is unchanged;
  durable and interactive paths share `AgentEngine` construction via
  `runtime/wiring.py` but remain separate entry paths by design.

---

# Phase-10 Report — Final Isolation, Fencing & Production Verification

Final report for the phase-10 pass. Every claim lists its verifying test or
command; statuses use the vocabulary from `autonomous-runtime.md` §9.

## P1. Initial audit findings and disposition

The independent re-audit (see `stabilization-checklist.md`, Phase-10
section) confirmed 14 residual items. Disposition:

| # | Disposition |
| --- | --- |
| P10-1 event layer owner-blind | **Repaired** (schema v9 + store + bus + supervisor) |
| P10-2 global dedup index | **Repaired** (per-owner `(owner_id, dedup_key)` index) |
| P10-3 learning coordinator owner-blind | **Repaired** (owner-scoped lifecycle) |
| P10-4 lesson store scope-blind | **Repaired** (private/shared scopes) |
| P10-5 unfenced side-effect marker | **Repaired** (worker identity + lease expiry enforced) |
| P10-6 cross-owner artifact reversal | **Repaired** (owner-verified `deactivate`) |
| P10-7 global stale-assignment retrieval / unverified assignment mutations | **Repaired** (SQL scoping + guarded update/heartbeat) |
| P10-8 cross-owner health/metrics leakage | **Repaired** (owner-scoped snapshots) |
| P10-9 notification sink unconfigured + silent async ack | **Repaired** (shared sink wired; no-loop path never acknowledges) |
| P10-10 cross-owner dead-letter replay | **Repaired** (owner-scoped resolution) |
| P10-11 learning counter type bug | **Repaired** |
| P10-12 invalid CLI resolve default / missing `--owner` | **Repaired** |
| P10-13 malformed CI push trigger | **Repaired** (`branches: [main]`) |
| P10-14 Python 3.12 validation | **Executed** (see P10 below) |

## P2. Root causes

1. **Event ownership never modeled.** Events, subscriptions, and deliveries
   predated the multi-owner model and carried no `owner_id`; scoping added
   in the stabilization pass stopped at the job layer. Fix: schema v9 adds
   ownership columns with a legacy-compatible default (`''` = global), the
   bus publishes through an owner-resolving adapter, and every retrieval or
   mutation verifies ownership in SQL.
2. **Dedup key treated as globally unique.** The v6 partial index ignored
   owner namespace. Fix: index on `(owner_id, dedup_key)`.
3. **Learning lifecycle bolted on globally.** The coordinator predates
   owner scoping and the supervisor forwards stopped at goals/workflows.
   Fix: every task operation carries the owner predicate.
4. **Lessons bypassed the memory scope system.** `LessonStore` wrote via
   `SessionStore.add_memory` without a scope, landing in the global
   namespace. Fix: `owner:<owner_id>` private scopes, `global` only for
   explicit sharing; recall/duplicate-check/removal are scope-filtered.
5. **Marker predates worker identity discipline.** The daemon marker was
   written before `mark_side_effect_started` required a worker; it passed
   `None` and the store ignored lease expiry. Fix: required worker id,
   lease version, and `lease_expires_at >= now` validated atomically.
6. **Supervisor health assembled from admin queries.** Fix: every health
   query carries the supervisor's owner; `None` remains the explicit
   administrative view.

## P3. Files changed

Source: `storage/migrations.py`, `events/store.py`, `events/bus.py`,
`storage/jobs.py`, `learning/integration.py`, `learning/lessons.py`,
`learning/evaluation.py`, `multiagent/orchestrator.py`,
`automation/supervision.py`, `automation/engine.py`,
`automation/concurrency.py`, `cli/autonomy.py`.
Tests: `tests/test_phase10_event_isolation.py`,
`tests/test_phase10_learning_isolation.py`,
`tests/test_phase10_fencing_observability.py`,
`tests/test_phase10_multi_owner.py` (new), plus
`test_stabilization_migrations.py` (2 new v9 tests) and minimal updates to
`test_event_bus.py`, `test_automation_worker.py`,
`test_stabilization_learning.py` where tests pinned the old unfenced or
global-lesson behavior (assertion strength preserved; the updates add
owner-scoped or lease-honoring calls). CI: `.github/workflows/ci.yml`.
Docs: the eleven architecture documents listed in the brief.

## P4. Database and migration changes

Schema v9 (additive, atomic, idempotent):
`eurus_domain_events.owner_id` and `eurus_event_subscriptions.owner_id`
(`NOT NULL DEFAULT ''`), unique index
`eurus_domain_events_dedup(owner_id, dedup_key) WHERE dedup_key IS NOT
NULL` (replacing the global one), indexes
`eurus_domain_events_owner(owner_id, occurred_at)` and
`eurus_event_subscriptions_owner(owner_id, enabled)`. No data is deleted or
rewritten; existing rows keep legacy global semantics. Verified by
`test_migration_from_v8_preserves_event_ownership_and_dedup`,
`test_v9_events_support_owner_scoped_queries`, and the existing
from-v7/interrupted/idempotency migration tests.

## P5. Event isolation semantics (implemented + tested)

Owner-scoped publish (CLI `--owner`; runtime events resolved from the
subject row), per-owner dedup, owner-scoped subscription matching
(`''`/`'*'` are the legacy and explicit-administrative scopes), scoped
outbox drain, delivery claiming/acknowledgement/failure/replay with
in-SQL ownership verification, scoped stats/listing, and job creation that
inherits the event's owner. Legacy unscoped events remain global by design
and are processed by any scoped daemon (outbox status guards
double-dispatch).

## P6. Learning isolation semantics (implemented + tested)

Owner-scoped pending retrieval, atomic owner-verified claiming, completion
and failure with owner guards, scoped stale requeue, scoped stats, scoped
inline processing, owner-verified `deactivate` (fail-closed when ownership
cannot be established), and lesson records written to the owner's private
memory scope. Sharing requires an explicit `shared=True` call with an owner
identity; automation never shares automatically. Sensitive content keeps
the existing memory privacy policy (rejection patterns unchanged).

## P7. Assignment isolation (implemented + tested)

`AssignmentStore.update` and `heartbeat` verify ownership and enforce the
owner predicate inside the guarded SQL statement; `get_scoped` added; the
supervisor retrieves stale assignments with the owner predicate in SQL;
assignment job callbacks verify the finishing job's owner matches.

## P8. Health and observability (implemented + tested)

Owner-scoped supervisor health (jobs, dead letters, event stats, learning
counts, stale assignments, concurrency snapshot), owner-scoped
`AutomationEngine.metrics()`, and the corrected `learning_processed`
counter. Administrative global views remain available via `owner_id=None`.

## P9. Side-effect and lease fencing (implemented + tested)

`mark_side_effect_started(job_id, worker_id, lease_version, *, now)`
raises on an empty worker and rejects wrong worker, wrong lease version,
and expired leases atomically. Failure-injection coverage: claim → expiry →
re-claim → old-worker mark/heartbeat/finish/retry all rejected, new worker
completes. The daemon marker forwards the claiming worker's identity and
refuses jobs without one.

## P10. Python 3.12 validation (executed)

Environment: Python **3.12.16**, pytest 9.0.2, ruff 0.17.3, mypy 2.4.1
(strict). Commands and results, run from the repository root with
`EURUS_HOME` unset:

| Command | Result | Exit |
| --- | --- | --- |
| `python -m pytest -q` | **2909 passed, 1 skipped** (Chromium browser test, skipped by design) | 0 |
| `ruff check src tests` | All checks passed | 0 |
| `ruff format --check src tests` | 600 files already formatted | 0 |
| `python -m mypy` (strict, `files=["src"]`) | Success: no issues found in 374 source files | 0 |
| `python -m compileall -q src tests` | clean | 0 |
| `python -m pytest tests/test_stabilization_migrations.py tests/test_migration_safety.py` | 13 passed | 0 |
| `python -m eurus autonomy --help`, `events publish/subscribe/stats`, `jobs dead-letters`, `health --owner` | all exit 0 | 0 |
| `python -m eurus autonomy daemon --poll-seconds 1 --workers 1` + SIGINT | starts, supervises, exits promptly on SIGINT | 0 |

CI already runs the same gates on Python 3.11/3.12/3.13
(`.github/workflows/ci.yml`); the malformed `push` trigger was repaired.

## P11. Multi-owner end-to-end verification

`tests/test_phase10_multi_owner.py` composes the real runtime (shared
SQLite database, per-owner `AutomationEngine`s, shared event bus, goals,
workflows, orchestrator, learning coordinator, per-owner supervisors) and
proves: concurrent execution of both owners' work, owner-scoped workers,
cross-owner goal/workflow/assignment control refusal, cross-owner
recovery/replay/dead-letter refusal, private lessons per owner, scoped
scheduler dispatch, scoped event dispatch with per-owner jobs, restart
preserving isolation, race-condition concurrency (6 jobs per owner in
parallel with zero cross-owner claims), and explicit administrative
operations. 48 of the 57 new behavioural tests fail against the pre-fix
implementation (verified via `git stash` before implementation).

## P12. Remaining limitations and deferred work

- Notification delivery has no built-in third-party provider; the
  `LoggingNotificationSink` is the production default and custom sinks are
  injectable. `notify` without a sink is explicitly unsupported (retried,
  then dead — never silently acknowledged). **Deferred**: a durable
  outbox-backed notification provider.
- Legacy unscoped events (`owner_id=''`) remain global by design for
  backward compatibility; a future major version may require owners.
- Lesson sharing is an explicit caller decision (`shared=True`); there is
  no per-workspace sharing UI. **Unsupported**: implicit lesson promotion
  to global memory.
- Exactly-once external side effects remain approximated (unchanged).
- In-flight specialists still do not emit autonomous heartbeats (unchanged).

## P13. Deliverable

Complete project archive: `eurus-phase10-stabilized.zip` (source, tests,
docs, migrations, configuration examples, project metadata; no secrets,
`.env`, runtime databases, caches, or virtual environments).

---

# Implementation Report — Agentic Engineering Upgrade (Hierarchical)

Append-only section for the Full Agentic Engineering Upgrade described in
`PRD.md` / `IMPLEMENTATION.md` (audited 3 November 2026). Every claim below is
backed by the test suite, ruff, and mypy runs recorded in §Evidence.

## 1. Scope executed

| Phase (plan §13) | Status | Delivered |
| --- | --- | --- |
| 0 — Baseline & security hardening | implemented + tested | Deny-by-default `ScopedToolRegistry` and `ExecutionPolicy` (empty allowlist ⇒ zero tools; explicit `*` wildcard only grant-all; blocked `*` denies all); explicit least-privilege allowlists for all built-in profiles (`agent/specialist_profiles.py`, 14 roles incl. the 5 legacy roles); registration/dispatch validation against the live tool catalog; specialist job payload allowlists resolved from the persisted profile via `PolicyEnforcer.specialist_profile_resolver` (payload escalation and tool-policy-version mismatch ⇒ `PolicyViolation`); reviewed migration of legacy empty-allowlist rows in `seed_builtin_specialists`. |
| 1 — Contracts & mission supervisor | implemented + tested | `multiagent/contracts.py` canonical contracts (Mission, TaskSpec, AssignmentContract, SpecialistResult, ArtifactRef, ReviewFinding, ReviewResult, VerificationEvidence, AcceptanceDecision); additive migration v12 (`eurus_agent_missions`, `eurus_agent_tasks`, `eurus_agent_task_dependencies`, `eurus_agent_artifacts`, `eurus_agent_reviews`, `eurus_agent_verifications`, specialist profile columns, assignment mission/task correlation); `agent/supervisor.py` `MissionStore` + `MainSupervisor` (create/plan/start/cancel/resume/report/finalize, direct-vs-hierarchical routing per FR-018). |
| 2 — Task DAG & hierarchical team leads | implemented + tested | `multiagent/task_graph.py` persisted DAG (deterministic ready queue by priority/task_key/created_at, cycle detection on write, owner-scoped queries, blocked propagation); `agent/task_decomposer.py` validated objective→DAG; `agent/team_coordinator.py` role selection (preferred role, then capability FR-023) and durable dispatch; `runtime/budgets.py::allocate_child_budget` hierarchical envelopes (children never exceed parent; delegation depth decreases downward). |
| 3 — Profiles, messages, artifacts | implemented + tested | Versioned profiles (`profile_version`, `tool_policy_version`, `risk_tier`, input/output schema, review policy, `max_delegation_depth`); `multiagent/communication.py` extended typed messages (`artifact_published`, `review_request`, `review_result`, `revision_request`, `blocker_report`, `decision_record`, `task_dependency_ready`); `multiagent/artifacts.py` immutable owner-scoped artifact store (sha256 integrity, per-scope versioning, explicit recipient sharing, size limit, bytes outside SQLite). |
| 4 — Coding workspace & integration | implemented + tested | `coding/worktree.py` base-commit recording, dirty-parent detection, path-scoped staging (`stage_paths`, no blanket `add -A`), `PatchMetadata` provenance; `coding/workspace_manager.py` one-worktree-per-assignment allocation/release; `coding/integration.py` integration-worktree application with `git apply --check` conflict detection, rollback ref, post-check hooks; `coding/conflict_resolution.py` classified conflicts + explicit resolution objective (no destructive overwrite). |
| 5 — QA, security, review & refinement | implemented + tested | `agent/acceptance.py` verification store (machine evidence: argv, exit code, duration, status, bounded output) + review store (severity/category/disposition findings) + `AcceptanceService` policy (open blocker/critical/major ⇒ blocked; failed check ⇒ blocked; missing/unavailable required check ⇒ `completed_unverified`; reviewer approval never overrides failing tests); `agent/review_loop.py` bounded cycles with revision-task creation and escalation with exact blockers. |
| 6 — Observability, API and UI | not in this pass | Backend correlation fields exist (`mission_id`/`task_id` on assignments, events, artifacts); REST/UI/CLI surfaces are explicitly out of scope of this pass and are listed as follow-up work. |
| 7 — Recovery, benchmarks, rollout | partially supported | Resume-from-persisted-state is implemented and tested (`MainSupervisor.resume_mission`); internal benchmark suite and dashboards remain follow-up work. |

Feature flags from plan §15 map to configuration as follows: the strict tool
scope is always on for `specialist` jobs (deny-by-default cannot be disabled);
legacy unrestricted behaviour for non-specialist scopes is granted explicitly
via the `TOOL_ALLOW_ALL` sentinel, never inferred from an empty set.

## 2. Components reused (not rewritten)

- `automation/engine.py`, `worker.py`, `storage/jobs.py` — durable execution,
  lease/fencing and idempotency untouched; the mission layer rides on top.
- `multiagent/orchestrator.py` — extended additively (mission/task correlation,
  profile version in payload); `agent/delegation.py`/`child_runtime.py` single
  delegation path preserved.
- `coding/worktree.py`, `agent/coder_tools.py` — isolation preserved; metadata
  and integration were added around them.
- No second job state machine: mission/task status is business workflow
  (ADR-002); assignment/job status remains the execution transport.

## 3. Intentional behavior changes (covered by tests)

1. Empty allowlist no longer means "all tools" anywhere: `ScopedToolRegistry`
   and `ExecutionPolicy` are deny-by-default (`tests/test_agentic_security.py`).
   Non-specialist scopes without a configured allowlist receive an explicit
   legacy trusted grant so historical automation behaviour is unchanged.
2. Specialist registration validates allowlist entries against the live tool
   catalog; wildcards are rejected. Test fixtures that used non-catalog tool
   names (`write_file`) were updated to real names (`create_file`).
3. `seed_builtin_specialists` now seeds the full versioned roster and migrates
   legacy rows with empty allowlists to the reviewed profile allowlists
   (`tests/test_specialist_seeding.py` updated to the expanded roster).
4. Migration version advanced 11 → 12; the delivery-migration test now asserts
   against `LATEST_SCHEMA_VERSION` instead of a hard-coded `11`.

## 4. Evidence

Commands run from the repository root (Python 3.12.16):

```
python -m pytest tests -q --ignore=tests/browser_benchmarks -k "not browser"
→ 2912 passed, 1 skipped, 0 failed (271.62s)

python -m mypy
→ Success: no issues found in 392 source files

ruff check src/eurus/ tests/test_agentic_security.py tests/test_mission_orchestration.py tests/test_coding_workspace_integration.py
→ All checks passed!
```

New test files (73 tests):

- `tests/test_agentic_security.py` — 23 tests: deny-by-default registry,
  policy semantics, profile binding/escalation/version mismatch, built-in
  profile allowlists vs live catalog, legacy row migration, migration v12
  schema (fresh + from v11 with data preserved).
- `tests/test_mission_orchestration.py` — 32 tests: mission lifecycle,
  owner isolation, DAG validation (cycles, missing deps, deterministic ready
  queue), dependency-aware parallel dispatch, cancellation propagation,
  resume, artifact ownership/integrity/versioning/sharing/size limits, review
  cycle bounds and escalation, evidence-backed acceptance (including
  "reviewer approval does not override failing tests").
- `tests/test_coding_workspace_integration.py` — 18 tests (real git): base
  commit + dirty-parent metadata, path-scoped staging, workspace manager
  lifecycle, clean patch integration into an integration worktree (primary
  workspace untouched), conflict detection without destructive overwrite,
  stale-base handling, pre-integration review rejection, post-checks,
  conflict classification.

PRD §10 acceptance mapping: items 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13 and
14 are exercised at the orchestration layer by the tests above; item 7
(specialist context minimisation) is enforced by the artifact sharing scope;
items requiring API/UI surfaces (dashboard views) remain follow-up work.

## 5. Follow-up work

- API/UI/CLI mission surfaces (plan §4.5, Phase 6) and event-stream correlation.
- Internal coding benchmark suite and cost/latency dashboards (Phase 7).
- `EngineeringManager` design-review gate and `agent/collaboration.py` facade
  over messages + artifacts (both P1 in the plan; the underlying message types,
  artifact store, review loop and acceptance service are in place).
- `coding/test_strategy.py` targeted-test selection and
  `coding/security_review.py` defensive review adapter.

---

# Addendum — Phases 5–7 completion (agentic engineering follow-up)

This addendum closes the follow-up list above. Every claim is backed by the
test suite, ruff, mypy strict, the frontend toolchain or the benchmark run
executed at the end of this phase.

## 1. Newly delivered components

| Plan item | Delivered |
| --- | --- |
| `coding/project_understanding.py` (P1) | bounded repository map: manifests, languages, test locations, docs, entry points, parse-error and partial-scan notes |
| `coding/test_strategy.py` (P1) | targeted/regression/static test plan from changed paths; every command passes the `coding/validation.py` allowlist; mirror test mapping with caps and fallback notes |
| `coding/security_review.py` (P1) | defensive static review (secrets, disabled TLS, shell injection, unsafe deserialization, SQL string building) over files, workspaces and diffs; findings convert to `ReviewFinding(category="security")`; no exploit execution |
| `agent/collaboration.py` (P1) | `CollaborationHub` facade: typed messages + immutable artifact publishing, sharing grants, review requests, blocker reports, decision records with source artifact ids, dependency-ready notices; mission/owner validation on every reference |
| `agent/engineering_manager.py` (P1) | deterministic plan review (roles, fan-in/fan-out, verification presence, plan size) and path-overlap conflict routing for concurrent assignments |
| `multiagent/supervision.py` (P1) | `MissionSupervision.tick()`: stale-heartbeat detection with bounded cancellation + requeue, restart reconciliation (vanished/terminal assignments), deadline expiry cancelling all descendants |
| Gateway mission API (Phase 6) | `POST/GET /api/missions`, detail, plan/start/cancel/resume/dispatch/finalize, tasks/artifacts/verifications/reviews; owner-scoped, keyed off the canonical durable orchestrator, with `[agentic_engineering]` kill-switch enforcement |
| CLI mission control (Phase 6) | `eurus missions create|list|show|tasks|plan|start|cancel|resume|dispatch|finalize` |
| Frontend mission control (Phase 6) | `/missions` page plus `MissionTree`, `AgentTaskPanel`, `ArtifactPanel`, `ReviewPanel` components; vitest coverage; typecheck and production build clean |
| Rollout flags (Phase 7) | `[agentic_engineering]` config section: `enabled`, `hierarchical_dispatch` (real kill switch), `strict_tool_scope` (posture assertion — loader rejects `false`), task/review bounds |
| Recovery tests (Phase 7) | restart/resume suite: completed work never re-dispatched, orphaned work requeued, artifacts/evidence/reviews survive restarts, deadline expiry after restart |
| Benchmark (Phase 7) | `benchmarks/bench_mission.py` — plan, promote, dispatch, artifact round-trip, review cycle medians; baseline written to `benchmarks/results/bench_mission.json` |
| Architecture docs (Phase 7) | `agentic-engineering.md`, `specialist-profiles.md`, `security-boundaries.md` |

## 2. Test evidence (this phase)

Command: `python -m pytest tests/ -q --ignore=tests/office_benchmarks`

- **3,118 tests collected, 3,117 passed, 1 skipped** (browser integration,
  skipped by design without Chromium). One TUI test flaked once under full-suite
  load and passes reliably in isolation and with its file (43/43); it covers
  code untouched by this phase.
- New test modules this phase: `test_agentic_tooling.py` (36),
  `test_mission_recovery.py` (7), `test_cli_missions.py` (8),
  `test_web_mission_api.py` (10), `test_config.py` additions (6).
- Frontend: `vitest` **65/65 passing** across 10 files; `tsc --noEmit` clean;
  `vite build` succeeds.
- `ruff check src tests benchmarks`: all checks passed.
- `mypy` (strict, 399 files): no issues found.
- `python scripts/check_docs_links.py`: OK.

## 3. PRD §10 acceptance — remaining items closed

Items 1–14 are now exercised at the orchestration layer or through the new
API/UI surfaces; item 15 (existing tests remain passing) is covered by the
full-suite result above. The mission tree, evidence and review findings are
visible in the web UI (`/missions`), the gateway API and the CLI, satisfying
the observability requirements (FR-020, FR-021).
