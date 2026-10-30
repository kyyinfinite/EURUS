"""Tests for the MCP risk-classification and approval policy engine."""

from __future__ import annotations

from pathlib import Path

from eurus.mcp.policy import (
    DESTRUCTIVE,
    READ_ONLY,
    REVERSIBLE_WRITE,
    SENSITIVE_MUTATION,
    UNKNOWN,
    MCPPolicy,
    default_policy_path,
    ensure_policy_file,
    risk_tag,
)


def test_google_read_only_tools_do_not_require_approval_by_default() -> None:
    policy = MCPPolicy()
    for tool in ("search_threads", "list_drafts", "get_message", "read_file", "find_files"):
        decision = policy.classify("google-gmail", tool)
        assert decision.level == READ_ONLY, tool
        assert decision.mutates is False
        assert decision.requires_approval is False


def test_google_sensitive_and_destructive_always_require_approval() -> None:
    policy = MCPPolicy()
    cases = {
        "send_message": SENSITIVE_MUTATION,
        "share_file": SENSITIVE_MUTATION,
        "create_permission": SENSITIVE_MUTATION,
        "delete_thread": DESTRUCTIVE,
        "trash_file": DESTRUCTIVE,
        "empty_trash": DESTRUCTIVE,
    }
    for tool, expected in cases.items():
        decision = policy.classify("google-gmail", tool)
        assert decision.level == expected, tool
        assert decision.mutates is True
        assert decision.requires_approval is True
        assert decision.approval_is_mandatory is True


def test_google_reversible_write_requires_approval() -> None:
    policy = MCPPolicy()
    decision = policy.classify("google-gmail", "create_draft")
    assert decision.level == REVERSIBLE_WRITE
    assert decision.mutates is True
    assert decision.requires_approval is True


def test_external_servers_keep_fail_closed_default() -> None:
    policy = MCPPolicy()
    for server in ("remote", "my-server", "example"):
        decision = policy.classify(server, "search_files")
        assert decision.level == UNKNOWN
        assert decision.mutates is True
        assert decision.requires_approval is True


def test_explicit_rules_override_keywords_and_apply_to_any_server() -> None:
    policy = MCPPolicy()
    policy.set_server_rule("remote", "search_*", READ_ONLY)
    decision = policy.classify("remote", "search_files")
    assert decision.level == READ_ONLY
    assert decision.requires_approval is False
    # other tools on that server remain fail-closed
    assert policy.classify("remote", "whatever").level == UNKNOWN


def test_read_only_approval_can_be_tightened_but_not_the_mandatory_levels() -> None:
    policy = MCPPolicy(read_only_requires_approval=True)
    assert policy.classify("google-gmail", "search_threads").requires_approval is True
    # sensitive/destructive stay mandatory either way
    strict = MCPPolicy(read_only_requires_approval=False)
    assert strict.classify("google-gmail", "send_message").requires_approval is True


def test_policy_save_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "mcp-policies.json"
    policy = MCPPolicy()
    policy.set_server_rule("google-gmail", "send_*", SENSITIVE_MUTATION)
    policy.read_only_requires_approval = True
    policy.save(path)
    loaded = MCPPolicy.load(path)
    assert loaded.read_only_requires_approval is True
    assert loaded.server_patterns == {"google-gmail": {"send_*": SENSITIVE_MUTATION}}
    assert loaded.classify("google-gmail", "send_message").level == SENSITIVE_MUTATION


def test_policy_load_tolerates_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    policy = MCPPolicy.load(path)
    assert policy.classify("google-gmail", "search_x").level == READ_ONLY


def test_invalid_rule_level_is_rejected() -> None:
    policy = MCPPolicy()
    try:
        policy.set_server_rule("x", "*", "ultra-danger")
        raise AssertionError("invalid level must raise")
    except ValueError:
        pass


def test_risk_tag_format() -> None:
    assert risk_tag(READ_ONLY) == "[mcp risk: read_only]"


def test_ensure_policy_file_creates_private_file(tmp_path: Path) -> None:
    path = ensure_policy_file(tmp_path)
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600
    assert default_policy_path(tmp_path) == tmp_path / "security" / "mcp-policies.json"


def test_describe_lists_rules(tmp_path: Path) -> None:
    policy = MCPPolicy()
    assert policy.describe() == []
    policy.set_server_rule("google-drive", "delete_*", DESTRUCTIVE)
    rows = policy.describe()
    assert rows == [
        {
            "server": "google-drive",
            "tool_pattern": "delete_*",
            "risk_level": DESTRUCTIVE,
            "approval": "required",
        }
    ]
    assert policy.remove_server("google-drive") is True
    assert policy.remove_server("google-drive") is False
