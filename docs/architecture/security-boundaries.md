# Security Boundaries

Permission, workspace and approval model for hierarchical agentic
engineering. Every boundary here is enforced in the runtime, not in prompts.

## 1. Tool access — deny by default (FR-006)

`ScopedToolRegistry` (`automation/executor.py`) exposes only tools listed in
the assignment's allowlist:

- empty allowlist → zero tools (never "everything");
- `TOOL_ALLOW_ALL` wildcard exists but must be granted explicitly;
- unknown tool names are rejected at specialist registration and dispatch;
- `blocked` always wins over `allowed`.

`[agentic_engineering] strict_tool_scope = false` is **rejected by the config
loader**. Deny-by-default scoping is mandatory; the flag exists as a posture
assertion so operators can pin the expected state, and the loader fails
closed instead of silently weakening it.

## 2. Owner isolation

Missions, tasks, assignments, artifacts, messages, reviews and verification
records all carry `owner_id` and every store query is owner-scoped:

- cross-owner reads return "not found" (no existence leak);
- artifacts additionally require assignment-level grants
  (`ArtifactPermissionError` otherwise);
- the web gateway pins the whole mission surface to the gateway owner
  (`eurus`); the CLI pins to `local`.

Tests: `test_agentic_security.py`, `test_mission_orchestration.py`
(owner-isolation cases), `test_web_mission_api.py`.

## 3. Workspace isolation

- One Git worktree per coding assignment (`coding/workspace_manager.py`),
  recorded base commit, dirty-parent awareness — uncommitted parent changes
  are never silently included.
- Patches are applied only in a dedicated integration worktree
  (`coding/integration.py`) after path/policy review (`review_patch`), with
  conflict detection via `git apply --check` and rollback preserved. The
  user's primary workspace is never mutated implicitly.
- Staging uses explicit path lists, never blanket `git add -A`.
- Path confinement helpers (`coding/paths.py`) keep artifact and worktree
  operations inside their roots.

## 4. Approval gates and budgets

- Destructive, external, credential and deployment operations still flow
  through the existing approval broker (`security/approval.py`,
  gateway `WebApprovalBroker`); specialist tool scoping is additional to
  approvals, not a replacement.
- Budgets are enforced at every level: mission budget envelope, specialist
  `max_iterations`/`max_runtime_seconds`, per-role `max_concurrent`,
  delegation depth and per-parent assignment caps, retry caps, and the
  automation-level concurrency governor.
- Cancellation and deadlines propagate to descendants:
  `MainSupervisor.cancel_mission()` and `MissionSupervision.tick(deadline=)`
  cancel every non-terminal descendant in a controlled order.

## 5. Evidence integrity

- Artifacts are immutable, versioned, sha256-checksummed and re-verified on
  read; content lives outside SQLite under `data/artifacts/<owner>/` with
  `0700`/`0600` permissions.
- Verification records store command argv, exit code, duration and truncated
  output. A `verified` mission completion can only be derived from these
  records; reviewer approval never overrides failing tests, and unavailable
  required checks yield `completed_unverified`, never success.
- Secret redaction (`logs.redact`, `collect_secrets`) applies to logs,
  traces and messages; defensive scanning (`coding/security_review.py`)
  flags committed secrets as blockers.

## 6. Configuration kill switches

| Switch | Effect when disabled |
|---|---|
| `[agentic_engineering] enabled = false` | Mission surface (gateway + CLI create/dispatch) reports itself disabled |
| `[agentic_engineering] hierarchical_dispatch = false` | Specialist dispatch refused; missions remain creatable, inspectable, cancellable |
| `[agentic_engineering] strict_tool_scope = false` | **Rejected at load time** — deny-by-default is not negotiable |

The primary rollback path is `hierarchical_dispatch = false`: no work is
re-dispatched to specialists, while persisted missions, tasks, artifacts and
evidence stay intact for inspection.
