# EURUS Autonomous Runtime — Architecture Audit (Phase 0)

Audit of the EURUS codebase as uploaded, performed before the autonomous-runtime
evolution. This document describes the architecture that existed **before** the
eight-phase evolution, the weaknesses found, and the decisions that governed the
implementation. The post-evolution architecture is described in
`autonomous-runtime.md`.

## 1. Current Architecture

### 1.1 Component inventory

| Area | Modules | Responsibility |
| --- | --- | --- |
| Central agent loop | `agent/loop.py` (`Agent`), `agent/context.py`, `agent/history.py` | Streaming agent loop: model calls, tool batches, planning, verification, learning hooks |
| Execution engine | `runtime/engine.py` (`AgentEngine`) | Durable run lifecycle, budgets, checkpoints, tool-attempt journaling, verification, cancellation |
| Runtime application | `runtime/application.py` (`AgentRuntime`) | Session-scoped agent cache + engine facade used by both gateways |
| Run state | `runtime/models.py`, `runtime/state_machine.py`, `runtime/run_state.py` | `Run` records, strict status transitions, rendered run state |
| Recovery / resume | `runtime/recovery.py`, `runtime/resume.py` | Failure classification, recovery actions, checkpoint-based resume |
| Budgets | `runtime/budgets.py`, `runtime/token_governor.py`, `runtime/pricing.py` | Iteration/tool/token/cost/deadline budgets |
| Delegation | `agent/delegation.py`, `agent/child_runtime.py`, `agent/coder_tools.py`, `agent/team_tools.py` | Read-only subagents, worktree-confined coder agents, 3-role document review |
| Delegation (unified) | `agent/delegate.py` | Single `delegate` tool over subagent, coder, document-review and office modes |
| Scheduler | `scheduler.py` (`ScheduleStore`) | `once`/`interval` schedules, due dispatch into `eurus_jobs` |
| Automation worker | `automation/worker.py` (`AutomationWorkerPool`) | Claim/heartbeat/finish loop over `JobStore` |
| Job store | `storage/jobs.py` | Durable queue: leases, fencing, side-effect boundary, unknown-outcome |
| Gateway tasks | `gateway/tasks.py`, `gateway/manager.py` | Telegram/Web durable tasks with retries and resume gating |
| Memory | `sessions.py` (`SessionStore`), `memory/*` | FTS5 memories with trust levels, hybrid recall, compaction lineage |
| Learning | `learning/*` | Lesson store, reflector, skill drafts, candidate validation, regression reports |
| Skills | `skills/*`, `learning/skills.py` | Discovery, quarantine, promotion, patching |
| Verification | `agent/verification.py`, `runtime/contracts.py` | Deterministic completion/step verification |
| Trajectory (dark) | `trajectory.py` | Redacted trajectory store — complete but unwired |
| Interfaces | `cli/*`, `tui/*`, `gateway/telegram/*`, `gateway/web/*` | CLI, TUI, Telegram, Web entry points |

### 1.2 Execution flow (before evolution)

```
CLI run/chat ─────────────┐
Telegram gateway ─────────┤
Web gateway ──────────────┼─► AgentRuntime ─► AgentEngine.run() ─► Agent loop ─► tools
schedule daemon ──────────┘   (gateways)        │
   │                                            ├─ RunStore / EventStore / ToolAttemptStore
   ├─ ScheduleStore.dispatch_due() ─► eurus_jobs│
   └─ AutomationWorkerPool.execute_job closure ─┘ (rebuilds router+MCP+agent+engine per job)
```

The scheduled path executes through `AgentEngine` (good), but the wiring lives
entirely inside the `schedule daemon` CLI command closure — there is no reusable
automation service, no retry policy, no dead-letter handling, and schedule-run
reconciliation is a compensating loop inside the same closure.

### 1.3 Persistence model (before evolution)

One SQLite file (`~/.eurus/data/eurus.sqlite3`), five schema owners:

- `storage/migrations.py` v1–v5: `eurus_runs`, `eurus_run_checkpoints`,
  `eurus_runtime_events`, `eurus_jobs`, `eurus_run_controls`,
  `eurus_tool_attempts`, `eurus_schema_migrations`.
- `scheduler.py`: `eurus_schedules`, `eurus_schedule_runs`, `eurus_task_profiles`.
- `reminders.py`: `eurus_reminders`.
- `sessions.py`: sessions/messages/memories + FTS5.
- `gateway/tasks.py`: `gateway_tasks` and gateway session tables.

Two connection stacks coexist: the pooled `Database` (jobs/runs/events/attempts)
and direct `storage.sqlite.connect` (schedules/reminders/sessions/tasks). WAL is
enabled by both.

## 2. Existing reusable components (retained by the evolution)

- `AgentEngine` — already the authoritative execution funnel with checkpoints,
  budgets, verification, attempt journaling. **Kept as the single execution path.**
- `JobStore` — lease/fencing/heartbeat/side-effect-boundary semantics are sound.
  Extended with priority, retry scheduling, replay, and dead-letter records.
- `RunStore`/`EventStore`/`ToolAttemptStore` — run persistence reused directly.
- `runtime/state_machine.py`, `runtime/recovery.py` — transition tables and
  failure classification reused by the retry manager and goal/workflow engines.
- `agent/delegation.py` + `child_runtime.py` — kept for single delegation; the
  multi-agent orchestrator builds on the same durable child-run pattern.
- `orchestration/graph.py` + `graph_runtime.py` were removed; the DAG/harness
  semantics survive conceptually in the workflow engine (dependency satisfaction,
  skip propagation, restore semantics) over a durable store.
- `learning/*` validation/quarantine machinery — reused by the learning pipeline.
- `trajectory.py` redaction — reused for trajectory collection.

## 3. Architectural weaknesses found

1. **Automation wiring is CLI-local.** `execute_job`/`finish_job` closures in
   `cli/schedule.py` are the entire automation layer: no service object, not
   reusable by gateways, events, goals, or workflows.
2. **No retry policy.** `eurus_jobs.max_attempts` exists but nothing ever
   re-queues failed jobs; `available_at` (retry scheduling) is never advanced.
   Failed jobs are terminal, silently.
3. **No dead-letter or replay.** Exhausted/unknown-outcome jobs have no
   operator workflow.
4. **Side-effect boundary marked too early.** `AutomationWorkerPool` calls
   `mark_side_effect_started` immediately after claim, so any lease expiry —
   even before any mutation — becomes `unknown_outcome` instead of a safe
   requeue.
5. **Scheduler limitations.** Only `once`/`interval`; no cron, no calendar or
   event triggers, no misfire policy, no per-schedule concurrency, no priority,
   no occurrence identity (dedup relies on `UNIQUE(schedule_id, scheduled_at)`
   on both jobs and schedule runs), no revision history, no execution windows,
   no expiry or occurrence caps.
6. **Three parallel durable-work systems** (`eurus_jobs`, `gateway_tasks`,
   `eurus_schedule_runs`) with overlapping semantics and split reconciliation.
7. **`orchestration/` was dark** — complete, tested, but no production path ever
   invoked it; it has since been removed in favour of the unified `delegate` tool.
8. **No persistent goals, workflows, specialist assignments, or domain events.**
   EURUS cannot hold an objective across restarts, coordinate a dependency
   graph, fan out to specialists, or react to its own lifecycle events.
9. **`JobStore.enqueue` never called in production**; scheduler hand-writes
   `INSERT OR IGNORE INTO eurus_jobs` through a different connection stack.
10. **Attribution mismatch:** gateway-driven runs persist `owner_id="local"`.
11. **Duplicate agent/engine wiring** across six call sites with small
    variations (approver, stream, learn flags, store freshness).
12. **Store-construction churn:** `_engine_stores()` opens a new `Database`
    (migrations + pool) per engine construction in gateways.

## 4. Dependency graph (before evolution)

```
cli/schedule.py ─► scheduler.ScheduleStore ─► eurus_jobs (raw SQL)
cli/schedule.py ─► automation.AutomationWorkerPool ─► storage.jobs.JobStore
cli/schedule.py ─► agent.factory.build_agent ─► runtime.engine.AgentEngine
gateway/telegram ─► runtime.application.AgentRuntime ─► AgentEngine
gateway/web ─► AgentRuntime ─► AgentEngine
AgentEngine ─► RunStore, EventStore, ToolAttemptStore, CheckpointStore
agent/delegation ─► child_runtime ─► AgentEngine (child runs)
(orchestration: removed; trajectory: no inbound edges)
```

Circular-dependency check: none found at import time; the risks were semantic
(CLI owning business logic, scheduler owning job-table SQL).

## 5. Migration strategy

- All new tables ship as **migration v6** in `storage/migrations.py`, applied by
  the existing `Database` constructor — atomic, versioned, idempotent, safe on
  existing installs (verified by the migration-safety test pattern).
- `eurus_schedules` gains columns through additive `ALTER TABLE` guarded by
  `PRAGMA table_info` checks inside `ScheduleStore` (the established pattern
  used by `gateway/tasks.py`), because that table is owned by the scheduler's
  own schema script rather than the migration framework.
- `eurus_jobs` gains `priority` and `source` columns via v6 (table is owned by
  migrations).
- No existing column is removed or retyped; no table is dropped; existing rows
  satisfy every new column's default. Rollback = drop the v6 tables and the
  added columns (documented in `migration-guide.md`).
- Storage stays SQLite/WAL on one file. Every new store accepts
  `Database | DatabasePath` and confines SQL to its own tables, so a future
  PostgreSQL port only needs the `Database` layer (see `operations-guide.md`).

## 6. Implementation decisions

1. **One execution path.** A new `automation` package owns a reusable
   `AutomationEngine`; every trigger (schedule, goal, workflow, event, CLI,
   specialist assignment) submits `eurus_jobs` rows and workers execute them
   through `AgentEngine` via a single injectable `JobExecutor`. The CLI daemon
   becomes a thin wrapper.
2. **Triggers are not intelligence.** Scheduler, event bus, and goal engine
   never call models directly; they create jobs (or durable occurrences that
   create jobs).
3. **Honest uncertainty.** Job-level side-effect marking is deferred until the
   underlying run records its first mutating tool attempt; lease expiry before
   that requeues safely, after it becomes `unknown_outcome` with reconciliation
   tooling.
4. **Reuse over rewrite.** Existing state machines, failure classification,
   delegation, learning validation, and verification are reused; nothing
   working was replaced.
5. **Zero comments in new code**, per engineering standards; existing files
   keep their comments and only receive additive, comment-free edits.
