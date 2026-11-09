# EURUS Stabilization — Implementation Checklist (Phase 0 audit output)

## Confirmed defects

| # | Severity | Defect | Location |
| --- | --- | --- | --- |
| D1 | critical | `build_stack()` constructs `EventBus` without the `AutomationEngine`; every `submit_job` subscription fails with "automation engine unavailable" and dead-letters | cli/autonomy.py:102 |
| D2 | critical | Production daemon constructs `AgentJobExecutor` without `side_effect_marker_factory`, so `defer_side_effect_marking` is False and the side-effect boundary is marked at claim time; crashes before any tool call become `unknown_outcome` instead of a safe requeue | cli/autonomy.py:104, automation/worker.py:61 |
| D3 | critical | `JobStore.claim()` has no owner filter: a daemon scoped to owner A claims owner B's jobs | storage/jobs.py:214 |
| D4 | critical | `JobStore.recover_expired()` has no owner filter; also called unscoped from the worker poll loop (automation/worker.py:130) and supervisor — cross-owner recovery | storage/jobs.py:354 |
| D5 | high | `RuntimeSupervisor.supervise()` never forwards `owner_id` to goals tick, workflows tick/recover, or assignment recovery | automation/supervision.py:85-97 |
| D6 | high | Lease-exhausted failures (recovered as `failed` by `recover_expired`) bypass dead-letter creation, notifications and TASK_FAILED publication | storage/jobs.py:372, automation/reconciliation.py |
| D7 | high | `model_policy` is stored on specialists but never influences model selection | multiagent/orchestrator.py, automation/executor.py |
| D8 | high | Learning pipeline (`EvaluationEngine`/`LearningPipeline`) is not wired into the automation completion path at all | learning/* |
| D9 | medium | Daemon supervision loop swallows every exception (`contextlib.suppress(Exception)`) with no logging, failure tracking or backoff | cli/autonomy.py:185 |
| D10 | medium | Dead-letter creation is not race-safe (check-then-insert, no unique index) → duplicates under concurrent recovery | storage/jobs.py:440 |
| D11 | medium | Worker auto-retries jobs whose side-effect boundary was already crossed → possible duplicate mutations | automation/worker.py:79-98 |
| D12 | medium | `AssignmentStore.list_assignments` / orchestrator capacity / supervisor stale-assignment recovery are owner-blind | multiagent/orchestrator.py |
| D13 | medium | Scheduler `occurrence_finalizer` never wired; occurrence rows stay `dispatched` after job completion | cli/autonomy.py:105 |
| D14 | low | Scheduler lacks chained (dependency) triggers, explicit pause/resume; event schedules bypass TASK_CREATED publication (accepted) | scheduler.py |
| D15 | low | Stale-worker commit failures (fenced `finish`) are dropped silently | automation/worker.py:109 |
| D16 | low | `AgentOrchestrator._goal_job_running`-style scans and `_has_capacity` count across owners | multiagent/orchestrator.py:502 |

## Resolution status (post-implementation)

All 16 defects are repaired and regression-tested; the fix plan below is
retained as the record of intent. D14 was discovered during end-to-end
validation (event amplification through wildcard subscriptions) and added
to the original defect list. See `implementation-report.md` §S1–S4 for the
root-cause table, verification commands, and remaining boundaries.

## Fix plan (phase → items)

- Phase 1: D1, D2 (+ integration tests through `build_stack`)
- Phase 2: D3, D4, D5, D12, D16 (+ adversarial two-owner tests)
- Phase 3: D6, D10, D11, D15, lease-fencing tests, reconciliation coverage
- Phase 4: D9, supervisor health/liveness/backoff, observability metrics
- Phase 5: composition root ordering, shared Database ownership, deterministic shutdown
- Phase 6: D7 (+ routing tests incl. fallback/unavailable provider)
- Phase 7: D8 (+ durable learning tasks, dedup, provenance)
- Phase 8: D14 (+ chain triggers with real semantics, pause/resume)
- Phase 9: E2E scenarios A–H against the production composition

---

# Phase-10 audit output (final isolation, fencing & production verification)

Independent re-audit of the post-stabilization tree. Every finding below was
verified against the current source before implementation; the previously
repaired defects (D1–D16) were confirmed still fixed.

## Confirmed residual defects

| # | Severity | Defect | Location |
| --- | --- | --- | --- |
| P10-1 | critical | The event layer had no owner concept: `DomainEventStore` outbox drain, delivery retrieval, claiming, acknowledgement, retry, dead-letter listing/replay, and stats were global; the daemon-supervised dispatch processed every owner's events and `submit_job` created jobs from the subscription config regardless of event ownership | events/store.py, events/bus.py, automation/supervision.py |
| P10-2 | critical | The domain-event dedup index was global on `dedup_key`, so the same external event id from two owners collided | storage/migrations.py (v6 index) |
| P10-3 | critical | `LearningCoordinator` was owner-blind: `pending_tasks`, stale requeue, claim, complete, fail, and stats were global; the supervisor forwarded no owner to the learning tick | learning/integration.py, automation/supervision.py |
| P10-4 | critical | `LessonStore` was scope-blind: lessons derived from owner-scoped run events were stored globally and visible to every owner/workplace | learning/lessons.py |
| P10-5 | critical | The side-effect marker passed `None` as the worker identity and `mark_side_effect_started` ignored lease expiry and tolerated a missing worker, so an expired-lease worker could mark the boundary | cli/autonomy.py, storage/jobs.py |
| P10-6 | high | `LearningCoordinator.deactivate` had no owner verification: any caller could reverse another owner's artifacts | learning/integration.py |
| P10-7 | high | Supervisor stale-assignment recovery retrieved stale assignments globally (cross-owner metadata pulled, Python-only filter) and `AssignmentStore.update`/`heartbeat` performed no ownership verification | automation/supervision.py, multiagent/orchestrator.py |
| P10-8 | high | Owner-scoped health/metrics leaked cross-owner metadata: supervisor `health()` used global job/dead-letter/event/assignment queries; `AutomationEngine.metrics()` counted dead letters globally; `ConcurrencyGovernor.snapshot()` exposed other owners' counts | automation/supervision.py, automation/engine.py, automation/concurrency.py |
| P10-9 | high | `build_stack` created the `EventBus` without any notification sink; `notify` deliveries always failed permanently, no explicit unsupported state was documented, and `_notify` silently acknowledged when an async sink was used without a running event loop | cli/autonomy.py, events/bus.py |
| P10-10 | medium | `resolve_dead_letter` replayed jobs without an owner constraint (cross-owner dead-letter replay) | storage/jobs.py |
| P10-11 | medium | `supervise()` learning counter assigned the whole `LearningTickReport` object to an int field | automation/supervision.py |
| P10-12 | low | CLI `autonomy jobs resolve` defaulted to an invalid resolution value (`acknowledged`) and offered no `--owner` | cli/autonomy.py |
| P10-13 | low | CI `push` trigger had a malformed `branches:` value | .github/workflows/ci.yml |
| P10-14 | process | Python 3.12 validation had not been independently executed (previous audit ran 3.13) | environment |

## Already fixed (verified still in place)

Owner-scoped claim/recovery (D3/D4), supervisor owner forwarding to
goals/workflows (D5), dead-letter idempotency + unique index (D10),
side-effect retry guard (D11), assignment listing/capacity scoping (D12),
occurrence finalizer wiring (D13), event amplification guard (D14),
fenced-commit observability (D15), model-policy routing (D7), durable
learning lifecycle with dedup (D8), daemon backoff and explicit failure
logging (D9).

## Required source changes (all applied)

- `storage/migrations.py` — v9: event/subscription `owner_id`, per-owner
  dedup index, owner indexes.
- `events/store.py` — owner columns, scoped publish/retrieval/ack/fail/
  replay/stats/management, `subscription_accepts` matching rule.
- `events/bus.py` — `dispatch_tick(owner_id=...)`, event-owner-preserving
  handlers, no-silent-ack async handling.
- `storage/jobs.py` — fenced `mark_side_effect_started`, owner-scoped
  dead-letter resolution.
- `learning/integration.py`, `learning/lessons.py`,
  `learning/evaluation.py` — owner-scoped learning lifecycle and
  private/shared lesson visibility.
- `multiagent/orchestrator.py` — ownership-verified assignment
  update/heartbeat/get, owner-checked job callbacks.
- `automation/supervision.py`, `automation/engine.py`,
  `automation/concurrency.py` — owner forwarding and scoped health/metrics.
- `cli/autonomy.py` — worker-identity marker, owner-resolving event
  publisher, shared notification sink, `--owner` CLI options, valid
  resolution choices.
- `.github/workflows/ci.yml` — fixed push trigger (3.12 already in matrix).

## Required regression tests (all added)

`tests/test_phase10_event_isolation.py` (15),
`tests/test_phase10_learning_isolation.py` (17),
`tests/test_phase10_fencing_observability.py` (15),
`tests/test_phase10_multi_owner.py` (10), plus v9 migration tests in
`tests/test_stabilization_migrations.py` (2). 48 of the 57 behavioural
tests were verified to fail against the pre-fix implementation and pass
against the corrected one (the remainder pin compatible behavior).
