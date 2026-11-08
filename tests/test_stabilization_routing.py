from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eurus.config import Config, ConfigError, load_config
from eurus.multiagent.orchestrator import AgentOrchestrator
from eurus.multiagent.registry import MODEL_POLICIES
from eurus.multiagent.routing import resolve_model_override


def config_with_policies(mapping: dict[str, str]) -> Config:
    overrides: dict[str, Any] = {
        "agent": {"model": "openai/gpt-4o", "model_policies": mapping},
        "providers": {
            "openai": {
                "protocol": "openai",
                "base_url": "http://127.0.0.1:9",
                "api_key_env": "OPENAI_API_KEY",
            },
            "groq": {
                "protocol": "openai",
                "base_url": "http://127.0.0.1:9",
                "api_key_env": "GROQ_API_KEY",
            },
        },
    }
    return load_config(overrides=overrides, environ={})


def test_model_policies_loaded_from_config() -> None:
    config = config_with_policies(
        {"fast": "openai/gpt-4o-mini", "reasoning": "groq/llama-3.3-70b-versatile"}
    )
    assert config.agent.model_for_policy("fast") == "openai/gpt-4o-mini"
    assert config.agent.model_for_policy("reasoning") == "groq/llama-3.3-70b-versatile"
    assert config.agent.model_for_policy("precise") is None


def test_unknown_model_policy_in_config_is_rejected() -> None:
    with pytest.raises(ConfigError):
        config_with_policies({"fast": "nosuchprovider/gpt-x"})


def test_registry_accepts_extended_policies() -> None:
    assert "cost_optimized" in MODEL_POLICIES
    assert "configured_default" in MODEL_POLICIES


def test_explicit_model_override_wins_over_policy() -> None:
    payload = {"model_policy": "fast", "model": "openai/gpt-4o"}
    assert resolve_model_override(payload, None) == "openai/gpt-4o"


def test_policy_resolves_through_config_mapping() -> None:
    config = config_with_policies({"fast": "openai/gpt-4o-mini"})
    payload = {"model_policy": "fast"}
    assert resolve_model_override(payload, config) == "openai/gpt-4o-mini"


def test_unmapped_policy_falls_back_to_default_chain() -> None:
    config = config_with_policies({})
    payload = {"model_policy": "fast"}
    assert resolve_model_override(payload, config) == ""


def test_default_and_configured_default_use_the_default_chain() -> None:
    payload = {"model_policy": "configured_default"}
    assert resolve_model_override(payload, None) == ""


def test_unknown_policy_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown model policy"):
        resolve_model_override({"model_policy": "turbo"}, None)


def test_orchestrator_puts_model_policy_into_job_payload(tmp_path: Path) -> None:
    from eurus.automation.engine import AutomationEngine
    from eurus.automation.executor import ExecutionContext, JobExecutor
    from eurus.storage.database import Database
    from eurus.storage.jobs import Job

    class Stub(JobExecutor):
        async def execute(self, job: Job, context: ExecutionContext) -> str:
            return "done"

    database = Database(tmp_path / "routing.sqlite3")
    engine = AutomationEngine(database, Stub(), owner_id="alice", poll_seconds=0.05)
    orchestrator = AgentOrchestrator(database, engine)
    orchestrator.registry.register(
        "analyst",
        description="a",
        model_policy="reasoning",
    )
    assignment = orchestrator.dispatch("analyse", role="analyst", owner_id="alice")
    job = engine.jobs.get(assignment.job_id)
    assert job is not None
    assert job.payload["model_policy"] == "reasoning"
    database.close()
