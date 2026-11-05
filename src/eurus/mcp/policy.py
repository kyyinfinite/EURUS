from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eurus.mcp.jsonio import atomic_write_text, read_json_dict

READ_ONLY = "read_only"
REVERSIBLE_WRITE = "reversible_write"
SENSITIVE_MUTATION = "sensitive_mutation"
DESTRUCTIVE = "destructive"
UNKNOWN = "unknown"

RISK_LEVELS = (READ_ONLY, REVERSIBLE_WRITE, SENSITIVE_MUTATION, DESTRUCTIVE, UNKNOWN)
_APPROVAL_MANDATORY = {SENSITIVE_MUTATION, DESTRUCTIVE, UNKNOWN}

DESTRUCTIVE_TOKENS: frozenset[str] = frozenset(
    {"delete", "remove", "trash", "purge", "destroy", "erase", "wipe"}
)
SENSITIVE_TOKENS: frozenset[str] = frozenset(
    {
        "send",
        "share",
        "unshare",
        "permission",
        "permissions",
        "acl",
        "grant",
        "revoke",
        "publish",
        "submit",
        "forward",
        "reply",
        "invite",
    }
)
READ_ONLY_FIRST_TOKENS: frozenset[str] = frozenset(
    {
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
    }
)
REVERSIBLE_TOKENS: frozenset[str] = frozenset(
    {
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
        "unlabel",
        "mark",
        "archive",
        "move",
        "append",
        "write",
        "batch",
        "set",
    }
)
READ_ONLY_ANY_TOKENS: frozenset[str] = frozenset({"export", "summarize", "watch"})


@dataclass(frozen=True, slots=True)
class RiskDecision:
    level: str
    reason: str
    mutates: bool
    requires_approval: bool

    @property
    def approval_is_mandatory(self) -> bool:
        return self.level in _APPROVAL_MANDATORY


@dataclass(frozen=True, slots=True)
class ToolAnnotations:
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ToolAnnotations:
        if not data:
            return cls()
        read_only = data.get("readOnlyHint")
        destructive = data.get("destructiveHint")
        return cls(
            read_only_hint=bool(read_only) if isinstance(read_only, bool) else None,
            destructive_hint=bool(destructive) if isinstance(destructive, bool) else None,
        )


def _keyword_level(tool_name: str) -> str | None:
    tokens = tool_name.lower().split("_")
    first = tokens[0] if tokens else ""
    if any(token in DESTRUCTIVE_TOKENS for token in tokens):
        return DESTRUCTIVE
    if any(token in SENSITIVE_TOKENS for token in tokens):
        return SENSITIVE_MUTATION
    if first in READ_ONLY_FIRST_TOKENS:
        return READ_ONLY
    if first in REVERSIBLE_TOKENS or any(token in REVERSIBLE_TOKENS for token in tokens):
        return REVERSIBLE_WRITE
    if any(token in READ_ONLY_ANY_TOKENS for token in tokens):
        return READ_ONLY
    return None


@dataclass(slots=True)
class MCPPolicy:
    read_only_requires_approval: bool = False
    server_patterns: dict[str, dict[str, str]] = field(default_factory=dict)
    path: Path | None = None
    google_access: str = "standard"
    load_warnings: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> MCPPolicy:
        policy = cls(path=path)
        raw, warning = read_json_dict(path)
        if warning:
            policy.load_warnings.append(warning)
        if raw is None:
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
        payload = {
            "read_only_requires_approval": self.read_only_requires_approval,
            "server_patterns": self.server_patterns,
        }
        atomic_write_text(target, json.dumps(payload, indent=2) + "\n")

    def _pattern_rules(self, server_name: str) -> list[tuple[str, str, str]]:
        matched: list[tuple[str, str, str]] = []
        for server_glob, rules in sorted(self.server_patterns.items()):
            if fnmatch.fnmatchcase(server_name, server_glob):
                for pattern, level in rules.items():
                    matched.append((server_glob, pattern, level))
        return matched

    def classify(
        self,
        server_name: str,
        tool_name: str,
        annotations: ToolAnnotations | None = None,
    ) -> RiskDecision:
        for server_glob, pattern, level in self._pattern_rules(server_name):
            if pattern and fnmatch.fnmatchcase(tool_name, pattern):
                return self._decision(level, f"rule {server_glob}:{pattern}")
            if not pattern:
                return self._decision(level, f"rule {server_glob}:*")
        if annotations is not None and (annotations.read_only_hint or annotations.destructive_hint):
            if annotations.destructive_hint:
                return self._decision(DESTRUCTIVE, f"annotation destructiveHint on '{tool_name}'")
            return self._decision(READ_ONLY, f"annotation readOnlyHint on '{tool_name}'")
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

    def tool_flags(
        self,
        server_name: str,
        tool_name: str,
        annotations: ToolAnnotations | None = None,
    ) -> RiskDecision:
        decision = self.classify(server_name, tool_name, annotations)
        if server_name.startswith("google-") and self.google_access == "read" and decision.mutates:
            return RiskDecision(
                decision.level,
                f"access=read floor over {decision.reason}",
                decision.mutates,
                True,
            )
        if (
            server_name.startswith("google-")
            and self.google_access == "full"
            and decision.level == REVERSIBLE_WRITE
        ):
            return RiskDecision(
                decision.level,
                f"access=full floor over {decision.reason}",
                decision.mutates,
                False,
            )
        return decision

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
