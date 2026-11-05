# Scheduler Design

The scheduler is a trigger provider: it computes *when* work is due and
records durable occurrences; the Automation Engine owns *how* the work runs.
`ScheduleStore` lives in `src/eurus/scheduler.py`; the cron engine in
`src/eurus/cron.py`.

## 1. Schedule kinds

| Kind | Configuration | Next-run computation |
| --- | --- | --- |
| `once` | `run_at` (ISO-8601, timezone-aware) | single occurrence, then disabled |
| `interval` | `interval_seconds` (60 s – 1 y) | `anchor + interval`, catch-up bounded |
| `cron` | `cron_expr` (5-field), `timezone` | `CronExpression.next_after` |
| `event` | `trigger_event` (+ optional `trigger_subject`) | occurrence created on matching domain event |
| `chain` | `trigger_schedule` (same-owner schedule id) | no clock; occurrence created when the dependency schedule's run finishes `completed` |

## 2. Cron engine

`CronExpression.parse` supports the standard five fields (minute, hour,
day-of-month, month, day-of-week) with `*`, lists, ranges, and steps.
Occurrence search (`next_after`) is an iterative field-by-field scan with a
bounded window (`MAX_SCAN_YEARS`, `MAX_SEARCH_ITERATIONS`), not string
splitting. Local-time correctness is enforced by `_valid_local`: a candidate
is accepted only when it round-trips through the schedule's zone, which
handles DST gaps and ambiguous times; fixed-offset and UTC zones are always
valid. Timezone-aware evaluation uses `zoneinfo` with `tzdata`.

## 3. Occurrence management

Every dispatch creates a durable occurrence row
(`eurus_schedule_occurrences`) with a unique `(schedule_id, scheduled_at)`;
duplicate dispatch across scheduler restarts or concurrent instances is
prevented by that constraint plus `claim_due`, an immediate-transaction
claim. The resulting job carries `schedule_id` + `scheduled_at`, and
`eurus_schedule_runs` records the run linkage that job-completion callbacks
finalize (`finish_run`).

## 4. Misfire policies

| Policy | Behavior on downtime |
| --- | --- |
| `skip` | missed occurrences are marked skipped |
| `run_once` | the single latest missed occurrence runs |
| `catch_up` | up to `catch_up_limit` missed occurrences run in order |
| `coalesce` | all missed occurrences collapse into one run |

A schedule never silently advances past an occurrence: each one is finalized
with a status (`pending`, `dispatched`, `skipped`, `coalesced`, `completed`,
`failed`, `cancelled`).

## 5. Scheduling policies

Per-schedule concurrency (`concurrency_limit`), global automation concurrency
(`ConcurrencyGovernor`), priority (1–9, flows into `eurus_jobs.priority`),
execution windows (`execution_window_start/end`), expiry (`expires_at`),
occurrence caps (`max_occurrences`), pause/enable, manual trigger
(`trigger_now`), revision history (`eurus_schedule_revisions`), and run
history (`history`).

## 6. Dispatch flow

```
ScheduleStore.dispatch_due(now)
  -> pending_occurrences (claim_due, limit)
  -> _select_for_policy(misfire policy, grace)
  -> _create_job_for  -> JobStore.enqueue(priority, source="schedule")
  -> occurrence marked dispatched with job id
```

The daemon invokes this through `AutomationEngine.dispatch_tick`, which also
runs reconciliation in the same pass: expired leases are recovered, schedule
runs are synced to terminal job states, and orphaned occurrences are
finalized.

## 7. Event-triggered schedules

`trigger_event_schedules(event_type, subject_id=..., owner_id=...)` creates
occurrences for enabled `kind='event'` schedules whose `trigger_event`
matches (and `trigger_subject` matches when set). The event bus reaches it
through the `ScheduleTriggerPort` adapter in `cli/autonomy.py`, so a
`WEBHOOK_RECEIVED` event can start scheduled automations without the event
subsystem depending on the scheduler module.

## 8. Chain triggers (implemented + verified)

A `chain` schedule depends on another schedule of the **same owner**
(cross-owner references are rejected at creation). When
`ScheduleStore.finish_run` records a `completed` run for the dependency, an
occurrence is created for every enabled, non-expired, non-concurrency-blocked
chain schedule and a durable job is enqueued under the chain schedule's own
owner. Failed/cancelled dependency runs trigger nothing. Triggering is
idempotent per dependency run (`finish_run` only mutates a
`queued`/`running` run once, and occurrence uniqueness holds). Pause
(`pause`/`resume`, explicit aliases of `set_enabled`) stops chain firing.

Not implemented as aliases: relative-time and conditional triggers are
**unsupported** (documented boundary); calendar semantics are covered by
cron + execution windows.

Verified by `tests/test_stabilization_scheduler.py`.
