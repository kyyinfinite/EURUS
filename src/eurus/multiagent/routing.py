from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from eurus.config import Config
from eurus.multiagent.registry import MODEL_POLICIES

SUPPORTED_MODEL_POLICIES = frozenset(MODEL_POLICIES) | {"cost_optimized"}


def resolve_model_override(payload: Mapping[str, Any], config: Config | None) -> str:
    explicit = str(payload.get("model", "") or "").strip()
    if explicit:
        return explicit
    policy = str(payload.get("model_policy", "") or "").strip()
    if not policy or policy in ("default", "configured_default"):
        return ""
    if policy not in SUPPORTED_MODEL_POLICIES:
        raise ValueError(f"unknown model policy: {policy!r}")
    if config is None:
        return ""
    mapped = config.agent.model_for_policy(policy)
    if mapped:
        return mapped
    return ""
