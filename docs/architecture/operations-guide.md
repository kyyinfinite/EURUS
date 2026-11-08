# Operations Guide

Running EURUS as an autonomous runtime: starting the daemon, managing the
subsystems, monitoring, and maintenance.

## 1. Files and locations

| Path | Purpose |
| --- | --- |
| `~/.eurus/data/eurus.sqlite3` | runs, jobs, goals, workflows, assignments, events (WAL) |
| `~/.eurus/data/trajectory.sqlite3` | execution trajectories (created by the daemon) |
| `~/.eurus/sessions.sqlite3` | sessions, messages, memories (pre-existing) |
| `~/.eurus/skills/` | skill drafts and active skills |
| `EURUS_HOME` | relocates the whole layout |

## 2. The unified daemon

```bash
eurus autonomy daemon --workers 4 --poll-seconds 15 --owner local
eurus schedule daemon                    # same runtime, legacy entry point
```

Each pass of the supervision loop: recover expired leases → cancel stuck
jobs → reconcile stale assignments → recover and advance workflows → tick
goals → dispatch events → dispatch due schedules. Workers execute jobs
through the Agent Engine (provider router, MCP, tools, lessons) with
per-job policies, budgets, and trajectory recording.

Flags `--no-goals`, `--no-workflows`, `--no-events` disable subsystems for
focused operation.

## 3. Managing goals

```bash
eurus autonomy goals create "Publish a weekly digest" --criteria "digest exists;sources cited"
eurus autonomy goals list
eurus autonomy goals show <goal_id>
eurus autonomy goals pause|resume|cancel <goal_id> --owner <owner>
```

Goals advance on their own once created. "waiting" goals consume nothing;
failed goals record evidence (deadline, empty plan, replan exhaustion).

## 4. Managing workflows

```bash
eurus autonomy workflows define workflow.json
eurus autonomy workflows start digest-flow --input topic=agents
eurus autonomy workflows show <instance_id>
eurus autonomy workflows approvals
eurus autonomy workflows decide <approval_id> approved --by alice
eurus autonomy workflows pause|resume|cancel <instance_id>
```

Definition documents are JSON: `workflow_id`, `name`, `required_inputs`,
and `nodes` (see `docs/architecture/workflow-engine.md` for the node
contracts). Registration validates the graph; instances pin their version.

## 5. Managing specialists

```bash
eurus autonomy agents register researcher --capability research --tool web_search
eurus autonomy agents list
eurus autonomy agents dispatch "Research topic X" researcher --owner alice
eurus autonomy agents assignments --role researcher
eurus autonomy agents enable|disable researcher
```

## 6. Managing events

```bash
eurus autonomy events subscribe file-watcher --type FILE_CREATED \
    --subject-type file --objective "Analyze {{payload.path}}" --owner alice
eurus autonomy events publish FILE_CREATED --subject-type file --subject-id f1 \
    --payload '{"path": "/tmp/report.md"}' --dedup-key f1-v1 --owner alice
eurus autonomy events list
eurus autonomy events stats
```

Subscriptions and published events carry an owner scope: a scoped daemon
dispatches only its own owner's events and deliveries, and the created
jobs inherit the event's owner. Omitting `--owner` publishes a legacy
unscoped (global) event.

## 7. Jobs, dead letters, health

```bash
eurus autonomy jobs list --status failed
eurus autonomy jobs retry <job_id>
eurus autonomy jobs dead-letters
eurus autonomy jobs resolve <dead_letter_id> --resolution discarded --owner alice
eurus autonomy health --owner alice
```

`jobs resolve` accepts `--resolution replayed|discarded|completed` and an
optional `--owner`; with `--owner`, only that owner's dead letter can be
resolved (and its job replayed). Without the flag the command is the
explicit administrative mode.

`health` prints the overall state (`healthy` / `degraded` / `unhealthy`),
owner scope, queue depths (running/queued/failed/unknown), dead letters,
oldest pending age, stuck jobs, active workers with liveness ages, time
since the last reconciliation, per-subsystem state (healthy/backoff/
consecutive failures/last error), event bus stats, and automation metrics.
`unhealthy` means a core subsystem (lease recovery or scheduler dispatch)
is failing; `degraded` means a non-core subsystem is failing or in
backoff. Subsystem failures never abort the supervision loop: each failing
subsystem retries on bounded exponential backoff (2 s → 300 s).

Show commands accept `--owner` (`goals show`, `workflows show`,
`workflows decide`) so operator inspection can be scoped; omitting the
flag is the explicit administrative mode.

## 8. Monitoring and logs

Logs are redacted (API keys and secret-like strings are scrubbed by the
existing logging pipeline). Useful signals:

- `jobs_running` sustained at the worker count → throughput bound.
- `oldest_queued_age_seconds` growing → starvation or a stuck scheduler.
- `dead_letters` increasing → inspect `eurus autonomy jobs dead-letters`.
- `events.dead_deliveries` → handler failures; retry with
  `retry_dead_delivery` after fixing the cause.

## 9. Maintenance

- **Backups:** stop the daemon (or rely on WAL), then copy the three SQLite
  files plus `~/.eurus/skills/`. `PRAGMA wal_checkpoint(TRUNCATE)` before
  copying gives a compact snapshot.
- **Retention:** events purge automatically after 30 days
  (`DomainEventStore.purge`, invoked by long-running operators); trajectories
  keep 90 days by default; dead letters persist until resolved.
- **Crash recovery:** no manual steps — leases expire, the supervisor
  reconciles, and durable state machines resume. After an unclean shutdown,
  `unknown_outcome` jobs are listed in dead letters and may need a decision
  (replay or resolve).
- **Scaling:** raise `--workers` (bounded by provider limits), per-schedule
  `concurrency_limit`, and specialist `max_concurrent`. Global concurrency is
  governed by `ConcurrencyGovernor`.
- **Owner isolation:** run one daemon per owner (`--owner <id>`); claims,
  recovery, supervision, schedule dispatch, and dead-letter views are
  owner-scoped. Unscoped access (owner `None`) is the explicit
  administrative mode and touches all owners.

## 10. Security checklist

- Specialists run with explicit tool allowlists; blocked tools are invisible
  to their registries.
- Scheduled jobs retain their owner's scope; approval-gated nodes require an
  explicit decision.
- Remote approval is denied in daemon context (`deny_remote_approval`).
- Lessons are validated (length, secret scan, dedup) before entering memory,
  with provenance records that can be reversed. Learning runs against
  durable per-run tasks (`eurus_learning_tasks`); a failed evaluation is
  retried with a bounded budget and never blocks job completion.
- Automation lifecycle events (`TASK_*`) are not delivered to `submit_job`
  subscriptions unless the subscription sets `allow_task_events: true` in
  its handler config.
