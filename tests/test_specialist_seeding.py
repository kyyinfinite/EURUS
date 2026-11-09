from __future__ import annotations

from pathlib import Path

from eurus.multiagent.registry import (
    BUILTIN_SPECIALISTS,
    SpecialistRegistry,
    seed_builtin_specialists,
)


def expected_roles() -> list[str]:
    from eurus.agent.specialist_profiles import builtin_profiles

    seen: list[str] = []
    for profile in (*builtin_profiles(), *BUILTIN_SPECIALISTS):
        if profile["role"] not in seen:
            seen.append(profile["role"])
    return seen


def test_seed_registers_all_builtin_roles(tmp_path: Path) -> None:
    registry = SpecialistRegistry(tmp_path / "eurus.sqlite3")
    roles = expected_roles()
    assert seed_builtin_specialists(registry) == roles
    for role in roles:
        profile = registry.get(role)
        assert profile is not None
        assert profile.description
        assert profile.allowed_tools, f"{role} must have an explicit allowlist"
        assert "*" not in profile.allowed_tools
    model_policies = {
        profile.model_policy for profile in registry.list_specialists(enabled_only=False)
    }
    assert model_policies <= {
        "default",
        "reasoning",
        "precise",
        "cost_optimized",
        "configured_default",
        "fast",
    }


def test_seed_is_idempotent_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "eurus.sqlite3"
    assert seed_builtin_specialists(SpecialistRegistry(path)) == expected_roles()
    assert seed_builtin_specialists(SpecialistRegistry(path)) == []
    reopened = SpecialistRegistry(path)
    assert seed_builtin_specialists(reopened) == []
    roles = set(expected_roles())
    listed = {specialist.role for specialist in reopened.list_specialists(enabled_only=False)}
    assert roles <= listed


def test_custom_roles_are_preserved_by_seeding(tmp_path: Path) -> None:
    registry = SpecialistRegistry(tmp_path / "eurus.sqlite3")
    registry.register("legal", description="Contract review specialist")
    seed_builtin_specialists(registry)
    assert registry.get("legal") is not None
    assert registry.get("legal").description == "Contract review specialist"
