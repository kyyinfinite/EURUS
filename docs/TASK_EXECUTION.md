# Task Execution

The durable task controller runs planned steps sequentially, verifies each
result, retries failures within a fixed budget, and persists progress. It never
bypasses tool approval or workspace boundaries.

## Runtime

`AgentEngine` (`src/eurus/runtime/engine.py`) owns the run lifecycle:

1. Create a `QUEUED` run record and transition to `RUNNING`.
2. Stream agent events, recording model calls, tool activity, plan updates, and
   recovery attempts as ordered runtime events.
3. Save checkpoints every `checkpoint_interval_steps` iterations.
4. On completion, transition to `VERIFYING`, evaluate the verification outcome,
   and only then record `COMPLETED` or `FAILED`.
5. Persist a final budget snapshot with token usage and, when pricing is known,
   an estimated cost.

## Budgets

`BudgetSpec` bounds iterations, wall-clock time, tool calls, parallelism, model
calls, tokens, and cost. Cost budgets are enforced only when a price is
configured for every model used; otherwise the run reports
`cost.tracking_unavailable` and never presents an invented cost as an estimate.
Pricing comes from the built-in catalog and can be overridden in configuration:

```toml
[pricing]
updated_at = "2026-10-01"

[pricing.models."groq/llama-3.3-70b-versatile"]
input_per_mtok = 0.59
output_per_mtok = 0.79
```

## Bounded orchestration API

`SubagentOrchestrator` (`src/eurus/orchestration/subagents.py`) executes a task
graph with a shared iteration budget, per-node allowlists, and bounded retries.
Every child result is marked unverified until the parent confirms it. The
controller serializes progress so application-managed persistence can resume.

## Delegation

- `delegate_task` runs a read-only subagent with a restricted toolset. The result
  is marked unverified; the parent must confirm critical claims itself.
- `delegate_code_task` runs a coder subagent inside a disposable git worktree.
  The subagent cannot write outside the worktree. Its diff is reviewed for path
  traversal, symlinks, binary content, and secret exposure. A patch that fails
  review is withheld and reported as a tool error; an accepted patch is returned
  as a proposal only. Integration happens exclusively through `apply_patch` with
  owner approval.
- Both delegation paths execute the child through `AgentEngine` itself: the
  child receives its own durable run record linked to the parent, its own
  events, tool-attempt records, checkpoints, bounded budget (iterations, tool
  calls, wall-clock deadline), and finalization on crash, timeout, or
  cancellation. Children never orphan in `RUNNING` state.

## Recovery and resume

`eurus resume <run_id>` classifies the interrupted run:

- runs with a usable checkpoint resume through a fresh engine,
- runs with unresolved tool side effects are blocked until the target state is
  reconciled by hand,
- runs without any checkpoint require manual review.

Resumed runs receive a resumption directive built from the durable record: the
last completed iteration, plan steps already verified, successful and failed
tool calls, the last plan summary, and prior recovery attempts. The directive
instructs the agent to verify current state before continuing instead of
blindly repeating completed work.

Uncertain mutations are never blindly replayed: the tool boundary consults both
an in-process set and the durable `eurus_tool_attempts` table, so an identical
mutating call stays blocked across process restarts until it is reconciled.
Approvals are re-validated at resume time and do not carry over when the action
identity or relevant context changes.

## Failure classification

`classify_failure_kind` separates failures into transient, permanent, policy,
interrupted, and uncertain categories. Gateway tasks retry automatically only
for transient failures and only within their configured attempt cap
(`max_attempts`, default 1). Jobs with an expired lease after a side-effect
boundary always land in `unknown_outcome` and require reconciliation.

## Durable gateway tasks

Telegram tasks persist in `gateway_tasks` with run linkage, attempt caps, and
deadlines. On gateway restart, queued tasks are re-dispatched automatically;
interrupted tasks resume only after the user runs `/resume` (with
`--ack-side-effects` when side effects may have started). `/pause` interrupts a
task while keeping its state recoverable.
