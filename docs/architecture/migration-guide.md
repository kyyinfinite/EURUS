# Migration Guide

How the evolution's schema changes apply to existing installations, and how
to back them out. All migrations live in `src/eurus/storage/migrations.py`
and run automatically when a `Database` is constructed.

## 1. Versions

| Version | Contents |
| --- | --- |
| v1–v5 | Pre-existing: runs, checkpoints, runtime events, jobs, run controls, tool attempts |
| v6 | `eurus_jobs.priority/source/correlation_id`; `eurus_dead_letters`; goals, goal tasks, milestones; workflow definitions, instances, node states, approvals; specialists, assignments, heartbeats; domain events, subscriptions, deliveries, outbox; evaluations; learning provenance |
| v7 | `eurus_workflow_instances.child_instance_id`; `eurus_workflow_node_states.created_at/updated_at`; `eurus_agent_assignments.owner_id/delegation_depth`; `eurus_agent_messages` |
| v8 | unique partial index `eurus_dead_letters_open_job` (one open dead letter per job, legacy duplicates deduplicated keeping the newest); `eurus_learning_tasks` (+ unique run index, status index); `eurus_agent_assignments` owner index |
| v9 | `eurus_domain_events.owner_id` and `eurus_event_subscriptions.owner_id` (default `''`, i.e. legacy unscoped/global); per-owner dedup index `(owner_id, dedup_key)` replacing the global dedup index; owner indexes on events and subscriptions |

`LATEST_SCHEMA_VERSION` is 9. A database with a *newer* schema than the
running code is rejected (`database schema is newer than this Eurus version`).

## 2. Safety properties

- **Atomic.** Each version applies inside `BEGIN IMMEDIATE` ... `COMMIT`
  with a re-check of the migrations table, so concurrent processes apply it
  exactly once.
- **Idempotent.** Applied versions are skipped by row, not by heuristic.
- **Additive.** Every v6/v7 change is a new table, a new index, or a new
  nullable/defaulted column. No column is retyped, renamed, or dropped; no
  table is dropped.
- **Backward-compatible data.** Existing rows satisfy every new column's
  default (`priority=5`, `source='schedule'`, `owner_id='local'`,
  `delegation_depth=0`, `created_at=''` — stores treat empty timestamps as
  "unknown" and substitute the current time when reading).
- **Non-destructive.** Existing user data (runs, jobs, schedules, sessions,
  memories) is untouched.

## 3. Upgrade procedure

1. Back up the database file (see `operations-guide.md`).
2. Replace the package and start any entry point (CLI, TUI, daemon,
   gateway). Migration v6 then v7 apply on first `Database` construction.
3. Verify: `eurus autonomy health` (connectivity) and
   `SELECT MAX(version) FROM eurus_schema_migrations;` should read `9`.

### v8 specifics

- Before creating the unique dead-letter index, v8 deletes duplicate open
  dead letters (same `job_id`, `resolved_at IS NULL`), keeping the newest
  by `created_at` (ties broken by id). Existing resolved letters are never
  touched.
- `eurus_learning_tasks` starts empty; the learning coordinator populates
  it from job completions going forward.

### v9 specifics

- Existing events and subscriptions keep `owner_id=''`, which preserves
  their legacy semantics exactly: unscoped events stay visible to every
  subscription, and event jobs created from them use the subscription
  config owner. New runtime events are published with the owning subject's
  owner, so scope only strengthens going forward.
- The dedup index becomes `(owner_id, dedup_key)`: the same external
  dedup key from two owners no longer collides, while re-publication
  inside one owner stays deduplicated. No data is deleted.
- Verified by `tests/test_stabilization_migrations.py`
  (`test_migration_from_v8_preserves_event_ownership_and_dedup`,
  `test_v9_events_support_owner_scoped_queries`) and the interrupted-
  migration retry test.

## 4. Rollback strategy

Schema rollback is a deliberate, manual operation:

```sql
-- v8
DROP TABLE eurus_learning_tasks;
DROP INDEX eurus_dead_letters_open_job;
DROP INDEX eurus_learning_tasks_run;
DROP INDEX eurus_learning_tasks_status;
DROP INDEX eurus_agent_assignments_owner;
-- v7
DROP TABLE eurus_agent_messages;
ALTER TABLE eurus_agent_assignments DROP COLUMN delegation_depth;   -- SQLite >= 3.35
ALTER TABLE eurus_agent_assignments DROP COLUMN owner_id;
ALTER TABLE eurus_workflow_node_states DROP COLUMN updated_at;
ALTER TABLE eurus_workflow_node_states DROP COLUMN created_at;
ALTER TABLE eurus_workflow_instances DROP COLUMN child_instance_id;
-- v6 (drops autonomous-runtime data; pre-existing tables keep their rows)
DROP TABLE eurus_learning_provenance, eurus_evaluations, eurus_outbox,
  eurus_event_deliveries, eurus_event_subscriptions, eurus_domain_events,
  eurus_agent_heartbeats, eurus_agent_assignments, eurus_agent_specialists,
  eurus_workflow_approvals, eurus_workflow_node_states,
  eurus_workflow_instances, eurus_workflow_definitions,
  eurus_goal_milestones, eurus_goal_tasks, eurus_goals, eurus_dead_letters;
DROP INDEX eurus_jobs_priority;
ALTER TABLE eurus_jobs DROP COLUMN correlation_id;                  -- SQLite >= 3.35
ALTER TABLE eurus_jobs DROP COLUMN source;
ALTER TABLE eurus_jobs DROP COLUMN priority;
DELETE FROM eurus_schema_migrations WHERE version IN (6, 7, 8);
```

For SQLite versions without `DROP COLUMN`, restore from the pre-upgrade
backup instead — the recommended rollback path.

## 5. Compatibility notes

- The scheduler's own tables (`eurus_schedules`, occurrences, runs,
  revisions) are owned by `ScheduleStore` and are unchanged by this
  evolution; no co-migration is required.
- The v6 `eurus_jobs` additions are consumed by `JobStore` only; older job
  rows behave exactly as before.
- Config files (`eurus.toml`, `.env`) needed no new required keys; the
  autonomous subsystems configure through constructor parameters with
  production defaults.

## 6. Migration testing (verified)

`tests/test_stabilization_migrations.py` covers: fresh databases, v7 → v8
with preserved rows, legacy duplicate dead-letter dedup, idempotent
re-application, interrupted-migration recovery (a database left at v6/v7
boundary resumes cleanly), and rejection of newer schemas. Schema versions
are validated on every `Database` construction.
