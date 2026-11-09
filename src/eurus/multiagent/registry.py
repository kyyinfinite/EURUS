from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from eurus.storage.database import Database, DatabasePath

ROLE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.\-]{0,63}$")
TOOL_NAME_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
MODEL_POLICIES = frozenset(
    {"default", "fast", "reasoning", "precise", "cost_optimized", "configured_default"}
)
RISK_TIERS = frozenset({"low", "medium", "high"})

_CATALOG_CACHE: frozenset[str] | None = None


def live_tool_catalog() -> frozenset[str]:
    global _CATALOG_CACHE
    if _CATALOG_CACHE is None:
        from eurus.tools.builtin import default_registry

        registry = default_registry(include_capability_tools=True, include_workflow_tools=True)
        _CATALOG_CACHE = frozenset(registry.names()) | {"shell"}
    return _CATALOG_CACHE


def validate_allowlist_against_catalog(allowed_tools: Sequence[str]) -> None:
    catalog = live_tool_catalog()
    for tool in allowed_tools:
        if not TOOL_NAME_PATTERN.match(tool):
            raise ValueError(f"invalid tool name in allowlist: {tool!r}")
        if tool == "*":
            raise ValueError(
                "wildcard tool grants are not permitted in specialist profiles; "
                "list every tool explicitly"
            )
        if tool not in catalog and not tool.startswith("mcp__"):
            raise ValueError(f"unknown tool in allowlist (not in live catalog): {tool!r}")


def _now() -> datetime:
    return datetime.now(UTC)


def _time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must include a timezone")
    return value.astimezone(UTC).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(slots=True)
class Specialist:
    specialist_id: str
    role: str
    created_at: datetime
    updated_at: datetime
    description: str = ""
    capabilities: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    model_policy: str = "default"
    max_concurrent: int = 2
    delegation_allowed: bool = False
    max_iterations: int = 20
    max_runtime_seconds: int = 600
    priority: int = 5
    enabled: bool = True
    result_schema: dict[str, Any] | None = None
    risk_tier: str = "medium"
    profile_version: int = 1
    tool_policy_version: int = 1
    max_delegation_depth: int = 3
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    review_policy: dict[str, Any] | None = None

    def validate(self) -> None:
        if not ROLE_PATTERN.match(self.role):
            raise ValueError(f"invalid specialist role: {self.role!r}")
        if self.model_policy not in MODEL_POLICIES:
            raise ValueError(f"unknown model policy: {self.model_policy}")
        if self.risk_tier not in RISK_TIERS:
            raise ValueError(f"unknown risk tier: {self.risk_tier}")
        if not 1 <= self.max_concurrent <= 32:
            raise ValueError("max_concurrent must be between 1 and 32")
        if not 1 <= self.max_iterations <= 200:
            raise ValueError("max_iterations must be between 1 and 200")
        if not 10 <= self.max_runtime_seconds <= 86400:
            raise ValueError("max_runtime_seconds must be between 10 and 86400")
        if not 1 <= self.priority <= 9:
            raise ValueError("priority must be between 1 and 9")
        if not 0 <= self.max_delegation_depth <= 8:
            raise ValueError("max_delegation_depth must be between 0 and 8")
        if not 1 <= self.profile_version <= 1_000_000:
            raise ValueError("profile_version must be a positive integer")
        if not 1 <= self.tool_policy_version <= 1_000_000:
            raise ValueError("tool_policy_version must be a positive integer")
        for tool in self.allowed_tools:
            if not re.match(r"^[a-z0-9_]{1,64}$", tool):
                raise ValueError(f"invalid tool name in allowlist: {tool!r}")
            if tool == "*":
                raise ValueError("wildcard tool grants are not permitted in specialist profiles")
        for capability in self.capabilities:
            if not isinstance(capability, str) or not capability.strip():
                raise ValueError("capabilities must be non-empty strings")


def _optional_json(value: Any) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


class SpecialistRegistry:
    def __init__(self, database: Database | DatabasePath) -> None:
        self.database = database if isinstance(database, Database) else Database(database)

    def register(
        self,
        role: str,
        *,
        description: str = "",
        capabilities: Sequence[str] = (),
        allowed_tools: Sequence[str] = (),
        model_policy: str = "default",
        max_concurrent: int = 2,
        delegation_allowed: bool = False,
        max_iterations: int = 20,
        max_runtime_seconds: int = 600,
        priority: int = 5,
        result_schema: dict[str, Any] | None = None,
        risk_tier: str = "medium",
        profile_version: int = 1,
        tool_policy_version: int = 1,
        max_delegation_depth: int = 3,
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        review_policy: dict[str, Any] | None = None,
    ) -> Specialist:
        current = _now()
        specialist = Specialist(
            specialist_id=uuid.uuid4().hex,
            role=role,
            created_at=current,
            updated_at=current,
            description=description[:4000],
            capabilities=tuple(capabilities),
            allowed_tools=tuple(allowed_tools),
            model_policy=model_policy,
            max_concurrent=max_concurrent,
            delegation_allowed=delegation_allowed,
            max_iterations=max_iterations,
            max_runtime_seconds=max_runtime_seconds,
            priority=priority,
            result_schema=result_schema,
            risk_tier=risk_tier,
            profile_version=profile_version,
            tool_policy_version=tool_policy_version,
            max_delegation_depth=max_delegation_depth,
            input_schema=input_schema,
            output_schema=output_schema
            or (dict(result_schema) if isinstance(result_schema, dict) else None),
            review_policy=review_policy,
        )
        specialist.validate()
        validate_allowlist_against_catalog(specialist.allowed_tools)
        with self.database.connection() as connection:
            connection.execute(
                """INSERT INTO eurus_agent_specialists(
                    specialist_id, role, description, capabilities_json,
                    allowed_tools_json, model_policy, max_concurrent,
                    delegation_allowed, max_iterations, max_runtime_seconds,
                    priority, enabled, created_at, updated_at,
                    risk_tier, profile_version, tool_policy_version,
                    max_delegation_depth, input_schema_json, output_schema_json,
                    review_policy_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    specialist.specialist_id,
                    specialist.role,
                    specialist.description,
                    json.dumps(list(specialist.capabilities)),
                    json.dumps(list(specialist.allowed_tools)),
                    specialist.model_policy,
                    specialist.max_concurrent,
                    int(specialist.delegation_allowed),
                    specialist.max_iterations,
                    specialist.max_runtime_seconds,
                    specialist.priority,
                    _time(current),
                    _time(current),
                    specialist.risk_tier,
                    specialist.profile_version,
                    specialist.tool_policy_version,
                    specialist.max_delegation_depth,
                    json.dumps(specialist.input_schema) if specialist.input_schema else None,
                    json.dumps(specialist.output_schema) if specialist.output_schema else None,
                    json.dumps(specialist.review_policy) if specialist.review_policy else None,
                ),
            )
        return specialist

    def get(self, role: str) -> Specialist | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM eurus_agent_specialists WHERE role=?", (role,)
            ).fetchone()
        return self._specialist(row) if row is not None else None

    def specialist_for_role(self, role: str) -> Specialist | None:
        return self.get(role)

    def list_specialists(self, *, enabled_only: bool = True) -> list[Specialist]:
        query = "SELECT * FROM eurus_agent_specialists"
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY priority, role"
        with self.database.connection() as connection:
            rows = connection.execute(query).fetchall()
        return [self._specialist(row) for row in rows]

    def update(self, role: str, **changes: Any) -> Specialist | None:
        current = self.get(role)
        if current is None:
            return None
        allowed = {
            "description",
            "capabilities",
            "allowed_tools",
            "model_policy",
            "max_concurrent",
            "delegation_allowed",
            "max_iterations",
            "max_runtime_seconds",
            "priority",
            "result_schema",
            "risk_tier",
            "tool_policy_version",
            "max_delegation_depth",
            "input_schema",
            "output_schema",
            "review_policy",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown specialist fields: {sorted(unknown)}")
        updated = replace(current, **changes)
        updated.validate()
        validate_allowlist_against_catalog(updated.allowed_tools)
        with self.database.connection() as connection:
            connection.execute(
                """UPDATE eurus_agent_specialists SET description=?, capabilities_json=?,
                allowed_tools_json=?, model_policy=?, max_concurrent=?, delegation_allowed=?,
                max_iterations=?, max_runtime_seconds=?, priority=?, risk_tier=?,
                tool_policy_version=?, max_delegation_depth=?, input_schema_json=?,
                output_schema_json=?, review_policy_json=?, updated_at=?
                WHERE role=?""",
                (
                    updated.description,
                    json.dumps(list(updated.capabilities)),
                    json.dumps(list(updated.allowed_tools)),
                    updated.model_policy,
                    updated.max_concurrent,
                    int(updated.delegation_allowed),
                    updated.max_iterations,
                    updated.max_runtime_seconds,
                    updated.priority,
                    updated.risk_tier,
                    updated.tool_policy_version,
                    updated.max_delegation_depth,
                    json.dumps(updated.input_schema) if updated.input_schema else None,
                    json.dumps(updated.output_schema) if updated.output_schema else None,
                    json.dumps(updated.review_policy) if updated.review_policy else None,
                    _time(_now()),
                    role,
                ),
            )
        return self.get(role)

    def set_enabled(self, role: str, enabled: bool) -> bool:
        with self.database.connection() as connection:
            cursor = connection.execute(
                "UPDATE eurus_agent_specialists SET enabled=?, updated_at=? WHERE role=?",
                (int(enabled), _time(_now()), role),
            )
            return cursor.rowcount == 1

    def remove(self, role: str) -> bool:
        with self.database.connection() as connection:
            cursor = connection.execute("DELETE FROM eurus_agent_specialists WHERE role=?", (role,))
            return cursor.rowcount == 1

    def select(self, *, capability: str | None = None, role: str | None = None) -> list[Specialist]:
        specialists = self.list_specialists(enabled_only=True)
        if role is not None:
            return [item for item in specialists if item.role == role]
        if capability is not None:
            return [
                item
                for item in specialists
                if capability in item.capabilities or capability in item.description.lower()
            ]
        return specialists

    def _specialist(self, row: sqlite3.Row) -> Specialist:
        columns = set(row.keys())

        def column(name: str, default: Any) -> Any:
            return row[name] if name in columns else default

        return Specialist(
            specialist_id=row["specialist_id"],
            role=row["role"],
            description=row["description"] or "",
            capabilities=tuple(json.loads(row["capabilities_json"] or "[]")),
            allowed_tools=tuple(json.loads(row["allowed_tools_json"] or "[]")),
            model_policy=row["model_policy"],
            max_concurrent=int(row["max_concurrent"]),
            delegation_allowed=bool(row["delegation_allowed"]),
            max_iterations=int(row["max_iterations"]),
            max_runtime_seconds=int(row["max_runtime_seconds"]),
            priority=int(row["priority"]),
            enabled=bool(row["enabled"]),
            created_at=_parse(row["created_at"]) or datetime.now(UTC),
            updated_at=_parse(row["updated_at"]) or datetime.now(UTC),
            result_schema=_optional_json(row["result_schema"])
            if "result_schema" in columns
            else None,
            risk_tier=column("risk_tier", "medium"),
            profile_version=int(column("profile_version", 1)),
            tool_policy_version=int(column("tool_policy_version", 1)),
            max_delegation_depth=int(column("max_delegation_depth", 0)),
            input_schema=_optional_json(column("input_schema_json", None)),
            output_schema=_optional_json(column("output_schema_json", None)),
            review_policy=_optional_json(column("review_policy_json", None)),
        )


BUILTIN_SPECIALISTS: tuple[dict[str, Any], ...] = (
    {
        "role": "researcher",
        "description": (
            "Read-only research specialist: search the web and files, gather evidence, "
            "cite concrete sources, and separate facts from inference."
        ),
        "capabilities": ("research", "web_search", "summarize"),
        "model_policy": "default",
        "allowed_tools": (
            "web_search",
            "web_fetch",
            "deep_search",
            "list_files",
            "read_file",
            "search_code",
            "calculate",
            "get_time",
        ),
    },
    {
        "role": "coder",
        "description": (
            "Code-change specialist working in an isolated worktree: implement, test and "
            "summarize changes without touching the primary workspace."
        ),
        "capabilities": ("coding", "testing"),
        "model_policy": "default",
        "allowed_tools": (
            "list_files",
            "read_file",
            "search_code",
            "propose_patch",
            "edit_file",
            "create_file",
            "apply_patch",
            "shell",
            "git_status",
            "git_diff",
            "git_log",
            "git_show",
            "git_branches",
            "calculate",
            "get_time",
        ),
    },
    {
        "role": "reviewer",
        "description": (
            "Critical reviewer: check correctness, edge cases and clarity; propose "
            "concrete revisions without applying them."
        ),
        "capabilities": ("review", "critique"),
        "model_policy": "reasoning",
        "allowed_tools": (
            "list_files",
            "read_file",
            "search_code",
            "git_status",
            "git_diff",
            "git_log",
            "git_show",
            "calculate",
            "get_time",
        ),
    },
    {
        "role": "writer",
        "description": (
            "Writing specialist: structure and polish prose for the target audience "
            "while keeping terminology consistent."
        ),
        "capabilities": ("writing", "editing"),
        "model_policy": "default",
        "allowed_tools": (
            "list_files",
            "read_file",
            "search_code",
            "document_create",
            "document_read",
            "document_replace",
            "calculate",
            "get_time",
        ),
    },
    {
        "role": "analyst",
        "description": (
            "Analysis specialist: break problems into steps, run calculations, and "
            "report findings with assumptions made explicit."
        ),
        "capabilities": ("analysis", "calculations"),
        "model_policy": "default",
        "allowed_tools": (
            "list_files",
            "read_file",
            "search_code",
            "calculate",
            "get_time",
        ),
    },
)


def seed_builtin_specialists(registry: SpecialistRegistry) -> list[str]:
    from eurus.agent.specialist_profiles import (
        builtin_profiles,
        profile_registration_kwargs,
    )

    seeded: list[str] = []
    for profile in (*builtin_profiles(), *BUILTIN_SPECIALISTS):
        role = str(profile["role"])
        try:
            existing = registry.get(role)
            if existing is None:
                registry.register(**profile_registration_kwargs(profile))
            elif not existing.allowed_tools:
                registry.update(
                    role,
                    allowed_tools=tuple(profile.get("allowed_tools", ())),
                    **(
                        {"capabilities": tuple(profile["capabilities"])}
                        if profile.get("capabilities")
                        else {}
                    ),
                )
            else:
                continue
        except ValueError:
            continue
        seeded.append(role)
    return seeded
