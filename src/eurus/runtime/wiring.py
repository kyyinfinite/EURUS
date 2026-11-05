from __future__ import annotations

from pathlib import Path

from eurus import paths
from eurus.agent.loop import Agent
from eurus.coding.checkpoint import CheckpointStore
from eurus.config import Config
from eurus.models import ToolCall
from eurus.runtime.engine import AgentEngine
from eurus.runtime.pricing import build_pricing_catalog
from eurus.storage.database import Database
from eurus.storage.events import EventStore
from eurus.storage.runs import RunStore
from eurus.storage.tool_attempts import ToolAttemptStore
from eurus.tools.base import Tool


async def deny_remote_approval(call: ToolCall, tool: Tool) -> bool:
    return False


def resolve_database(database: Database | None = None) -> Database:
    if database is not None:
        return database
    paths.home_manager().ensure_layout()
    return Database(paths.database_path())


def build_engine_stores(
    database: Database | None = None,
) -> tuple[Database, RunStore, EventStore, ToolAttemptStore]:
    resolved = resolve_database(database)
    return resolved, RunStore(resolved), EventStore(resolved), ToolAttemptStore(resolved)


def build_agent_engine(
    config: Config,
    agent: Agent,
    *,
    run_store: RunStore,
    event_store: EventStore,
    attempt_store: ToolAttemptStore,
) -> AgentEngine:
    return AgentEngine(
        agent,
        run_store=run_store,
        event_store=event_store,
        attempt_store=attempt_store,
        checkpoint_store=CheckpointStore(),
        pricing=build_pricing_catalog(
            {"updated_at": config.pricing.updated_at, "models": config.pricing.models},
            home=paths.home_dir(),
        ),
    )


def build_agent_engine_from_database(
    config: Config,
    agent: Agent,
    *,
    database: Database | None = None,
) -> tuple[AgentEngine, Database]:
    resolved, runs, events, attempts = build_engine_stores(database)
    return build_agent_engine(
        config, agent, run_store=runs, event_store=events, attempt_store=attempts
    ), resolved


def default_workspace_root() -> Path:
    return Path.cwd()
