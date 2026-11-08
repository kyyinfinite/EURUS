# Recovery and Reliability

How the runtime keeps promises across crashes, and what it refuses to
promise. The controlling principles: durable state machines, lease fencing,
idempotent dispatch, honest `unknown_outcome` handling, and one supervisor
loop that reconciles reality.

## 1. Failure classification

`runtime/recovery.classify_failure_kind` maps errors to kinds — TRANSIENT
(timeouts, connection and rate-limit markers), PERMANENT (authentication,
schema, configuration), POLICY (approval denied, blocked), INTERRUPTED
(cancellations), UNCERTAIN (unknown outcomes). Only TRANSIENT failures are
retryable; the classification is shared by the retry manager, recovery
manager, and job reconciliation so every layer agrees.

## 2. Retry semantics

`RetryManager` (`automation/retry.py`) decides per failure: attempt cap
(`min(policy.max_attempts, job.max_attempts)`), exponential backoff with
jitter (`backoff_base_seconds` × 2^n capped at `backoff_max_seconds`),
maximum retry duration (`max_retry_duration_seconds` from job age), and
per-source policy overrides. Exhausted jobs become `failed` and are recorded
in `eurus_dead_letters` with reason `retry_exhausted`,
`retry_duration_exceeded`, or `operator_failed`. Operators replay them
(`JobStore.replay` / `eurus autonomy jobs retry`) or resolve them
(`resolve_dead_letter`).

## 3. Lease fencing and the side-effect boundary

- Claims rotate `lease_version`; every mutation (finish, heartbeat, retry
  scheduling, side-effect marking) carries `worker_id` + `lease_version`,
  so a superseded worker cannot corrupt state.
- `side_effect_started` is deferred until the run's first mutating tool
  call (the `MutatingToolMarker` wrapped registry). Lease expiry before
  that requeues the job safely; expiry after it produces
  `unknown_outcome`, which reconciliation flags rather than guessing.
- `recover_expired` splits expired leases into `requeued` /
  `unknown_outcome` / `failed` (attempt-exhausted) in one immediate
  transaction.

## 4. Reconciliation

`ReconciliationService` (`automation/reconciliation.py`) runs inside every
dispatch tick: recover expired leases, sync terminal jobs to schedule runs,
finalize orphaned occurrences, and surface unknown outcomes. Workflow and
assignment reconciliation live in their engines (`WorkflowEngine.recover`,
`stale_running` + supervisor sync), reconciling persisted node/assignment
state against durable job state after restarts.

## 5. Supervision

`RuntimeSupervisor` (`automation/supervision.py`) is the daemon's
watchdog. Each pass it:

1. recovers expired job leases,
2. requests cancellation for stuck running jobs (no progress for
   `stuck_after_seconds`),
3. reconciles stale specialist assignments,
4. recovers and advances workflow instances,
5. ticks the goal engine,
6. dispatches the event bus (outbox + deliveries),
7. dispatches due schedules and reconciles.

`health()` exposes queue depths, oldest-pending age, stuck counts, dead
letters, event bus stats, and automation metrics for monitoring
(`eurus autonomy health`).

## 6. Graceful shutdown

Worker pools stop through an `asyncio.Event`; the daemon cancels its tasks,
aways them with `suppress`, stops workers, and closes the pooled database
(WAL checkpoint). In-flight jobs either finish before shutdown or their
leases expire and recovery requeues them — no orphaned asyncio tasks.

## 7. Storage durability

SQLite with WAL, `synchronous=NORMAL`, a 10 s busy timeout, and foreign
keys on the pooled `Database`. Migrations are atomic, versioned, and
forward-only; every state change is a single guarded statement. See
`migration-guide.md` for rollback strategy and `operations-guide.md` for
backup procedures.

## 8. Known limits

- Exactly-once execution is approximated (idempotency keys + reconciliation
  + deterministic job ids), not assumed; external systems may still see a
  repeated side effect after an uncertain crash — the runtime surfaces this
  via `unknown_outcome` instead of hiding it.
- The supervisor runs inside the daemon process; multiple daemons on one
  database are safe (leases + immediate transactions) but each pays
  reconciliation work.
- Event retention is time-based, not size-based.

## 9. Stabilization changes (implemented + verified)

- **Owner-scoped recovery.** `recover_expired` accepts `owner_id`; the
  worker poll loop and the supervisor recover only their own owner's
  expired leases. Unscoped recovery remains available as the explicit
  administrative path (`owner_id=None`).
- **Retry guard at the side-effect boundary.** `schedule_retry` refuses to
  requeue jobs whose `side_effect_started` flag is set when the deferred
  boundary is active (`require_clean_side_effect`), so a mutation that
  crashed mid-flight is classified (`failed`/`unknown_outcome`) instead of
  blindly re-executed. Jobs that failed before the boundary still retry.
- **Lease-exhausted failures get dead letters.** Recovered
  `lease_expired_attempt_limit` failures are recorded by reconciliation
  (reason `lease_exhausted`), keep owner/attempts/payload context, are
  idempotent, and publish the terminal `TASK_FAILED` event.
- **Dead-letter creation is race-safe.** A unique partial index on open
  dead letters per job (schema v8, with legacy dedup) plus
  `IntegrityError` handling makes concurrent `record_dead_letter` calls
  idempotent.
- **Fenced commits are observable.** A worker whose `finish` lost the race
  logs a warning and increments `commits_fenced`; the newer attempt or
  reconciliation owns the outcome.
- **No silent exception swallowing.** The daemon supervision loop logs
  failures with backoff (`2^n` bounded at 300 s); the supervisor captures
  per-subsystem failures explicitly, applies bounded per-subsystem backoff,
  and reports healthy/degraded/unhealthy states. `health()` never raises on
  a broken subsystem — it reports the failure instead.

Verified by `tests/test_stabilization_recovery.py`,
`tests/test_stabilization_owners.py`, and
`tests/test_stabilization_supervisor.py`.

## 10. Phase-10 hardening (implemented + tested)

- **Side-effect marking is fully fenced.** `mark_side_effect_started`
  requires a non-empty worker identity, the claim's lease version, and an
  unexpired lease; all three are validated inside one UPDATE. The daemon's
  `_SideEffectMarker` forwards the claiming worker's identity and refuses
  to run when the job carries none. An expired or superseded worker can no
  longer mark the boundary (previously the marker used a `None` worker and
  ignored lease expiry).
- **Cross-owner dead-letter replay is refused.** `resolve_dead_letter` and
  `get_dead_letter` accept `owner_id`; replaying an owner's letter requeues
  the job only for that owner.
- **Owner-scoped supervision.** The supervisor forwards its owner to the
  event bus dispatch, the learning tick, and stale-assignment recovery (the
  retrieval is now owner-scoped in SQL; stale assignments of other owners
  are never fetched, and assignment updates/heartbeats verify ownership).
- **Owner-scoped observability.** `RuntimeSupervisor.health()` and
  `AutomationEngine.metrics()` scope job counts, dead letters, event stats,
  learning counts, stale assignments, and the concurrency snapshot to the
  supervisor's owner; `owner_id=None` remains the administrative global
  view. Metadata (ids, error summaries, counts) of other owners never
  appears in an owner-scoped snapshot.
- **Supervision learning counter fixed.** `learning_processed` now records
  the number of processed tasks (previously the whole report object was
  assigned).

Verified by `tests/test_phase10_fencing_observability.py`,
`tests/test_phase10_learning_isolation.py`, and
`tests/test_phase10_multi_owner.py`.
