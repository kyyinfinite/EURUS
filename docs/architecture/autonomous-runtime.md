# EURUS Autonomous Runtime — Post-Evolution Architecture

This document describes the architecture **after** the eight-phase evolution.
The starting point and the weaknesses that drove each change are recorded in
`autonomous-runtime-audit.md`. Everything described here exists in the source
tree and is covered by tests; module and table names are the real ones.

## 1. Component map

| Area | Modules | Responsibility |
| --- | --- | --- |
| Central agent loop | `agent/loop.py`, `agent/context.py`, `agent/history.py` | Unchanged reasoning loop: model calls, tool batches, planning, verification |
| Execution engine | `runtime/engine.py` (`AgentEngine`) | Unchanged authoritative run lifecycle, budgets, checkpoints, attempt journal |
| Runtime wiring | `runtime/wiring.py` | Shared engine/store construction reused by gateways and automation |
| Automation | `automation/engine.py` (`AutomationEngine`), `worker.py`, `executor.py`, `retry.py`, `policies.py`, `concurrency.py`, `reconciliation.py`, `notifications.py`, `supervision.py` | Durable job system: submit, lease, execute through the Agent Engine, retry, dead-letter, reconcile, supervise |
| Scheduler | `scheduler.py` (`ScheduleStore`), `cron.py` (`CronExpression`) | Occurrence calculation (once/interval/cron/event) and durable dispatch into the job queue |
| Goals | `goals/engine.py` (`GoalEngine`), `store.py`, `states.py`, `tools.py` | Persistent objectives, decomposition, task dependency execution, replanning, evaluation |
| Workflows | `workflows/engine.py` (`WorkflowEngine`), `definition.py`, `store.py`, `states.py` | Durable DAG execution with 13 node types, approvals, compensation, subworkflows |
| Multi-agent | `multiagent/registry.py` (`SpecialistRegistry`), `orchestrator.py` (`AgentOrchestrator`), `communication.py` | Specialist registration, durable assignments, heartbeats, reassignment, aggregation |
| Events | `events/bus.py` (`EventBus`), `store.py` (`DomainEventStore`) | Domain events with transactional outbox, subscriptions, delivery retry, dead-letter |
| Learning | `learning/evaluation.py` (`EvaluationEngine`, `LearningPipeline`, `LessonValidator`) + existing `learning/*`, `trajectory.py` | Deterministic evaluation, provenance, reversible lessons, trajectory collection |
| Interfaces | `cli/autonomy.py`, `cli/schedule.py`, `tui/*`, `gateway/*` | CLI/TUI/Telegram/Web entry points; `eurus autonomy` management surface; unified daemon |

## 2. The single execution path

```
CLI / TUI / API / Telegram          (interactive)
schedule occurrence                 (time trigger)
goal tick / workflow tick           (progression triggers)
event delivery                      (reaction trigger)
specialist dispatch                 (delegation trigger)
        |
        v
AutomationEngine.submit()  ------> eurus_jobs (durable queue)
        |
AutomationWorkerPool.run_once()     (claim + lease + heartbeat)
        |
AgentJobExecutor.execute()          (automation/executor.py)
        |-- build_router / MCP / default_registry
        |-- ScopedToolRegistry        (policy allow/block enforcement)
        |-- MutatingToolMarker        (deferred side-effect boundary)
        |-- Run + AgentEngine.run()   (the one authoritative engine)
        v
AgentEngine checkpoints / tool-attempt journal / verification
        |
AutomationEngine._handle_job_finished()
        |-- retry decision (RetryManager + failure classification)
        |-- dead-letter recording
        |-- on_job_finished callbacks: GoalEngine, WorkflowEngine, AgentOrchestrator
        |-- domain events -> EventBus -> subscriptions -> new jobs
        v
RuntimeSupervisor.supervise()       (recovery, stuck detection, progression)
```

No subsystem runs its own reasoning loop. The scheduler, event bus, goal
engine, workflow engine, and orchestrator only create durable jobs or advance
persisted state machines; every job executes through `AgentJobExecutor`, which
delegates to `AgentEngine.run()`.

## 3. Ownership boundaries

| Subsystem | Owns | Must not do |
| --- | --- | --- |
| AutomationEngine | `eurus_jobs`, worker pool, retry, dead letters | Reason, plan, or call models |
| ScheduleStore | `eurus_schedules`, `eurus_schedule_occurrences`, `eurus_schedule_runs` | Execute agent work |
| GoalEngine | `eurus_goals`, `eurus_goal_tasks`, `eurus_goal_milestones` | Execute tasks directly |
| WorkflowEngine | `eurus_workflow_*` tables | Execute nodes directly |
| AgentOrchestrator | `eurus_agent_specialists`, `eurus_agent_assignments`, `eurus_agent_heartbeats`, `eurus_agent_messages` | Spawn engines outside automation |
| EventBus | `eurus_domain_events`, `eurus_event_subscriptions`, `eurus_event_deliveries`, `eurus_outbox` | Run LLM logic in the transport |
| EvaluationEngine | `eurus_evaluations`, `eurus_learning_provenance` | Trust unvalidated lessons |

## 4. Persistence model

One SQLite file (`~/.eurus/data/eurus.sqlite3`) with WAL, migrations v1–v8 in
`storage/migrations.py`:

- v1–v5 (pre-existing): runs, checkpoints, runtime events, jobs, run controls,
  tool attempts.
- v6 (evolution): job `priority`/`source`/`correlation_id`, dead letters,
  goals/goal tasks/milestones, workflow definitions/instances/node
  states/approvals, specialists, assignments, heartbeats, domain events,
  subscriptions, deliveries, outbox, evaluations, learning provenance.
- v7 (evolution follow-up): `eurus_workflow_instances.child_instance_id`,
  `eurus_workflow_node_states.created_at/updated_at`, assignment
  `owner_id`/`delegation_depth`, `eurus_agent_messages`.
- v8 (stabilization): unique partial index on open dead letters per job
  (with legacy dedup), `eurus_learning_tasks` (+ indexes), assignment owner
  index.

Stores accept `Database | DatabasePath`, keep SQL to their own tables, and use
the pooled `Database` connection helper, so a PostgreSQL port only needs the
`Database` layer. The scheduler and trajectory store retain their own SQLite
files by pre-existing design; they are documented in
`operations-guide.md`.

## 5. Distrust-of-failure design rules

1. **Lease fencing.** Every job mutation carries `worker_id` +
   `lease_version`; stale workers cannot commit.
2. **Deferred side-effect boundary.** `side_effect_started` is set when the
   run's first mutating tool call executes (`MutatingToolMarker`), so a lease
   expiry before any mutation requeues safely; after it, the job becomes
   `unknown_outcome` and reconciliation surfaces it.
3. **Deterministic job identities.** Workflow nodes
   (`wf-<instance>-<node>-<attempt>`) and event deliveries
   (`evt-<event>-<subscription>`) derive job ids from their subjects, making
   redelivery after a crash idempotent.
4. **Compare-and-set state transitions.** Goal status saves are guarded by
   `status_version`; workflow instance transitions are guarded by expected
   status; approval decisions are single UPDATE ... WHERE decided_at IS NULL.
5. **Honest unknowns.** `unknown_outcome` is a first-class job status with
   dead-letter records and reconciliation, never a silent success.

## 6. Interface compatibility

- The `eurus` and `eurus-tui` entry points, all pre-existing CLI commands, the
  Telegram and Web gateways, MCP, browser, providers, memory and skills are
  unchanged in behavior.
- `eurus schedule daemon` keeps its options; it now runs the unified runtime
  (`cli.autonomy.run_daemon`) instead of a CLI-local closure.
- New management surface: `eurus autonomy {goals,workflows,agents,events,jobs,health,daemon}`.

## 7. Testing posture

- Unit and integration tests live in `tests/`; the full suite is 2,851 tests
  (2,850 passing, 1 environment-dependent browser skip).
- Failure injection: lease expiry, worker crash simulation, duplicate claims,
  restarts mid-flight, retry exhaustion, unknown outcomes, cancellation races.
- End-to-end scenarios A–H from the evolution brief are implemented in
  `tests/test_e2e_scenarios.py`.
- Quality gates: `ruff check`, `ruff format --check`, and `mypy` (strict) all
  pass.

## 8. Stabilization architecture decisions

The stabilization pass (see `stabilization-checklist.md` for the defect
list) changed the runtime in five structural ways; everything else was
repaired in place.

1. **Composition root ordering.** `build_stack` now constructs, in order:
   `Database` → `ScheduleStore`/`TrajectoryStore` → `AgentJobExecutor`
   (with the deferred side-effect marker factory bound to the engine's
   `JobStore`) → `AutomationEngine` → `EventBus(automation=...)` → goal /
   workflow engines → orchestrator → `LearningCoordinator` →
   `RuntimeSupervisor`. Partially initialized objects cannot leak into
   workers: the side-effect marker raises if used before wiring completes.
2. **Owner scope is a runtime-wide boundary.** The scoped claim path is
   `ConcurrencyGovernor.claim` (constructed with the daemon's owner);
   `JobStore.claim`/`recover_expired` take `owner_id` and unscoped use is
   the explicit administrative mode. Goals, workflows, assignments,
   schedules (chain references), and event handlers propagate owner scope;
   storage mutations accept an owner guard.
3. **Event amplification guard.** `TASK_*` domain events are not dispatched
   to `submit_job` subscriptions unless the subscription explicitly opts in
   (`allow_task_events`), eliminating the event→job→event loop observed
   with wildcard subscriptions.
4. **Learning lifecycle.** Job completions enqueue durable
   `eurus_learning_tasks` rows (unique per run) processed inline (bounded)
   or by the supervisor tick; crash recovery requeues stale tasks; only
   validated, secret-screened, deduplicated lesson candidates reach the
   lesson store, always with provenance that supports reversal.
5. **Supervisor observability.** Each subsystem run is wrapped explicitly:
   failures are logged with context, tracked (consecutive/total, last
   error, last success), and put on bounded exponential backoff; `health()`
   reports healthy/degraded/unhealthy plus worker liveness, queue depths,
   dead letters, learning stats, and per-subsystem state.
6. **Event and learning ownership.** Domain events, subscriptions, and
   deliveries carry `owner_id` (schema v9) with per-owner dedup; the bus
   publishes runtime events through an owner-resolving adapter and the
   supervisor dispatches events per owner. The learning coordinator is
   owner-scoped end to end, and automation lessons are private to their
   owner unless explicitly shared.
7. **Fenced side-effect boundary.** The marker requires the claiming
   worker's identity, lease version, and an unexpired lease in one guarded
   UPDATE; the composition root forwards the worker identity.

## 9. Implementation status vocabulary

- **Implemented + verified**: everything in this document — each claim has
  a test (see the test file references throughout the docs).
- **Implemented, partially verified**: notification fan-out through the
  `LoggingNotificationSink` in daemon deployments (no built-in third-party
  notification provider exists; a `notify` delivery without a configured
  sink is retried and dead-lettered with a visible error, never silently
  acknowledged), and gateway-level (Telegram/Web) integrations of the
  owner-scoped commands.
- **Deferred**: relative-time and conditional scheduler triggers;
  cross-process leader election for multi-daemon reconciliation.
- **Unsupported**: condition-predicate event subscriptions (event filters
  remain type/subject based); automatic dead-letter replay of
  non-idempotent jobs (replay is an explicit operator action).
