from __future__ import annotations

import json
from pathlib import Path

import pytest

from eurus.automation.executor import TOOL_ALLOW_ALL, ScopedToolRegistry
from eurus.automation.policies import ExecutionPolicy, PolicyEnforcer, PolicyViolation
from eurus.multiagent.registry import (
    SpecialistRegistry,
    live_tool_catalog,
    seed_builtin_specialists,
)
from eurus.storage.database import Database
from eurus.storage.migrations import LATEST_SCHEMA_VERSION, apply_migrations
from eurus.tools.builtin import default_registry


def build_registry() -> default_registry:
    registry = default_registry()
    registry.register_tool = getattr(registry, "register_tool", None)
    return registry


def _scoped(allowed: frozenset[str], blocked: frozenset[str] = frozenset()):
    registry = default_registry()
    return ScopedToolRegistry(registry, allowed=allowed, blocked=blocked)


class TestDenyByDefault:
    def test_empty_allowlist_exposes_zero_tools(self) -> None:
        scoped = _scoped(frozenset())
        assert scoped.names() == []
        assert scoped.all() == []
        assert len(scoped) == 0
        assert scoped.get("calculate") is None
        assert "calculate" not in scoped
        assert "calculate" not in scoped.specs()

    def test_explicit_allowlist_exposes_only_listed_tools(self) -> None:
        scoped = _scoped(frozenset({"calculate"}))
        assert scoped.names() == ["calculate"]
        assert scoped.get("get_time") is None
        assert scoped.get("calculate") is not None

    def test_wildcard_is_the_only_allow_all(self) -> None:
        scoped = _scoped(frozenset({TOOL_ALLOW_ALL}))
        assert "calculate" in scoped.names()
        assert "get_time" in scoped.names()

    def test_blocked_wildcard_denies_everything(self) -> None:
        scoped = _scoped(frozenset({TOOL_ALLOW_ALL}), blocked=frozenset({TOOL_ALLOW_ALL}))
        assert scoped.names() == []

    def test_blocked_tool_wins_over_allowlist(self) -> None:
        scoped = _scoped(frozenset({"calculate", "get_time"}), blocked=frozenset({"get_time"}))
        assert scoped.get("get_time") is None
        assert scoped.get("calculate") is not None


class TestExecutionPolicy:
    def test_specialist_scope_empty_allowlist_denies_tools(self) -> None:
        policy = ExecutionPolicy.from_payload({"mode": "specialist"}, scope="specialist")
        assert policy.allows_tool("calculate") is False

    def test_specialist_scope_payload_allowlist_is_honoured(self) -> None:
        policy = ExecutionPolicy.from_payload({"allowed_tools": ["read_file"]}, scope="specialist")
        assert policy.allows_tool("read_file") is True
        assert policy.allows_tool("shell") is False

    def test_legacy_non_specialist_scope_without_allowlist_is_explicitly_trusted(self) -> None:
        policy = ExecutionPolicy.from_payload({}, scope="scheduled")
        assert policy.allows_tool("calculate") is True
        assert TOOL_ALLOW_ALL in policy.allowed_tools

    def test_explicit_empty_allowlist_denies_for_any_scope(self) -> None:
        policy = ExecutionPolicy.from_payload({"allowed_tools": []}, scope="scheduled")
        assert policy.allows_tool("calculate") is False


class TestProfileBinding:
    def _job(self, tmp_path: Path, payload: dict) -> object:
        from eurus.storage.jobs import JobStore

        database = Database(tmp_path / "jobs.sqlite3")
        jobs = JobStore(database)
        return jobs, jobs.enqueue(
            "owner-a",
            "do the work",
            payload=payload,
            source="specialist",
        )

    def test_payload_escalation_is_rejected(self, tmp_path: Path) -> None:
        jobs, job = self._job(
            tmp_path,
            {
                "kind": "specialist_assignment",
                "role": "reviewer",
                "allowed_tools": ["shell", "read_file"],
            },
        )
        enforcer = PolicyEnforcer(
            specialist_profile_resolver=lambda role: {
                "allowed_tools": ["read_file"],
                "tool_policy_version": 1,
            }
        )
        with pytest.raises(PolicyViolation, match="outside the persisted"):
            enforcer.verify_startable(job)

    def test_empty_payload_resolves_profile_allowlist(self, tmp_path: Path) -> None:
        jobs, job = self._job(
            tmp_path,
            {"kind": "specialist_assignment", "role": "reviewer"},
        )
        enforcer = PolicyEnforcer(
            specialist_profile_resolver=lambda role: {
                "allowed_tools": ["read_file", "search_code"],
                "tool_policy_version": 3,
            }
        )
        policy = enforcer.verify_startable(job)
        assert policy.allows_tool("read_file") is True
        assert policy.allows_tool("shell") is False

    def test_policy_version_mismatch_blocks_dispatch(self, tmp_path: Path) -> None:
        jobs, job = self._job(
            tmp_path,
            {
                "kind": "specialist_assignment",
                "role": "reviewer",
                "allowed_tools": ["read_file"],
                "tool_policy_version": 1,
            },
        )
        enforcer = PolicyEnforcer(
            specialist_profile_resolver=lambda role: {
                "allowed_tools": ["read_file"],
                "tool_policy_version": 2,
            }
        )
        with pytest.raises(PolicyViolation, match="tool policy version"):
            enforcer.verify_startable(job)

    def test_wildcard_in_specialist_payload_is_rejected(self, tmp_path: Path) -> None:
        jobs, job = self._job(
            tmp_path,
            {
                "kind": "specialist_assignment",
                "role": "reviewer",
                "allowed_tools": [TOOL_ALLOW_ALL],
            },
        )
        enforcer = PolicyEnforcer(
            specialist_profile_resolver=lambda role: {"allowed_tools": ["read_file"]}
        )
        with pytest.raises(PolicyViolation):
            enforcer.verify_startable(job)


class TestBuiltinProfiles:
    def test_every_builtin_profile_has_non_empty_valid_allowlist(self) -> None:
        from eurus.agent.specialist_profiles import builtin_profiles

        catalog = live_tool_catalog()
        assert len(builtin_profiles()) >= 10
        for profile in builtin_profiles():
            allowlist = tuple(profile.get("allowed_tools", ()))
            assert allowlist, f"profile {profile['role']} must not have an empty allowlist"
            assert TOOL_ALLOW_ALL not in allowlist
            for tool in allowlist:
                assert tool in catalog, f"{profile['role']} allows unknown tool {tool!r}"

    def test_registration_rejects_unknown_tool_names(self, tmp_path: Path) -> None:
        registry = SpecialistRegistry(tmp_path / "spec.sqlite3")
        with pytest.raises(ValueError, match="not in live catalog"):
            registry.register("ghost", allowed_tools=["totally_fake_tool"])

    def test_registration_rejects_wildcard(self, tmp_path: Path) -> None:
        registry = SpecialistRegistry(tmp_path / "spec2.sqlite3")
        with pytest.raises(ValueError, match="invalid tool name"):
            registry.register("wildcard_role", allowed_tools=[TOOL_ALLOW_ALL])

    def test_seed_migrates_empty_allowlist_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "seed.sqlite3"
        database = Database(path)
        registry = SpecialistRegistry(database)
        legacy = registry.register("coder", description="legacy unconfigured row")
        assert legacy.allowed_tools == ()

        seeded = seed_builtin_specialists(registry)
        assert "coder" in seeded
        migrated = registry.get("coder")
        assert migrated is not None
        assert migrated.allowed_tools, "legacy row must be migrated to an explicit allowlist"
        assert TOOL_ALLOW_ALL not in migrated.allowed_tools

    def test_seed_never_overwrites_operator_customization(self, tmp_path: Path) -> None:
        database = Database(tmp_path / "custom.sqlite3")
        registry = SpecialistRegistry(database)
        registry.register("coder", description="custom", allowed_tools=["read_file"])
        seed_builtin_specialists(registry)
        custom = registry.get("coder")
        assert custom is not None
        assert custom.allowed_tools == ("read_file",)

    def test_dispatch_rejects_unknown_role_and_validates_names(self, tmp_path: Path) -> None:
        from eurus.automation.engine import AutomationEngine
        from eurus.multiagent.orchestrator import AgentOrchestrator

        class NoopExecutor:
            async def execute(self, job, context) -> str:
                return ""

        database = Database(tmp_path / "dispatch.sqlite3")
        automation = AutomationEngine(database, NoopExecutor())
        orchestrator = AgentOrchestrator(database, automation)
        registry = SpecialistRegistry(database)
        registry.register("tester", allowed_tools=["read_file"])
        specialist = orchestrator.registry.get("tester")
        assert specialist is not None
        assert specialist.profile_version == 1
        assert specialist.tool_policy_version == 1

    def test_catalog_covers_expected_core_tools(self) -> None:
        catalog = live_tool_catalog()
        for tool in {
            "read_file",
            "list_files",
            "search_code",
            "create_file",
            "edit_file",
            "apply_patch",
            "propose_patch",
            "shell",
            "git_status",
            "git_diff",
            "calculate",
            "get_time",
            "web_search",
            "document_create",
        }:
            assert tool in catalog


def test_migration_v12_creates_new_tables(tmp_path: Path) -> None:
    import sqlite3

    connection = sqlite3.connect(tmp_path / "fresh.sqlite3")
    applied = apply_migrations(connection)
    assert applied == LATEST_SCHEMA_VERSION
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for expected in (
        "eurus_agent_missions",
        "eurus_agent_tasks",
        "eurus_agent_task_dependencies",
        "eurus_agent_artifacts",
        "eurus_agent_reviews",
        "eurus_agent_verifications",
    ):
        assert expected in tables
    specialist_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(eurus_agent_specialists)")
    }
    for column in (
        "risk_tier",
        "profile_version",
        "tool_policy_version",
        "max_delegation_depth",
        "input_schema_json",
        "output_schema_json",
        "review_policy_json",
    ):
        assert column in specialist_columns
    assignment_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(eurus_agent_assignments)")
    }
    assert {"mission_id", "task_id"} <= assignment_columns


def test_migration_from_legacy_v11_database_preserves_data(tmp_path: Path) -> None:
    import sqlite3

    from eurus.storage.migrations import _MIGRATIONS

    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE eurus_schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version in range(1, 12):
        for statement in _MIGRATIONS[version]:
            connection.execute(statement)
        connection.execute("INSERT INTO eurus_schema_migrations VALUES (?, 'x')", (version,))
    connection.execute(
        """INSERT INTO eurus_agent_specialists(
            specialist_id, role, description, capabilities_json, allowed_tools_json,
            model_policy, max_concurrent, delegation_allowed, max_iterations,
            max_runtime_seconds, priority, enabled, created_at, updated_at
        ) VALUES ('s1', 'coder', 'legacy', '[]', '[]', 'default', 2, 0, 20, 600, 5, 1, 'x', 'x')"""
    )
    connection.execute(
        """INSERT INTO eurus_agent_assignments(
            assignment_id, specialist_id, role, objective, status, result,
            attempts, created_at, updated_at, owner_id, delegation_depth
        ) VALUES ('a1', 's1', 'coder', 'obj', 'succeeded', 'res', 1, 'x', 'x', 'eurus', 0)"""
    )
    connection.commit()

    applied = apply_migrations(connection)
    assert applied == LATEST_SCHEMA_VERSION
    assert connection.execute("SELECT role FROM eurus_agent_specialists").fetchall() == [("coder",)]
    assert connection.execute("SELECT status FROM eurus_agent_assignments").fetchall() == [
        ("succeeded",)
    ]
    missions = connection.execute("SELECT COUNT(*) FROM eurus_agent_missions").fetchone()
    assert missions[0] == 0
    connection.execute(
        "INSERT INTO eurus_agent_missions(mission_id, owner_id, objective, status, "
        "created_at, updated_at) VALUES ('m1', 'eurus', 'obj', 'created', 'x', 'y')"
    )
    connection.commit()
    assert connection.execute("SELECT owner_id FROM eurus_agent_missions").fetchone()[0] == "eurus"


def test_json_helpers_roundtrip(tmp_path: Path) -> None:
    database = Database(tmp_path / "meta.sqlite3")
    registry = SpecialistRegistry(database)
    specialist = registry.register(
        "meta_role",
        allowed_tools=["read_file"],
        risk_tier="high",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        review_policy={"required": True},
    )
    assert specialist.risk_tier == "high"
    assert specialist.input_schema == {"type": "object"}
    assert specialist.review_policy == {"required": True}
    fetched = registry.get("meta_role")
    assert fetched is not None
    assert json.dumps(fetched.output_schema) == json.dumps({"type": "object"})
