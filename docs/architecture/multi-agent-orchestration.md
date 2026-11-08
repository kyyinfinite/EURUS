# Multi-Agent Orchestration

Specialist coordination built on durable assignments. The orchestrator
(`src/eurus/multiagent/orchestrator.py`) does not spawn engines; every
specialist assignment is an automation job executed through the Agent Engine
with a scoped tool set.

## 1. Specialist registry

`SpecialistRegistry` (`registry.py`) persists specialists in
`eurus_agent_specialists` (unique role):

- identity: `role`, `description`, `capabilities`
- permissions: `allowed_tools` (validated tool names)
- budgets: `max_iterations`, `max_runtime_seconds`, `priority`
- concurrency: `max_concurrent` (1–32 active assignments)
- delegation: `delegation_allowed` (may receive nested delegations)
- model policy: `default`, `fast`, `reasoning`, `precise`, `cost_optimized`, `configured_default`

`list_specialists`, `update`, `set_enabled`, `remove`, and `select` (by
role or capability) manage the catalog; disabled specialists are never
dispatched.

## 2. Assignments

`AgentOrchestrator.dispatch(objective, role=..., owner_id=..., parent_run_id=..., ...)`:

1. Validates the role is registered and enabled.
2. Enforces nesting rules: `delegation_depth` cannot exceed
   `max_delegation_depth`, and depth > 0 requires `delegation_allowed`.
3. Enforces capacity (`max_concurrent` per specialist) and per-parent
   assignment limits.
4. Persists the assignment (`eurus_agent_assignments`, now with `owner_id`
   and `delegation_depth`), records a `task_request` message, and submits a
   `specialist_assignment` job carrying the specialist's tool allowlist and
   budgets.
5. Heartbeats the assignment and publishes `AGENT_STARTED`.

Job completion syncs assignments to `succeeded`/`failed`/`cancelled` with
results and errors; `reassign` creates a replacement (bounded by
`max_reassignments`) and marks the original `reassigned`.

## 3. Communication

`eurus_agent_messages` persists structured, assignment-scoped messages
(`communication.py`): `task_request`, `progress_update`, `result_delivery`,
`failure_report`, `clarification_request`, `cancellation_signal`. Agents
exchange task-specific messages and artifacts — never shared conversation
history. The orchestrator records the request, result, failure, and
cancellation messages automatically.

## 4. Context isolation

Each assignment runs as its own automation job: the executor builds a fresh
agent, scopes its registry to the specialist's `allowed_tools`
(`ScopedToolRegistry`), and passes the objective with a
`[role specialist]` prefix. A child can never see tools outside its
specialist's allowlist, so delegation never widens privileges.

## 5. Reliability

- Assignments are durable; `store.list_assignments` supports queries by
  parent run, workflow instance, goal, role, and status.
- `heartbeat` (assignment table + `eurus_agent_heartbeats`) feeds
  `stale_running`; the supervisor reconciles stale assignments against
  their jobs and marks terminal outcomes.
- Aggregation (`aggregate(parent_run_id)`) reports totals, successes,
  failures, pending, and cancelled work, plus structured results and
  errors; a parent is *complete* only when every assignment resolved
  successfully (replaced assignments are resolved by their replacements).

## 6. Workflow integration

`specialist_task` workflow nodes resolve their role through the
`SpecialistLookup` protocol (`specialist_for_role`), which the daemon binds
to the live registry — workflows can fan out to specialists without
importing the orchestrator.

## 7. Model-policy routing (implemented + verified)

`specialist.model_policy` now influences real model selection:

1. `dispatch` writes `model_policy` (and any resolved override) into the
   job payload.
2. `AgentJobExecutor` resolves the payload through
   `multiagent/routing.resolve_model_override`: an explicit
   `payload["model"]` wins; otherwise the policy name maps through
   `agent.model_policies` in `eurus.toml` (validated at config load);
   `default`/`configured_default` and unmapped policies use the standard
   chain with its fallbacks.
3. The default resource factory builds the router with the resolved model
   as the chain primary (fallbacks preserved); the resolution (policy,
   override, chain) is recorded in the trajectory (`model_resolved`).
4. Unknown policy names never silently pick a model: the registry rejects
   them at registration and routing falls back to the default chain with a
   warning.

Verified by `tests/test_stabilization_routing.py`.

## 8. Owner isolation (implemented + verified)

`AssignmentStore.list_assignments` and `stale_running` accept `owner_id`;
capacity counting is owner-scoped; `AgentOrchestrator.cancel` refuses to
cancel another owner's assignment; the supervisor only recovers stale
assignments of its own owner. Job callbacks from another owner's job are
ignored. Verified by `tests/test_stabilization_owners.py`.
