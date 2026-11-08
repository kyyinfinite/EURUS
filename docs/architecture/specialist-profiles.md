# Specialist Profiles

Built-in role definitions, permissions and contracts for hierarchical
engineering work. Source of truth: `agent/specialist_profiles.py`; persistence
and dispatch: `multiagent/registry.py` + `multiagent/orchestrator.py`.

## 1. Profile contract

Every profile carries:

| Field | Meaning |
|---|---|
| `role` | unique dispatch key (e.g. `backend`) |
| `description` | one-line purpose shown in tooling |
| `capabilities` | dynamic selection keys (`registry.select(capability=...)`) |
| `allowed_tools` | explicit least-privilege allowlist, validated against the live tool catalog at registration |
| `model_policy` | `default`, `reasoning`, `precise`, ... resolved through `agent.model_policies` |
| `max_iterations` / `max_runtime_seconds` | per-assignment budgets |
| `max_concurrent` | active assignment cap (1–32) |
| `delegation_allowed` | whether the role may receive nested work |
| `max_delegation_depth` | nesting bound for this role |
| `input_schema` / `output_schema` | assignment/result contracts |
| `review_policy` | required verification and review gates |

Two invariants are enforced, never optional:

1. **No empty allowlists.** A built-in profile ships an explicit tool list;
   registration rejects unknown tool names. An empty allowlist means *no
   tools* (deny-by-default, FR-006) — it never means "everything".
2. **Role prompt is not a permission boundary.** Tool visibility is enforced
   by `ScopedToolRegistry` at execution time; the profile only declares intent
   that the runtime then enforces.

## 2. Built-in roster

| Role | Capabilities | Tool policy | Model policy |
|---|---|---|---|
| `architect` | architecture, design, dependency_analysis | read-only repository + docs | reasoning |
| `ui_ux_designer` | ux, visual_design, accessibility | read-only files + document artifacts | default |
| `frontend` | frontend, ui, tests | scoped file read/write, patch tools | default |
| `backend` | api, services, tests | scoped file read/write, patch tools | default |
| `database` | schema, migration, query | scoped file read/write, patch tools; no production DB mutation | reasoning |
| `debugger` | reproduce, root_cause, testing | read + shell for reproduction, patch when assigned | reasoning |
| `qa` | tests, regression, test_strategy | read-only source + shell for test runs; no source mutation | precise |
| `security_reviewer` | secure_coding, dependency_audit, secret_detection | read-only source/config (defensive only) | precise |
| `reviewer` | review, critique | read-only diff/source/tests | reasoning |
| `coder` | coding, testing | isolated worktree coding tools | default |
| `researcher` | research, web_search, summarize | web read tools + local read | default |
| `writer` | writing, editing | read-only source + document tools | default |
| `analyst` | analysis, calculations | read-only + calculate | default |
| `devops` | ci, packaging, runtime_operations | scoped config changes; deployments blocked by default | default |

The initial roster to enable is the small useful set (`architect`, `coder`,
`frontend`, `backend`, `debugger`, `qa`, `reviewer`, `security_reviewer`,
`ui_ux_designer`, `database`); the remaining roles activate as their tool
contracts mature. Profiles can be registered while their dispatch stays
gated by `[agentic_engineering] hierarchical_dispatch`.

## 3. Selection and dispatch

`TeamCoordinator.select_role()` picks the specialist for a task:

1. `preferred_role` wins when present and enabled (disabled roles raise).
2. Otherwise the first capability match from `required_capabilities` in
   registration order.
3. No match → the task stays `ready` for the next tick (never misrouted).

`AgentOrchestrator.dispatch()` then validates nesting limits, capacity and
per-parent assignment caps, persists the assignment with mission/task
correlation, records a `task_request` message and submits a durable
`specialist_assignment` job carrying the specialist's allowlist and budgets.

## 4. Result and artifact contracts

A specialist assignment ends in a structured `SpecialistResult`:
`assignment_id`, `task_id`, `status` (`succeeded|failed|blocked`), `summary`,
`artifact_refs`, optional machine `VerificationEvidence`, `blockers` and
`metrics`. Outputs of substance are published through `ArtifactStore` (or the
`CollaborationHub` facade) as immutable, versioned, checksummed artifacts —
never passed inline through chat history. Consumers receive explicit grants
(`allowed_recipients`).

## 5. Extending the roster

New roles are registered with the same contract; tool names are validated at
registration and again at dispatch. When adding a role:

1. Define the profile in `specialist_profiles.py` with a minimal allowlist.
2. Add capability keys that planners can rely on.
3. Extend `tests/test_specialist_seeding.py` so the roster and its
   permissions stay pinned.
