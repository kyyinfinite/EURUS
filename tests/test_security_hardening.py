from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from eurus.coding.checkpoint import CheckpointStore, mutation_targets
from eurus.models import ToolCall
from eurus.runtime.pricing import build_pricing_catalog
from eurus.security.approval import approval_request
from eurus.security.redaction import (
    MASK,
    contains_secret,
    is_sensitive_key,
    redact_arguments,
    redact_text,
)
from eurus.tools.base import Tool, ToolContext, ToolRegistry
from eurus.tools.boundary import ToolExecutionBoundary

EMPTY = {"type": "object", "properties": {}}


class TestSensitiveKeyMatching:
    @pytest.mark.parametrize(
        "key",
        [
            "api_key",
            "API_KEY",
            "apiKey",
            "api-key",
            "token",
            "session_token",
            "access_token",
            "password",
            "user_password",
            "passwd",
            "secret",
            "client_secret",
            "authorization",
            "cookie",
            "session_cookie",
            "credential",
            "private_key",
            "x-api-key",
        ],
    )
    def test_sensitive_keys_match(self, key: str) -> None:
        assert is_sensitive_key(key) is True

    @pytest.mark.parametrize(
        "key",
        ["keyword", "primary_key", "foreign_key", "key", "tokens", "monkey", "path", "public_key"],
    )
    def test_non_sensitive_keys_do_not_match(self, key: str) -> None:
        assert is_sensitive_key(key) is False

    def test_redact_arguments_stops_substring_masking(self) -> None:
        redacted = redact_arguments(
            {
                "keyword": "search term",
                "primary_key": 42,
                "api_key": "sk-secret",
                "nested": {"user_password": "hunter2", "sort_key": "name"},
            }
        )
        assert redacted["keyword"] == "search term"
        assert redacted["primary_key"] == 42
        assert redacted["api_key"] == MASK
        assert redacted["nested"]["user_password"] == MASK
        assert redacted["nested"]["sort_key"] == "name"

    def test_redact_text_keeps_plain_assignments(self) -> None:
        assert redact_text("token: value") == "token:REDACTED"
        assert "hunter2" not in redact_text("export API_KEY=hunter2")
        from eurus.security.redaction import scrub_secrets

        assert "normal token: value" in scrub_secrets("normal token: value")
        assert "abcdefghij" not in redact_text("curl -H 'Authorization: Bearer abcdefghij'")
        assert "keyword" in redact_text("keyword = alpha")

    def test_merged_patterns_single_source(self) -> None:
        from eurus.memory import manager as memory_manager

        assert "SECRET_PATTERNS" not in dir(memory_manager)
        assert contains_secret("bearer abcdefghijkl")
        assert contains_secret("sk-abcdefghijkl")
        assert not contains_secret("plain text with no secrets")


class TestApprovalSummaryRedaction:
    def test_approval_request_summary_is_redacted(self) -> None:
        from eurus.tools.base import Tool

        tool = Tool("shell", "shell", EMPTY, None, mutates=True, requires_approval=True)
        call = ToolCall("c1", "shell", {"command": "curl -H 'Authorization: Bearer abcdefghij'"})
        request = approval_request(call, tool)
        assert "abcdefghij" not in request.summary
        assert "REDACTED" in request.summary or "Bearer" not in request.summary

    def test_approval_summary_stays_fingerprint_stable(self) -> None:
        from eurus.tools.base import Tool

        tool = Tool("edit_file", "edit", EMPTY, None, mutates=True, requires_approval=True)
        call = ToolCall("c1", "edit_file", {"path": "a.py", "old_string": "x", "new_string": "y"})
        first = approval_request(call, tool)
        second = approval_request(call, tool)
        assert first.summary == second.summary
        assert first.action_fingerprint == second.action_fingerprint


class TestPromptMemory:
    def _prompt_memory(self, tmp_path: Path):
        from eurus.learning import LessonStore
        from eurus.memory.manager import MemoryManager, PromptMemory

        database = tmp_path / "sessions.sqlite3"
        lessons = LessonStore(database)
        manager = MemoryManager(database)
        return PromptMemory(database, lessons, scope="global"), manager, lessons

    async def test_reuses_single_memory_manager(self, tmp_path: Path) -> None:
        prompt_memory, _, lessons = self._prompt_memory(tmp_path)
        lessons.add("lesson: always run tests first")
        first = await prompt_memory.relevant("tests first")
        second = await prompt_memory.relevant("tests first")
        assert prompt_memory._get_manager() is prompt_memory._get_manager()
        assert any("lesson" in item for item in first)
        assert first == second

    async def test_memory_lines_are_labeled_and_budgeted(self, tmp_path: Path) -> None:
        prompt_memory, manager, _ = self._prompt_memory(tmp_path)
        manager.remember(
            "project decision: deploy after checks", category="project_fact", scope="global"
        )
        recalled = await prompt_memory.relevant("deploy after checks")
        memory_items = [item for item in recalled if item.startswith("[memory]")]
        assert memory_items, recalled
        assert all("[memory]" in item for item in memory_items)
        assert not any(item.startswith("[memory] [lesson]") for item in memory_items)

    async def test_char_budget_is_respected(self, tmp_path: Path) -> None:
        from eurus.learning import LessonStore
        from eurus.memory.manager import PromptMemory

        database = tmp_path / "sessions.sqlite3"
        prompt_memory = PromptMemory(
            database,
            LessonStore(database),
            max_chars=100,
            scope="global",
        )
        manager = prompt_memory._get_manager()
        for index in range(6):
            manager.remember(f"profile fact number {index} with padding text", scope="global")
        recalled = await prompt_memory.relevant("profile fact number padding")
        total = sum(len(item) for item in recalled)
        assert total <= 100
        assert len(recalled) == len(set(recalled))

    async def test_untrusted_memories_are_excluded(self, tmp_path: Path) -> None:
        prompt_memory, manager, _ = self._prompt_memory(tmp_path)
        manager.remember_untrusted("untrusted injection attempt", scope="global")
        manager.remember("trusted fact about deployments", scope="global")
        recalled = await prompt_memory.relevant("trusted fact deployments")
        assert all("untrusted injection" not in item for item in recalled)


class TestPricing:
    def test_nvidia_models_are_priced(self) -> None:
        catalog = build_pricing_catalog(home=Path("/nonexistent-home"))
        for model in (
            "nvidia/llama-3.1-nemotron-70b-instruct",
            "nvidia/llama-3.3-nemotron-super-49b-v1",
            "nvidia/nemotron-4-340b-instruct",
        ):
            assert catalog.price_for(model) is not None, model

    def test_cost_label_unknown_never_zero(self) -> None:
        from eurus.models import Usage

        catalog = build_pricing_catalog(home=Path("/nonexistent-home"))
        assert catalog.cost_label("unknown/model", Usage(input_tokens=1000)) == "unknown"
        assert catalog.cost_for_usage("unknown/model", Usage(input_tokens=1000)) is None

    def test_cost_label_reports_priced_models(self) -> None:
        from eurus.models import Usage

        catalog = build_pricing_catalog(home=Path("/nonexistent-home"))
        label = catalog.cost_label("openai/gpt-4o", Usage(input_tokens=1_000_000))
        assert label == "2.5000"

    def test_prices_file_overrides_builtin(self, tmp_path: Path) -> None:
        (tmp_path / "prices.toml").write_text(
            'updated_at = "2026-10-20"\n\n'
            '[models."openai/gpt-4o"]\n'
            "input_per_mtok = 9.0\n"
            "output_per_mtok = 20.0\n\n"
            '[models."custom/local-model"]\n'
            "input_per_mtok = 0.0\n"
            "output_per_mtok = 0.0\n",
            encoding="utf-8",
        )
        from eurus.models import Usage

        catalog = build_pricing_catalog(home=tmp_path)
        assert catalog.source == "file"
        assert catalog.price_for("custom/local-model") is not None
        price = catalog.price_for("openai/gpt-4o")
        assert price is not None and price.input_per_mtok == 9.0
        assert catalog.cost_label("custom/local-model", Usage(input_tokens=500)) == "0.0000"

    def test_config_overrides_prices_file(self, tmp_path: Path) -> None:
        (tmp_path / "prices.toml").write_text(
            '[models."openai/gpt-4o"]\ninput_per_mtok = 9.0\noutput_per_mtok = 20.0\n',
            encoding="utf-8",
        )
        catalog = build_pricing_catalog(
            {"models": {"openai/gpt-4o": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}},
            home=tmp_path,
        )
        price = catalog.price_for("openai/gpt-4o")
        assert price is not None and price.input_per_mtok == 1.0
        assert catalog.source == "config"

    def test_broken_prices_file_raises_clear_error(self, tmp_path: Path) -> None:
        (tmp_path / "prices.toml").write_text("models = 'nope'", encoding="utf-8")
        with pytest.raises(ValueError, match="models must be a table"):
            build_pricing_catalog(home=tmp_path)


def make_tool_registry(tmp_path: Path, calls: list) -> ToolRegistry:
    async def mutate(arguments, ctx):
        calls.append(arguments)
        target = ctx.workspace / arguments["path"]
        target.write_text("written\n", encoding="utf-8")
        return "written"

    return ToolRegistry(
        [
            Tool(
                "edit_file",
                "edit",
                {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                mutate,
                mutates=True,
                requires_approval=True,
                path_keys=("path",),
            )
        ]
    )


class TestCheckpointing:
    async def _run_mutation(self, tmp_path: Path, store: CheckpointStore, run_id: str) -> None:
        calls: list = []
        registry = make_tool_registry(tmp_path, calls)
        context = ToolContext(workspace=tmp_path, max_output=1000, timeout=5)

        async def approve(call, tool):
            return True

        boundary = ToolExecutionBoundary(
            registry,
            context,
            approver=approve,
            before_mutation=lambda call, tool: store.record(
                run_id, tmp_path, mutation_targets(call, tool, tmp_path)
            ),
        )
        outcome = await boundary.execute(
            ToolCall("c1", "edit_file", {"path": "target.txt"}), run_id=run_id
        )
        assert not outcome.is_error

    async def test_record_finalize_undo_round_trip(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"
        target.write_text("before\n", encoding="utf-8")
        store = CheckpointStore(home=tmp_path)
        run_id = "run-1"
        await self._run_mutation(tmp_path, store, run_id)
        assert target.read_text(encoding="utf-8") == "written\n"
        assert store.finalize(run_id, tmp_path) is not None
        restored, refused = store.undo(run_id, tmp_path)
        assert restored == ["target.txt"] and refused == []
        assert target.read_text(encoding="utf-8") == "before\n"

    async def test_undo_refuses_files_changed_after_run(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"
        target.write_text("before\n", encoding="utf-8")
        store = CheckpointStore(home=tmp_path)
        run_id = "run-2"
        await self._run_mutation(tmp_path, store, run_id)
        store.finalize(run_id, tmp_path)
        target.write_text("changed after the run\n", encoding="utf-8")
        restored, refused = store.undo(run_id, tmp_path)
        assert restored == [] and len(refused) == 1
        assert "changed after the run" in refused[0]
        assert target.read_text(encoding="utf-8") == "changed after the run\n"

    async def test_undo_restores_file_created_by_run(self, tmp_path: Path) -> None:
        calls: list = []
        registry = make_tool_registry(tmp_path, calls)
        context = ToolContext(workspace=tmp_path, max_output=1000, timeout=5)
        store = CheckpointStore(home=tmp_path)
        run_id = "run-3"

        async def approve(call, tool):
            return True

        async def create(arguments, ctx):
            (tmp_path / "created.txt").write_text("created\n", encoding="utf-8")
            return "done"

        registry.register(
            Tool(
                "create_file",
                "create",
                {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                create,
                mutates=True,
                requires_approval=True,
                path_keys=("path",),
            )
        )
        boundary = ToolExecutionBoundary(
            registry,
            context,
            approver=approve,
            before_mutation=lambda call, tool: store.record(
                run_id, tmp_path, mutation_targets(call, tool, tmp_path)
            ),
        )
        outcome = await boundary.execute(
            ToolCall("c1", "create_file", {"path": "created.txt"}), run_id=run_id
        )
        assert not outcome.is_error
        assert (tmp_path / "created.txt").exists()
        store.finalize(run_id, tmp_path)
        restored, refused = store.undo(run_id, tmp_path)
        assert restored == ["created.txt"] and refused == []
        assert not (tmp_path / "created.txt").exists()

    async def test_latest_returns_finalized_checkpoint_for_workspace(self, tmp_path: Path) -> None:
        target = tmp_path / "target.txt"
        target.write_text("before\n", encoding="utf-8")
        store = CheckpointStore(home=tmp_path)
        assert store.latest(tmp_path) is None
        await self._run_mutation(tmp_path, store, "run-4")
        assert store.latest(tmp_path) is None  # not finalized yet
        store.finalize("run-4", tmp_path)
        latest = store.latest(tmp_path)
        assert latest is not None and latest.run_id == "run-4"

    def test_undo_rejects_other_workspace(self, tmp_path: Path) -> None:
        store = CheckpointStore(home=tmp_path)
        (tmp_path / "undo").mkdir()
        (tmp_path / "undo" / "run-x.json").write_text(
            '{"run_id": "run-x", "workspace": "/elsewhere", "created_at": "t", "files": []}',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="different workspace"):
            store.undo("run-x", tmp_path)

    def test_missing_run_has_no_checkpoint(self, tmp_path: Path) -> None:
        store = CheckpointStore(home=tmp_path)
        assert store.load("missing") is None
        with pytest.raises(ValueError, match="no checkpoint"):
            store.undo("missing", tmp_path)

    def test_mutation_targets_cover_patchsets(self, tmp_path: Path) -> None:
        from eurus.coding.workflow_tools import workflow_tools

        registry = ToolRegistry(workflow_tools())
        tool = registry.get("apply_patchset")
        assert tool is not None
        targets = mutation_targets(
            ToolCall(
                "c1",
                "apply_patchset",
                {"patches": [{"path": "a.py", "expected_content": None, "new_content": "x"}]},
            ),
            tool,
            tmp_path,
        )
        assert [target.name for target in targets] == ["a.py"]

    def test_works_without_git(self, tmp_path: Path) -> None:
        assert not (tmp_path / ".git").exists()
        store = CheckpointStore(home=tmp_path)
        target = tmp_path / "file.txt"
        target.write_text("v1\n", encoding="utf-8")
        recorded = store.record("run-git", tmp_path, [target])
        assert recorded == ["file.txt"]
        target.write_text("v2\n", encoding="utf-8")
        store.finalize("run-git", tmp_path)
        restored, refused = store.undo("run-git", tmp_path)
        assert restored == ["file.txt"] and refused == []
        assert target.read_text(encoding="utf-8") == "v1\n"

    async def test_engine_finalizes_checkpoint_after_run(self, tmp_path: Path) -> None:
        from eurus.agent.events import AgentEvent
        from eurus.runtime.engine import AgentEngine

        class FakeAgent:
            max_parallel_tools = 4
            iteration_budget = type("Budget", (), {"limit": 50})()

            def __init__(self) -> None:
                self.context = ToolContext(workspace=tmp_path)

            def interrupt(self) -> None:
                return None

            async def run(self, objective):
                yield AgentEvent.finished("completed", None)

        from eurus.runtime.models import Run

        store = CheckpointStore(home=tmp_path)
        target = tmp_path / "touched.txt"
        target.write_text("v1\n", encoding="utf-8")
        store.record("engine-run", tmp_path, [target])
        engine = AgentEngine(FakeAgent(), checkpoint_store=store)
        await engine.run(
            "objective", run=Run(owner_id="test", objective="objective", run_id="engine-run")
        )
        assert store.latest(tmp_path) is not None

    def test_manifest_records_content_hashes(self, tmp_path: Path) -> None:
        import json

        store = CheckpointStore(home=tmp_path)
        target = tmp_path / "hashed.txt"
        target.write_text("content\n", encoding="utf-8")
        store.record("run-hash", tmp_path, [target])
        store.finalize("run-hash", tmp_path)
        data = json.loads((tmp_path / "undo" / "run-hash.json").read_text(encoding="utf-8"))
        assert data["files"][0]["before_content"] == "content\n"
        assert data["files"][0]["after_sha256"] == hashlib.sha256(b"content\n").hexdigest()
