"""Local web gateway for the canonical EURUS runtime.

This package exposes the existing EURUS agent runtime through a local
FastAPI application and a browser frontend. It deliberately contains no
agent logic of its own: every execution path delegates to
:class:`eurus.runtime.application.AgentRuntime` with
``require_engine=True`` so web-initiated runs use the same durable
:class:`eurus.runtime.engine.AgentEngine` boundary as the CLI, TUI and
Telegram gateway.
"""
