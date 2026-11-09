from __future__ import annotations

from typing import Any

SPECIALIST_PROFILE_VERSION = 1
TOOL_POLICY_VERSION = 1

RISK_TIERS = frozenset({"low", "medium", "high"})

_READ_TOOLS: tuple[str, ...] = ("list_files", "read_file", "search_code")
_TIME_TOOLS: tuple[str, ...] = ("calculate", "get_time")
_GIT_READ_TOOLS: tuple[str, ...] = ("git_status", "git_diff", "git_log", "git_show")
_PATCH_TOOLS: tuple[str, ...] = ("propose_patch", "edit_file", "create_file", "apply_patch")
_DOC_TOOLS: tuple[str, ...] = ("document_create", "document_read", "document_replace")

_BUILT_IN_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "role": "architect",
        "description": (
            "Architecture specialist: read the repository and project context, propose "
            "interfaces, module boundaries, dependency and design decisions with explicit "
            "trade-offs."
        ),
        "capabilities": ("architecture", "design", "dependency_analysis"),
        "allowed_tools": (*_READ_TOOLS, *_GIT_READ_TOOLS, *_TIME_TOOLS),
        "model_policy": "reasoning",
        "risk_tier": "low",
    },
    {
        "role": "ui_ux_designer",
        "description": (
            "Design specialist: define user flows, layout, design tokens and accessibility "
            "requirements; produce design documents without changing implementation code."
        ),
        "capabilities": ("ux", "visual_design", "accessibility"),
        "allowed_tools": (*_READ_TOOLS, *_DOC_TOOLS, *_TIME_TOOLS),
        "model_policy": "default",
        "risk_tier": "low",
    },
    {
        "role": "frontend",
        "description": (
            "Frontend implementation specialist: build UI, client state and accessible "
            "markup inside an isolated workspace, run targeted checks."
        ),
        "capabilities": ("frontend", "ui", "tests"),
        "allowed_tools": (
            *_READ_TOOLS,
            *_PATCH_TOOLS,
            *_GIT_READ_TOOLS[:2],
            "shell",
            *_TIME_TOOLS,
        ),
        "model_policy": "default",
        "risk_tier": "medium",
    },
    {
        "role": "backend",
        "description": (
            "Backend implementation specialist: implement APIs, business logic and service "
            "integration inside an isolated workspace, run targeted checks."
        ),
        "capabilities": ("api", "services", "tests"),
        "allowed_tools": (
            *_READ_TOOLS,
            *_PATCH_TOOLS,
            *_GIT_READ_TOOLS[:2],
            "shell",
            *_TIME_TOOLS,
        ),
        "model_policy": "default",
        "risk_tier": "medium",
    },
    {
        "role": "database",
        "description": (
            "Database specialist: schema, query and migration design with integrity and "
            "migration-safety review. Production database mutation is not permitted."
        ),
        "capabilities": ("schema", "migration", "query"),
        "allowed_tools": (
            *_READ_TOOLS,
            *_PATCH_TOOLS,
            *_GIT_READ_TOOLS[:2],
            "shell",
            "get_time",
        ),
        "model_policy": "reasoning",
        "risk_tier": "high",
    },
    {
        "role": "debugger",
        "description": (
            "Debugging specialist: reproduce issues, isolate root causes from logs and code, "
            "and propose minimal fixes; applies patches only when assigned."
        ),
        "capabilities": ("reproduce", "root_cause", "testing"),
        "allowed_tools": (*_READ_TOOLS, *_GIT_READ_TOOLS, "shell", *_TIME_TOOLS),
        "model_policy": "reasoning",
        "risk_tier": "medium",
    },
    {
        "role": "qa",
        "description": (
            "Quality assurance specialist: define and run test plans and regression suites, "
            "and report evidence-backed results. No arbitrary source mutation."
        ),
        "capabilities": ("tests", "regression", "test_strategy"),
        "allowed_tools": (*_READ_TOOLS, *_GIT_READ_TOOLS[:2], "shell", *_TIME_TOOLS),
        "model_policy": "precise",
        "risk_tier": "low",
    },
    {
        "role": "security_reviewer",
        "description": (
            "Defensive security reviewer: audit source, configuration and dependencies for "
            "vulnerabilities and secret exposure. No exploit execution or external testing."
        ),
        "capabilities": ("secure_coding", "dependency_audit", "secret_detection"),
        "allowed_tools": (*_READ_TOOLS, *_GIT_READ_TOOLS, "get_time"),
        "model_policy": "precise",
        "risk_tier": "low",
    },
    {
        "role": "reviewer",
        "description": (
            "Independent reviewer: assess correctness, maintainability and edge cases of a "
            "diff with evidence; propose revisions without applying them."
        ),
        "capabilities": ("review", "critique"),
        "allowed_tools": (*_READ_TOOLS, *_GIT_READ_TOOLS, *_TIME_TOOLS),
        "model_policy": "reasoning",
        "risk_tier": "low",
    },
    {
        "role": "coder",
        "description": (
            "General coding specialist working in an isolated worktree: implement, test and "
            "summarize changes without touching the primary workspace."
        ),
        "capabilities": ("coding", "testing"),
        "allowed_tools": (
            *_READ_TOOLS,
            *_PATCH_TOOLS,
            *_GIT_READ_TOOLS,
            "git_branches",
            "shell",
            *_TIME_TOOLS,
        ),
        "model_policy": "default",
        "risk_tier": "medium",
    },
    {
        "role": "researcher",
        "description": (
            "Read-only research specialist: search the web and files, gather evidence, "
            "cite concrete sources, and separate facts from inference."
        ),
        "capabilities": ("research", "web_search", "summarize"),
        "allowed_tools": (
            "web_search",
            "web_fetch",
            "deep_search",
            *_READ_TOOLS,
            *_TIME_TOOLS,
        ),
        "model_policy": "default",
        "risk_tier": "low",
    },
    {
        "role": "writer",
        "description": (
            "Writing specialist: structure and polish prose for the target audience "
            "while keeping terminology consistent."
        ),
        "capabilities": ("writing", "editing"),
        "allowed_tools": (*_READ_TOOLS, *_DOC_TOOLS, *_TIME_TOOLS),
        "model_policy": "default",
        "risk_tier": "low",
    },
    {
        "role": "analyst",
        "description": (
            "Analysis specialist: break problems into steps, run calculations, and "
            "report findings with assumptions made explicit."
        ),
        "capabilities": ("analysis", "calculations"),
        "allowed_tools": (*_READ_TOOLS, *_TIME_TOOLS),
        "model_policy": "default",
        "risk_tier": "low",
    },
    {
        "role": "devops",
        "description": (
            "DevOps specialist: CI configuration, packaging and runtime operations. "
            "Deployment actions are blocked by default and require an approval gate."
        ),
        "capabilities": ("ci", "packaging", "runtime_operations"),
        "allowed_tools": (*_READ_TOOLS, *_PATCH_TOOLS, *_GIT_READ_TOOLS[:2], "shell", "get_time"),
        "model_policy": "default",
        "risk_tier": "high",
    },
)

SPECIALIST_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "summary"],
    "properties": {
        "status": {"type": "string", "enum": ["succeeded", "failed", "blocked"]},
        "summary": {"type": "string"},
        "artifact_refs": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["artifact_id", "artifact_type"],
                "properties": {
                    "artifact_id": {"type": "string"},
                    "artifact_type": {"type": "string"},
                    "version": {"type": "integer"},
                    "content_hash": {"type": "string"},
                },
            },
        },
        "verification": {
            "type": "object",
            "required": ["check_name", "exit_code", "status"],
            "properties": {
                "check_name": {"type": "string"},
                "command_argv": {"type": "array", "items": {"type": "string"}},
                "exit_code": {"type": "integer"},
                "duration_ms": {"type": "integer"},
                "status": {
                    "type": "string",
                    "enum": ["passed", "failed", "skipped", "not_available", "timed_out"],
                },
            },
        },
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
}


def builtin_profiles() -> tuple[dict[str, Any], ...]:
    return tuple(dict(profile) for profile in _BUILT_IN_PROFILES)


def profile_for_role(role: str) -> dict[str, Any] | None:
    for profile in _BUILT_IN_PROFILES:
        if profile["role"] == role:
            return dict(profile)
    return None


def profile_registration_kwargs(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": str(profile["role"]),
        "description": str(profile.get("description", "")),
        "capabilities": tuple(profile.get("capabilities", ())),
        "allowed_tools": tuple(profile.get("allowed_tools", ())),
        "model_policy": str(profile.get("model_policy", "default")),
        "max_concurrent": int(profile.get("max_concurrent", 2)),
        "delegation_allowed": bool(profile.get("delegation_allowed", False)),
        "max_iterations": int(profile.get("max_iterations", 40)),
        "max_runtime_seconds": int(profile.get("max_runtime_seconds", 1800)),
        "priority": int(profile.get("priority", 5)),
        "result_schema": profile.get("result_schema", SPECIALIST_RESULT_SCHEMA),
        "risk_tier": str(profile.get("risk_tier", "medium")),
        "profile_version": int(profile.get("profile_version", SPECIALIST_PROFILE_VERSION)),
        "tool_policy_version": int(profile.get("tool_policy_version", TOOL_POLICY_VERSION)),
        "max_delegation_depth": int(profile.get("max_delegation_depth", 0)),
    }
