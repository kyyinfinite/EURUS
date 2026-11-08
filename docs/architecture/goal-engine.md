# Goal Engine

Persistent objectives that outlive processes. `GoalEngine`
(`src/eurus/goals/engine.py`) owns goals; it never executes work itself —
every task and every planning pass is a durable automation job that runs
through the Agent Engine.

## 1. Data model

- `eurus_goals`: objective, description, constraints, priority, deadline,
  status (`status_version` for optimistic concurrency), progress, success
  criteria, plan JSON (also carries `replan_count`, pending plans, pending
  evaluations), parent goal, source, completion evidence.
- `eurus_goal_tasks`: title, detail, status, `depends_on` (resolved task
  ids), success criteria, milestone, schedule, job and run references,
  output, error, attempts.
- `eurus_goal_milestones`: title, criteria, status, target progress.

## 2. Decomposition

The Core Agent decomposes goals: the engine submits a planning job whose
objective instructs the agent to call the `goal_submit_plan` tool. The tool
schema accepts tasks (unique titles, optional `depends_on` by title,
optional milestone) and milestones. `capture_plan` validates the document
(non-empty, bounded, deduplicated titles, resolvable dependencies) and
stores it as the goal's pending plan; the next tick applies it. This keeps
decomposition adaptive — plans can be partial and revised by replanning
rather than requiring a complete up-front design.

## 3. Lifecycle and transitions

`goals/states.py` defines the transition table (see `execution-lifecycle.md`
for the diagram). Invalid transitions raise `InvalidGoalTransition`;
terminal states are COMPLETED, FAILED, CANCELLED. WAITING is reachable from
every non-terminal state (pause) and returns to ACTIVE on resume.

## 4. The autonomous loop

`tick()` iterates non-terminal goals (bounded by `max_active_goals`):

1. CREATED → submit planning job (ANALYZING → PLANNING).
2. PLANNING/REPLANNING → apply a captured plan if present; if the planning
   job finished without a usable plan, the goal FAILS with evidence.
3. ACTIVE/EXECUTING → submit PENDING tasks whose dependencies SUCCEEDED,
   bounded by `max_concurrent_task_jobs`; then evaluate.
4. EVALUATING → deterministic mode completes on task evidence; agent mode
   waits for the `goal_submit_evaluation` verdict.
5. Evaluation outcomes: all-succeeded → EVALUATING/COMPLETED; any-failed →
   `_maybe_replan` (bounded by `max_replans`, then FAILED with
   `replan_exhausted`); nothing open → back to ACTIVE.

While a goal WAITs for an external event or a scheduled occurrence it burns
no jobs — polling is state-driven, not token-driven.

## 5. Progress and evaluation

`evaluate()` computes progress as succeeded/total tasks, gathers per-task
evidence strings, and updates milestones whose `target_progress` is reached.
Completion is evidence-based: `completion_evidence` records task outputs,
success criteria, evaluation verdicts, or failure reasons (deadline
`deadline_exceeded`, empty `planning_empty`, `replan_exhausted`). The
`goal_report_progress` tool lets executing agents publish fractional
progress.

## 6. Replanning

On task failure the engine transitions to REPLANNING (budget permitting),
cancels superseded open tasks and their jobs, and submits a fresh planning
job. Deadline expiry fails the goal outright. Pause/resume/cancel are owner
-scoped operations (owner isolation enforced in the store).

## 7. Events

The engine publishes `GOAL_CREATED`, `GOAL_PROGRESS_CHANGED`,
`GOAL_COMPLETED`, `GOAL_FAILED`, and `GOAL_CANCELLED` through the injected
publisher (the daemon wires this to an owner-resolving adapter on the event
bus, so each event carries the goal's owner), so subscriptions can react to
goal progress without polling — and only within the owning owner's scope.

## 8. Agent tools

`goals/tools.py` registers `goal_submit_plan`, `goal_submit_evaluation`, and
`goal_report_progress`. The daemon injects them through
`goal_tool_provider`, which adds the tools only for jobs whose payload
carries a `goal_id` — goal context stays scoped to goal jobs.
