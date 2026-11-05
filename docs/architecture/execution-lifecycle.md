# Execution Lifecycle

How a unit of work travels from trigger to durable completion, and what each
state means. All states are persisted; timers exist only inside workers, never
as the source of truth.

## 1. Job lifecycle (`eurus_jobs`)

```
queued --claim--> running --finish--> completed
  ^                  |
  |                  |--retryable failure-- schedule_retry --> queued (available_at = now + backoff)
  |                  |--permanent failure--> failed --> dead letter
  |                  |--cancel requested---> cancelled
  |                  +--lease expires------> requeued | unknown_outcome | failed
  +--replay (manual)---------------------------^
```

- `claim(worker, lease_seconds)` atomically flips `queued` to `running` with a
  fresh `lease_version`; the old worker's later `finish`/`heartbeat` is
  rejected by the fencing token.
- `heartbeat` extends `lease_expires_at` and refreshes `updated_at`; the
  supervisor treats a running job with no progress for `stuck_after_seconds`
  as stuck and requests cancellation.
- `schedule_retry` keeps `attempts` and sets `available_at` in the future;
  `RetryManager` decides via `runtime/recovery.classify_failure_kind`
  (transient failures retry, policy and permanent failures do not).
- `unknown_outcome` marks side effects that may or may not have happened; the
  reconciliation service reports them and operators resolve them from the
  dead-letter view.

## 2. Goal lifecycle (`eurus_goals`)

```
created -> analyzing -> planning -> active -> executing -> evaluating -> completed
                |          |          |           |             |
                |          |          |           |             +-> replanning -> active
                |          |          +-> waiting (paused) -----+
                |          +-> replanning (task failure)
                +-> failed / cancelled (terminal from any non-terminal state)
```

- `GoalEngine.tick()` advances every non-terminal goal once per pass:
  CREATED submits a planning job, PLANNING applies a captured plan, ACTIVE
  submits ready tasks (dependency + concurrency gated), EVALUATING closes or
  replans.
- Plans arrive through the `goal_submit_plan` agent tool (validated: unique
  titles, known dependencies, bounded size). Evaluations arrive through
  `goal_submit_evaluation` when the goal runs in agent-evaluation mode.
- A failed task triggers `_maybe_replan`; when the replan budget is exhausted
  the goal FAILS with evidence. Applying a new plan cancels superseded open
  tasks and their jobs.
- WAITING goals burn no jobs; progression resumes on the next tick after
  `resume`.

## 3. Workflow lifecycle (`eurus_workflow_instances` / `eurus_workflow_node_states`)

```
instance: pending -> running -> (waiting_approval | waiting_schedule) -> completed | failed | cancelled
              |          +-> paused -> running | cancelled
node:       pending -> running | waiting_approval | waiting_schedule
            -> succeeded | failed | skipped | cancelled
```

- The engine dispatches PENDING nodes whose dependencies are terminal and
  whose `only_if` branch conditions match; a FAILED/CANCELLED/SKIPPED
  dependency skips the dependent node (skip propagation).
- Action nodes (agent/tool/specialist tasks) submit durable jobs with
  deterministic ids; delay/schedule nodes submit jobs with a future
  `available_at` and hold the node in WAITING_SCHEDULE.
- Approval nodes create `eurus_workflow_approvals` rows; `decide_approval` is
  idempotent for repeated identical decisions and resumes the instance.
- Subworkflow nodes launch a child instance and complete when the child
  reaches a terminal state.
- On FAILED/CANCELLED, the engine submits compensation jobs for SUCCEEDED
  nodes that declare `compensation` config.

## 4. Assignment lifecycle (`eurus_agent_assignments`)

```
pending -> dispatched -> running -> succeeded | failed | cancelled
                          |
                          +-> reassigned (superseded by a replacement assignment)
```

- Dispatch is capacity-gated per specialist (`max_concurrent`), depth-gated
  (`delegation_allowed`, `max_delegation_depth`), and count-gated per parent.
- The job-finished callback syncs terminal jobs onto assignments; heartbeats
  in `eurus_agent_heartbeats` feed stale-assignment detection in the
  supervisor, which reconciles against the job table.
- Reassignment creates a replacement assignment and marks the original
  `reassigned`; aggregation treats replaced work as resolved by its
  replacement.

## 5. Event lifecycle (`eurus_domain_events` / `eurus_event_deliveries`)

```
publish -> outbox(pending) -> fan out -> delivery(pending)
delivery -> delivering -> delivered
                |--handler failure---> pending (attempts+1, backoff) -> ... -> dead
                +-> retry_dead_delivery (operator) -> pending
```

- `publish` writes the event and its outbox row in one transaction; duplicate
  `dedup_key` values are silently ignored (idempotent publication).
- `dispatch_tick` drains the outbox, creates deliveries for matching
  subscriptions, then processes due deliveries; handler kinds are
  `submit_job`, `trigger_schedules`, and `notify`.
- `submit_job` uses the deterministic job id `evt-<event>-<subscription>`, so
  a crash between submission and acknowledgement cannot duplicate the work.

## 6. Run lifecycle (unchanged, `eurus_runs`)

`AgentEngine` keeps the pre-existing run state machine with checkpoints,
budgets, cancellation controls, and the tool-attempt journal. Automation jobs
reference runs by `job_id` (plus `.lease_version` suffix on retries), which
is how the recovery and resume machinery correlates them.


## Stabilization notes (implemented + verified)

- The deferred side-effect boundary is active in the production daemon:
  `build_stack` passes a lease-fenced marker factory to
  `AgentJobExecutor`, so `side_effect_started` is set exactly when the
  run's first mutating tool call begins — not at claim time.
- Automatic retry is refused once the boundary is crossed
  (`JobStore.schedule_retry(require_clean_side_effect=True)` from deferred
  pools); the job becomes `failed`/`unknown_outcome` and reconciliation
  records a dead letter instead of re-executing a possibly half-applied
  mutation.
- Lease-exhausted failures (`lease_expired_attempt_limit`) flow through
  reconciliation into dead letters (reason `lease_exhausted`) and publish
  their terminal domain event.
- Job completion triggers the learning lifecycle: a durable
  `eurus_learning_tasks` row (unique per run identity) is enqueued and
  processed inline or by the supervisor; failures are retried with a
  bounded budget.

Verified by `tests/test_stabilization_wiring.py`,
`tests/test_stabilization_recovery.py`, and
`tests/test_stabilization_e2e.py` (scenario F).

## Phase-10 additions (implemented + verified)

- The side-effect boundary marker forwards the claiming worker's identity;
  `JobStore.mark_side_effect_started` validates worker, lease version, and
  lease expiry atomically, so an expired or superseded worker cannot mark
  the boundary.
- Event ownership flows end to end: the owner-resolving publisher stamps
  runtime events, fan-out and acknowledgement verify ownership in SQL, and
  event-triggered jobs inherit the event's owner.
- The learning lifecycle is owner-scoped: task retrieval, claiming,
  completion, failure, and stale requeue all carry the owner predicate;
  lessons produced by automation are private to the owning owner unless a
  caller explicitly shares them.
