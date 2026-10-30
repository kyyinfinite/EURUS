"""Centralized MCP security and approval policies.

Every MCP tool that Eurus registers - from external servers or from the native
Google Workspace provider - passes through this policy engine. The engine maps
a (server, remote tool) pair to a risk level and derives the ToolRegistry
flags that feed the existing approval boundary:

    READ_ONLY           -> mutates=False, approval follows the user policy
    REVERSIBLE_WRITE    -> mutates=True,  approval required
    SENSITIVE_MUTATION  -> mutates=True,  approval required (cannot be disabled)
    DESTRUCTIVE         -> mutates=True,  approval required (cannot be disabled)
    UNKNOWN             -> mutates=True,  approval required

The model never sees or overrides these classifications; they are applied at
tool-registration time, before any agent loop runs.
"""

from __future__ import annotations

import fnmatch
import json
import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

READ_ONLY = "read_only"
REVERSIBLE_WRITE = "reversible_write"
SENSITIVE_MUTATION = "sensitive_mutation"
DESTRUCTIVE = "destructive"
UNKNOWN = "unknown"

RISK_LEVELS = (READ_ONLY, REVERSIBLE_WRITE, SENSITIVE_MUTATION, DESTRUCTIVE, UNKNOWN)
_APPROVAL_MANDATORY = {SENSITIVE_MUTATION, DESTRUCTIVE, UNKNOWN}

# Ordered classification applied to remote tool names when no explicit rule
# matches. Google tool families use verb-first names (search_threads,
# create_draft, send_message), so destructive and sensitive verbs win over
# everything, read-only prefixes beat write keywords, and write keywords beat
# trailing read-only words.
DESTRUCTIVE_KEYWORDS: tuple[str, ...] = (
    "delete",
    "remove",
    "trash",
    "purge",
    "destroy",
    "erase",
    "wipe",
    "empty_trash",
)
SENSITIVE_KEYWORDS: tuple[str, ...] = (
    "send",
    "share",
    "unshare",
    "permission",
    "acl",
    "grant",
    "revoke",
    "publish",
    "submit",
    "forward",
    "reply",
    "invite",
    "make_public",
    "move_out",
    "escalate",
    "externalize",
)
READ_ONLY_PREFIXES: tuple[str, ...] = (
    "search",
    "list",
    "get",
    "read",
    "fetch",
    "find",
    "query",
    "view",
    "show",
    "describe",
    "inspect",
    "count",
    "download",
)
REVERSIBLE_WRITE_KEYWORDS: tuple[str, ...] = (
    "create",
    "draft",
    "insert",
    "add",
    "update",
    "patch",
    "edit",
    "modify",
    "upload",
    "rename",
    "label",
    "mark",
    "archive",
    "move",
    "append",
    "write",
    "batch",
    "set_",
)
READ_ONLY_KEYWORDS: tuple[str, ...] = (
    "export",
    "summarize",
    "watch",
)


@dataclass(frozen=True, slots=True)
class RiskDecision:
    level: str
    reason: str
    mutates: bool
    requires_approval: bool

    @property
    def approval_is_mandatory(self) -> bool:
        return self.level in _APPROVAL_MANDATORY


def _keyword_level(tool_name: str) -> str | None:
    lowered = tool_name.lower()
    for keyword in DESTRUCTIVE_KEYWORDS:
        if keyword in lowered:
            return DESTRUCTIVE
    for keyword in SENSITIVE_KEYWORDS:
        if keyword in lowered:
            return SENSITIVE_MUTATION
    for prefix in READ_ONLY_PREFIXES:
        if lowered.startswith(prefix):
            return READ_ONLY
    for keyword in REVERSIBLE_WRITE_KEYWORDS:
        if keyword in lowered:
            return REVERSIBLE_WRITE
    for keyword in READ_ONLY_KEYWORDS:
        if keyword in lowered:
            return READ_ONLY
    return None


@dataclass(slots=True)
class MCPPolicy:
    """Risk classification and approval policy for MCP tools."""

    read_only_requires_approval: bool = False
    server_patterns: dict[str, dict[str, str]] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def load(cls, path: Path) -> MCPPolicy:
        policy = cls(path=path)
        if not path.exists():
            return policy
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return policy
        if not isinstance(raw, dict):
            return policy
        read_only = raw.get("read_only_requires_approval")
        if isinstance(read_only, bool):
            policy.read_only_requires_approval = read_only
        patterns = raw.get("server_patterns")
        if isinstance(patterns, dict):
            for server, rules in patterns.items():
                if isinstance(server, str) and isinstance(rules, dict):
                    policy.server_patterns[server] = {
                        str(pattern): str(level)
                        for pattern, level in rules.items()
                        if str(level) in RISK_LEVELS
                    }
        return policy

    def save(self, path: Path | None = None) -> None:
        target = path or self.path
        if target is None:
            raise ValueError("policy path is not configured")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.with_suffix(target.suffix + ".tmp")
        payload = {
            "read_only_requires_approval": self.read_only_requires_approval,
            "server_patterns": self.server_patterns,
        }
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        with suppress(OSError):
            os.chmod(temporary, 0o600)
        temporary.replace(target)

    def _pattern_rules(self, server_name: str) -> list[tuple[str, str, str]]:
        matched: list[tuple[str, str, str]] = []
        for server_glob, rules in sorted(self.server_patterns.items()):
            if fnmatch.fnmatchcase(server_name, server_glob):
                for pattern, level in rules.items():
                    matched.append((server_glob, pattern, level))
        return matched

    def classify(self, server_name: str, tool_name: str) -> RiskDecision:
        for server_glob, pattern, level in self._pattern_rules(server_name):
            if pattern and fnmatch.fnmatchcase(tool_name, pattern):
                return self._decision(level, f"rule {server_glob}:{pattern}")
            if not pattern:
                return self._decision(level, f"rule {server_glob}:*")
        # Keyword heuristics cover the native Google Workspace tool families.
        # Tools from other external servers keep the historical fail-closed
        # behavior (approval required) unless the user adds explicit rules.
        if server_name.startswith("google-"):
            keyword = _keyword_level(tool_name)
            if keyword is not None:
                return self._decision(keyword, f"keyword match on '{tool_name}'")
        return self._decision(UNKNOWN, "no rule matched; treated as unknown")

    def _decision(self, level: str, reason: str) -> RiskDecision:
        if level in _APPROVAL_MANDATORY:
            return RiskDecision(level, reason, True, True)
        if level == READ_ONLY:
            return RiskDecision(level, reason, False, self.read_only_requires_approval)
        return RiskDecision(level, reason, True, True)

    def tool_flags(self, server_name: str, tool_name: str) -> RiskDecision:
        return self.classify(server_name, tool_name)

    def set_server_rule(self, server_glob: str, pattern: str, level: str) -> None:
        if level not in RISK_LEVELS:
            raise ValueError(f"unknown risk level: {level}")
        rules = self.server_patterns.setdefault(server_glob, {})
        if not pattern and level == UNKNOWN:
            rules.pop("", None)
        else:
            rules[pattern] = level

    def remove_server(self, server_glob: str) -> bool:
        return self.server_patterns.pop(server_glob, None) is not None

    def describe(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for server_glob, rules in sorted(self.server_patterns.items()):
            for pattern, level in sorted(rules.items()):
                rows.append(
                    {
                        "server": server_glob,
                        "tool_pattern": pattern or "*",
                        "risk_level": level,
                        "approval": "required"
                        if level in _APPROVAL_MANDATORY or self.read_only_requires_approval
                        else "not required"
                        if level == READ_ONLY
                        else "required",
                    }
                )
        return rows


def risk_tag(level: str) -> str:
    return f"[mcp risk: {level}]"


def default_policy_path(home: Path) -> Path:
    return home / "security" / "mcp-policies.json"


def ensure_policy_file(home: Path) -> Path:
    path = default_policy_path(home)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        MCPPolicy().save(path)
    return path
