from __future__ import annotations

import asyncio
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

import click
from rich.console import Console
from rich.table import Table

from eurus import __version__, paths
from eurus.agent.events import AgentEvent
from eurus.agent.factory import build_agent
from eurus.agent.loop import Agent
from eurus.catalog import is_ready, resolve_api_key
from eurus.cli.base import State, _state, main
from eurus.config import LOG_LEVELS, Config, load_config, sample_config
from eurus.diagnose import probe_all
from eurus.dotenv import load_dotenv
from eurus.errors import ConfigError, EurusError
from eurus.evals import TASKS, EvalResult, run_task
from eurus.logs import collect_secrets, setup_logging, shutdown_logging
from eurus.mcp.manager import MCPServerConfig, MCPToolManager
from eurus.models import Message, ModelRef, ToolCall, Usage
from eurus.providers.factory import build_router, resolve_chain
from eurus.providers.router import Router
from eurus.runtime.budgets import BudgetSpec
from eurus.runtime.engine import AgentEngine, RunResult
from eurus.runtime.models import RunStatus, RuntimeEvent
from eurus.runtime.pricing import build_pricing_catalog
from eurus.runtime.resume import RecoveryBlocked, RecoveryManager
from eurus.runtime.state_machine import InvalidRunTransition, transition_run
from eurus.security.approval import approval_request
from eurus.sessions import SessionStore
from eurus.storage.events import EventStore
from eurus.storage.runs import ConcurrentRunUpdate, RunStore
from eurus.tools.base import Tool
from eurus.tools.builtin import default_registry

__all__ = [
    "Agent",
    "AgentEngine",
    "AgentEvent",
    "Any",
    "BudgetSpec",
    "ConcurrentRunUpdate",
    "Config",
    "ConfigError",
    "Console",
    "EurusError",
    "EvalResult",
    "EventStore",
    "InvalidRunTransition",
    "LOG_LEVELS",
    "MCPServerConfig",
    "MCPToolManager",
    "Message",
    "ModelRef",
    "Path",
    "RecoveryBlocked",
    "RecoveryManager",
    "Router",
    "RunResult",
    "RunStatus",
    "RunStore",
    "RuntimeEvent",
    "SessionStore",
    "State",
    "TASKS",
    "Table",
    "Tool",
    "ToolCall",
    "Usage",
    "__version__",
    "_approve",
    "_build_engine",
    "_engine_stores",
    "_render",
    "_run_once",
    "_state",
    "agent_main",
    "annotations",
    "approval_request",
    "asyncio",
    "build_agent",
    "build_pricing_catalog",
    "build_router",
    "click",
    "collect_secrets",
    "common",
    "config_cmds",
    "core",
    "default_registry",
    "evals",
    "gateway",
    "integrations",
    "is_ready",
    "json",
    "load_config",
    "load_dotenv",
    "main",
    "memory",
    "observability",
    "os",
    "paths",
    "platform",
    "probe_all",
    "resolve_api_key",
    "resolve_chain",
    "run",
    "run_task",
    "sample_config",
    "schedule",
    "sessions",
    "skills",
    "setup_logging",
    "shutdown_logging",
    "sys",
    "time",
    "transition_run",
    "ui",
]


from eurus.cli import (
    common,
    config_cmds,
    core,
    evals,
    gateway,
    integrations,
    memory,
    observability,
    run,
    schedule,
    sessions,
    skills,
    ui,
)
from eurus.cli.common import (
    _approve,
    _build_engine,
    _engine_stores,
    _render,
    _run_once,
)
from eurus.cli.run import agent_main

if __name__ == "__main__":
    main()
