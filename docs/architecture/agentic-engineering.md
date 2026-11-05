# Agentic Engineering Architecture

Hierarchical mission orchestration on top of the durable automation runtime.
The Main Orchestrator owns a persisted mission; work flows down through team
leads to specialist agents and back up as artifacts plus machine verification
evidence. Nothing here spawns a second agent engine — every specialist
assignment is an automation job executed through the existing
`AgentJobExecutor` → `AgentEngine` path (see `multi-agent-orchestration.md`).

## 1. Organization model

```
User → Main Orchestrator (MainSupervisor)
         → Engineering Manager (plan review, conflict routing)
         → Coding Team Lead (TeamCoordinator)
              → specialists (frontend, backend, qa, reviewer, ...)
```

- `agent/supervisor.py` — `MissionStore` (`eurus_agent_missions`) and
  `MainSupervisor`: lifecycle (create → plan → start → running → verify →
  final acceptance), cancellation, resume, verification recording, route
  decision (`direct` vs `hierarchical`).
- `agent/engineering_manager.py` — deterministic plan review (role coverage,
  fan-in/fan-out, verification presence) and path-overlap conflict routing
  before integration.
- `agent/team_coordinator.py` — dependency-aware dispatch through
  `AgentOrchestrator` (preferred role first, capability match second).
- `agent/task_decomposer.py` — objective + subtask drafts → validated
  `TaskPlan` (unique keys, acyclic dependencies, known roles).
- `agent/acceptance.py` — `VerificationStore`, `ReviewStore`,
  `AcceptanceService` (evidence-backed final decision).
- `agent/review_loop.py` — bounded review/revision cycles with escalation.
- `agent/collaboration.py` — typed message + artifact exchange facade.
- `multiagent/contracts.py` — Mission, TaskSpec, ArtifactRef,
  SpecialistResult, ReviewFinding, VerificationEvidence, AcceptanceDecision.
- `multiagent/task_graph.py` — persisted task DAG (`eurus_agent_tasks`,
  `eurus_agent_task_dependencies`), ready queue, blocked propagation.
- `multiagent/artifacts.py` — immutable, versioned, owner-scoped artifact
  store (content outside SQLite, sha256 integrity, recipient grants).
- `multiagent/supervision.py` — descendant-aware supervision tick (stale
  detection, restart reconciliation, deadline expiry).

## 2. Mission lifecycle

States (`MISSION_STATUSES`): `created → planning → planned → running →
verifying → awaiting_approval → completed`, with `completed_unverified`,
`failed`, `cancelled`, `blocked` as terminal alternatives. Job/assignment
state remains the execution transport; the mission/task graph is business
state and never duplicates the job state machine.

1. `create_mission()` persists the objective, acceptance criteria and budget.
2. `plan_mission()` validates a task DAG through `TaskDecomposer` and
   persists tasks + dependency edges; cycles and unknown roles are rejected.
3. `start_mission()` promotes dependency-ready tasks (`promote_ready`).
4. `dispatch_ready()` hands ready tasks to the `TeamCoordinator`, which
   dispatches durable assignments with mission/task correlation.
5. `on_task_succeeded()` / `on_task_failed()` unlock or block dependents.
6. `record_verification()` stores machine evidence (command argv, exit code,
   duration, truncated output, environment) — agent claims are never evidence.
7. `finalize_mission()` evaluates the acceptance policy:
   - open blocker/critical/major findings or failing checks → `blocked`
   - missing/unavailable required checks → `completed_unverified`
   - everything passed → `completed`
   A mission is never marked completed merely because model calls succeeded.

## 3. Scheduling and parallelism

`TaskGraphStore.ready_tasks()` returns a deterministic queue (priority, then
task_key, then creation time). Independent tasks dispatch in one pass; a task
with unmet dependencies stays `pending` and becomes `blocked` when a
predecessor fails. Revision tasks (`<key>-revision-N`) link back to their
parent and close it on approval.

## 4. Collaboration protocol

`CollaborationHub` wraps the message log (`eurus_agent_messages`) and the
artifact store. Message types: `task_request`, `progress_update`,
`artifact_published`, `result_delivery`, `review_request`, `review_result`,
`revision_request`, `blocker_report`, `clarification_request`,
`decision_record`, `failure_report`, `cancellation_signal`,
`task_dependency_ready`. Rules: no shared chat history; artifact references
must belong to the same mission and owner; recipients read only granted
artifacts; decision records carry rationale plus source artifact ids.

## 5. Verification, review and integration

- Coding specialists work in isolated Git worktrees (`coding/worktree.py`,
  `coding/workspace_manager.py`); patches carry base revision, changed paths
  and diff hash.
- `coding/integration.py` applies patches in a dedicated integration
  worktree after `review_patch` policy checks, detects conflicts via
  `git apply --check`, and never touches the user's primary workspace
  directly. Conflicts are classified by `coding/conflict_resolution.py`.
- `coding/test_strategy.py` derives a runnable plan (targeted mirror tests,
  regression suite, ruff/mypy static checks) where every command passes the
  `coding/validation.py` allowlist.
- `coding/security_review.py` performs defensive static review (secrets,
  disabled TLS, shell injection, unsafe deserialization, SQL string
  building) on files, workspaces and diffs; findings convert to
  `ReviewFinding(category="security")` and flow through the review loop.
- `coding/project_understanding.py` builds a bounded repository map
  (manifests, languages, test locations, docs, entry points) for planner and
  specialist context.

## 6. Observability and control surface

- Web API (auth-scoped, owner `eurus`): `GET/POST /api/missions`,
  `GET /api/missions/{id}` (mission + tasks + assignments + aggregation),
  `POST .../plan|start|cancel|resume|dispatch|finalize`, and
  `GET .../tasks|artifacts|verifications|reviews`.
- Frontend: `Missions` page (`/missions`) with `MissionTree`,
  `AgentTaskPanel`, `ArtifactPanel` and `ReviewPanel` components.
- CLI: `eurus missions create|list|show|tasks|plan|start|cancel|resume|
  dispatch|finalize`.
- Supervision: `MissionSupervision.tick()` reconciles stale assignments,
  requeues orphaned work after restarts and enforces mission deadlines.

## 7. Rollout and rollback

`[agentic_engineering]` configuration (see `security-boundaries.md`):

- `enabled = false` — the mission surface (gateway + CLI) reports the
  surface as disabled; existing missions stay inspectable at the storage
  layer.
- `hierarchical_dispatch = false` — dispatch refuses to hand work to
  specialists; planning, inspection, cancellation and acceptance keep
  working. This is the primary rollback path.
- `strict_tool_scope` — mandatory posture assertion; the loader rejects
  `false` outright.

All schema changes (missions, tasks, dependencies, artifacts, reviews,
verifications) are additive migrations; no existing rows are rewritten.

## 8. Benchmarks

`python benchmarks/bench_mission.py [--repeats N] [--json PATH]` measures
planning, dependency promotion, durable dispatch, artifact round-trips and
review cycles offline. Results land in `benchmarks/results/bench_mission.json`
and establish the baseline before performance targets are chosen (PRD §11).
