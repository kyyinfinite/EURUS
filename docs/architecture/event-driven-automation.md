# Event-Driven Automation

A durable event layer that turns system occurrences into automation work.
The bus (`src/eurus/events/bus.py`) and store (`store.py`) never reason: a
delivery either submits a job through the Automation Engine, triggers event
schedules, or sends a notification.

## 1. Event catalogue

Typed events are strings; the runtime publishes (non-exhaustive):

`TASK_CREATED`, `TASK_STARTED`, `TASK_RETRY_SCHEDULED`, `TASK_COMPLETED`,
`TASK_FAILED`, `TASK_CANCELLED`, `TASK_UNKNOWN_OUTCOME` (job lifecycle,
from `AutomationEngine`); `GOAL_CREATED`, `GOAL_PROGRESS_CHANGED`,
`GOAL_COMPLETED`, `GOAL_FAILED`, `GOAL_CANCELLED` (goal engine);
`WORKFLOW_STARTED`, `WORKFLOW_COMPLETED`, `WORKFLOW_FAILED`,
`WORKFLOW_CANCELLED` (workflow engine); `AGENT_STARTED`, `AGENT_COMPLETED`,
`AGENT_FAILED`, `AGENT_CANCELLED` (orchestrator); `APPROVAL_GRANTED`,
`APPROVAL_REJECTED` (workflow approvals). External systems publish
`WEBHOOK_RECEIVED`, `FILE_CREATED`, and any custom type through the CLI or
the store API.

## 2. Storage and outbox

`eurus_domain_events` records every event with correlation/causation ids,
an optional `dedup_key`, and an `owner_id` (empty string = legacy/unscoped
"global" event). The dedup partial index is `(owner_id, dedup_key)`: the
same external event id published by two owners produces two events, while
re-publication inside one owner is deduplicated. `publish` writes the event
*and* its `eurus_outbox` row in one transaction, so fan-out cannot be lost
if the process dies mid-publication. `eurus_event_subscriptions` stores
name, event-type filter (or `*`), subject filters, handler kind, handler
config, and `owner_id`. `eurus_event_deliveries` tracks per-subscription
delivery with attempts, backoff, and dead states (unique
`(event_id, subscription_id)`); a delivery's owner is the owner of its
event.

## 3. Dispatch

`EventBus.dispatch_tick(now=..., owner_id=...)` is owner-scoped at the SQL
level. `owner_id=None` is the administrative mode that processes every
event; a daemon scoped to an owner processes only that owner's outbox rows
and deliveries (plus legacy unscoped rows, which are global by design).

1. **Outbox drain** — pending events fan out to matching enabled
   subscriptions (deliveries created idempotently), then the outbox row is
   marked dispatched (ownership re-verified in the UPDATE).
2. **Delivery processing** — due deliveries are claimed (`pending` →
   `delivering`, attempts+1) and handled:

| Handler | Config | Effect |
| --- | --- | --- |
| `submit_job` | `objective` template, `payload`, `priority`, `max_attempts` | durable job (`source="event"`, id `evt-<event>-<subscription>`) owned by the **event's** owner; for legacy unscoped events the subscription config `owner_id` is used |
| `trigger_schedules` | `owner_id` (legacy events only) | `ScheduleStore.trigger_event_schedules` for `kind='event'` schedules, scoped to the event's owner |
| `notify` | `message` template | notification sink delivery carrying the event's owner |

Subscription matching is owner-scoped: a subscription sees events of its
own owner plus legacy unscoped events. A subscription created with
`owner_id="*"` is an explicit administrative global subscription and sees
every event. The composition root (`build_stack`) publishes runtime events
through an owner-resolving adapter that reads the owner from the subject
row (job, goal, workflow instance, or assignment), so every event produced
by the runtime carries its owner.

3. **Acknowledgement** — success marks the delivery `delivered`; failure
   requeues with exponential backoff (attempts bounded by
   `max_delivery_attempts`) or marks it `dead` when exhausted. Claim,
   acknowledge, fail, and dead-replay operations accept an `owner_id` and
   verify the delivery's event ownership inside the UPDATE, so a worker
   operating for one owner cannot acknowledge or replay another owner's
   delivery.

Templates resolve against `{event_type, subject_type, subject_id, payload}`
with the workflow template engine, so objectives can embed event data
without string interpolation risks.

## 4. Idempotency and recovery

- Publication dedup via `dedup_key`.
- Delivery creation dedup via the unique `(event_id, subscription_id)`
  index.
- Job submission dedup via deterministic job ids: if a crash happens after
  submission but before acknowledgement, the retry finds the existing job
  and acknowledges.
- The outbox survives restarts; the daemon's supervision loop calls
  `dispatch_tick` every poll, so pending work resumes without manual
  intervention.

## 5. Retention

`purge(older_than_days=30)` deletes dispatched outbox rows and events (and,
by foreign key, their deliveries) older than the cutoff, keeping the event
table bounded. `stats()` reports queue depths, dead deliveries, and active
subscriptions for monitoring.

## 6. Management

`eurus autonomy events {publish,list,subscribe,unsubscribe,stats}` covers
the operator surface; subscriptions can be disabled without deletion via
`set_subscription_enabled`.

## 7. Stabilization notes (verified)

- The daemon composition root (`build_stack`) constructs the
  `AutomationEngine` first and hands it to the `EventBus`, so `submit_job`
  subscriptions work in the production daemon (regression-tested through
  `build_stack` itself).
- Automation lifecycle events (`TASK_*`) are **not** dispatched to
  `submit_job` subscriptions by default; a subscription must opt in with
  `handler_config["allow_task_events"] = true`. This prevents unbounded
  event→job→event amplification (verified by
  `tests/test_stabilization_wiring.py::test_task_lifecycle_events_do_not_retrigger_subscriptions`).
- Duplicate `(event, subscription)` deliveries and job submission races are
  idempotent: the derived job id is deterministic and
  `sqlite3.IntegrityError` on the second insert is treated as delivered.
- Unexpected delivery exceptions are logged with delivery/event/subscription
  ids, retried with backoff, and dead-lettered after `max_delivery_attempts`.
- Dead deliveries are observable via `DomainEventStore.stats()` /
  `eurus autonomy events stats` and replayable with `retry_dead_delivery`.

## 8. Owner isolation (implemented + tested)

- Every event, subscription, and delivery carries ownership. Outbox drain,
  delivery retrieval, claiming, acknowledgement, failure/retry, dead-letter
  listing/replay, stats, and listing are owner-scoped in SQL, not only in
  Python.
- A delivery belonging to owner A can only ever create a job owned by A;
  cross-owner acknowledgement and dead-delivery replay are refused.
- Owner scope survives restart and recovery; scoped stats never include
  other owners' events or deliveries.
- Global (administrative) dispatch exists only as the explicit
  `dispatch_tick()` / `owner_id=None` mode used by operator tooling.
- Verified by `tests/test_phase10_event_isolation.py` and the multi-owner
  scenarios in `tests/test_phase10_multi_owner.py`.

## 9. Notification support

`notify` subscriptions require a configured `NotificationSink`. The daemon
composition wires one sink shared by the AutomationEngine and the EventBus
(`build_stack(notification_sink=...)`, default `LoggingNotificationSink`);
there is no built-in third-party provider, so a `notify` delivery without a
sink is **explicitly unsupported**: it is retried and dead-lettered with a
visible error instead of being acknowledged. An asynchronous sink invoked
without a running event loop is likewise not acknowledged. Delivery status
is exposed through the delivery records (`pending`/`delivering`/
`delivered`/`dead`) and `stats()`.
