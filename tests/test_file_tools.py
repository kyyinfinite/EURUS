from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from pathlib import Path

import pytest

from eurus.coding.tools import coding_tools
from eurus.errors import ToolError
from eurus.models import ToolCall
from eurus.runtime.contracts import ToolExecutionStatus
from eurus.tools.base import Tool, ToolContext, ToolRegistry
from eurus.tools.boundary import ToolExecutionBoundary

EMPTY = {"type": "object", "properties": {}}


def run_tool(registry: ToolRegistry, name: str, arguments: dict, workspace: Path):
    return asyncio.run(
        registry.execute(
            ToolCall("test-call", name, arguments),
            ToolContext(workspace=workspace, max_output=20_000, timeout=5),
        )
    )


def make_registry(workspace: Path) -> ToolRegistry:
    return ToolRegistry(coding_tools())


def context(workspace: Path) -> ToolContext:
    return ToolContext(workspace=workspace, max_output=20_000, timeout=5)


class TestReadFileSlices:
    def test_full_read_reports_total_lines(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("a\nb\nc\n", encoding="utf-8")
        result = run_tool(make_registry(tmp_path), "read_file", {"path": "app.py"}, tmp_path)
        assert not result.is_error
        assert "[read_file] lines 1-3 of 3 total" in result.output
        assert "a\nb\nc" in result.output

    def test_slice_is_one_based_inclusive(self, tmp_path: Path) -> None:
        (tmp_path / "data.txt").write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")
        result = run_tool(
            make_registry(tmp_path),
            "read_file",
            {"path": "data.txt", "start_line": 2, "end_line": 4},
            tmp_path,
        )
        assert not result.is_error
        assert "showing lines 2-4 of 5 total" in result.output
        assert "output truncated" in result.output
        assert "l2\nl3\nl4" in result.output
        assert "l1" not in result.output and "l5" not in result.output

    def test_end_line_beyond_total_is_clamped(self, tmp_path: Path) -> None:
        (tmp_path / "data.txt").write_text("l1\nl2\n", encoding="utf-8")
        result = run_tool(
            make_registry(tmp_path),
            "read_file",
            {"path": "data.txt", "start_line": 2, "end_line": 99},
            tmp_path,
        )
        assert not result.is_error
        assert "showing lines 2-2 of 2 total" in result.output

    def test_start_only_reads_to_end(self, tmp_path: Path) -> None:
        (tmp_path / "data.txt").write_text("l1\nl2\nl3\n", encoding="utf-8")
        result = run_tool(
            make_registry(tmp_path), "read_file", {"path": "data.txt", "start_line": 3}, tmp_path
        )
        assert "showing lines 3-3 of 3 total" in result.output
        assert "l3" in result.output

    def test_invalid_ranges_fail(self, tmp_path: Path) -> None:
        (tmp_path / "data.txt").write_text("l1\nl2\n", encoding="utf-8")
        registry = make_registry(tmp_path)
        bad_start = run_tool(registry, "read_file", {"path": "data.txt", "start_line": 0}, tmp_path)
        assert bad_start.is_error
        inverted = run_tool(
            registry,
            "read_file",
            {"path": "data.txt", "start_line": 2, "end_line": 1},
            tmp_path,
        )
        assert inverted.is_error
        beyond = run_tool(registry, "read_file", {"path": "data.txt", "start_line": 9}, tmp_path)
        assert beyond.is_error and "beyond the end" in beyond.output

    def test_empty_file_reports_zero_lines(self, tmp_path: Path) -> None:
        (tmp_path / "empty.txt").write_text("", encoding="utf-8")
        result = run_tool(make_registry(tmp_path), "read_file", {"path": "empty.txt"}, tmp_path)
        assert not result.is_error
        assert "0 lines" in result.output

    def test_one_megabyte_limit_still_enforced(self, tmp_path: Path) -> None:
        (tmp_path / "big.txt").write_text("x" * 1_000_001, encoding="utf-8")
        result = run_tool(make_registry(tmp_path), "read_file", {"path": "big.txt"}, tmp_path)
        assert result.is_error and "1000000" in result.output

    def test_symlink_rejection_unchanged(self, tmp_path: Path) -> None:
        target = tmp_path / "real.txt"
        target.write_text("secret", encoding="utf-8")
        link = tmp_path / "link.txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symbolic links unavailable on this platform")
        result = run_tool(make_registry(tmp_path), "read_file", {"path": "link.txt"}, tmp_path)
        assert result.is_error and "symbolic links" in result.output


class TestEditFile:
    def test_replaces_single_occurrence(self, tmp_path: Path) -> None:
        path = tmp_path / "code.py"
        path.write_text("def one():\n    return 1\n", encoding="utf-8")
        result = run_tool(
            make_registry(tmp_path),
            "edit_file",
            {"path": "code.py", "old_string": "return 1", "new_string": "return 42"},
            tmp_path,
        )
        assert not result.is_error
        assert path.read_text(encoding="utf-8") == "def one():\n    return 42\n"

    def test_missing_old_string_fails(self, tmp_path: Path) -> None:
        (tmp_path / "code.py").write_text("value = 1\n", encoding="utf-8")
        result = run_tool(
            make_registry(tmp_path),
            "edit_file",
            {"path": "code.py", "old_string": "not here", "new_string": "x"},
            tmp_path,
        )
        assert result.is_error and "not found" in result.output

    def test_ambiguous_old_string_fails_without_replace_all(self, tmp_path: Path) -> None:
        (tmp_path / "code.py").write_text("x = 1\ny = 1\n", encoding="utf-8")
        result = run_tool(
            make_registry(tmp_path),
            "edit_file",
            {"path": "code.py", "old_string": "= 1", "new_string": "= 2"},
            tmp_path,
        )
        assert result.is_error
        assert "2 locations" in result.output
        assert (tmp_path / "code.py").read_text(encoding="utf-8") == "x = 1\ny = 1\n"

    def test_replace_all_updates_every_occurrence(self, tmp_path: Path) -> None:
        (tmp_path / "code.py").write_text("x = 1\ny = 1\n", encoding="utf-8")
        result = run_tool(
            make_registry(tmp_path),
            "edit_file",
            {
                "path": "code.py",
                "old_string": "= 1",
                "new_string": "= 2",
                "replace_all": True,
            },
            tmp_path,
        )
        assert not result.is_error
        assert (tmp_path / "code.py").read_text(encoding="utf-8") == "x = 2\ny = 2\n"

    def test_identical_strings_fail(self, tmp_path: Path) -> None:
        (tmp_path / "code.py").write_text("same\n", encoding="utf-8")
        result = run_tool(
            make_registry(tmp_path),
            "edit_file",
            {"path": "code.py", "old_string": "same", "new_string": "same"},
            tmp_path,
        )
        assert result.is_error and "identical" in result.output

    def test_base_sha256_stale_write_protection(self, tmp_path: Path) -> None:
        path = tmp_path / "code.py"
        path.write_text("value = 1\n", encoding="utf-8")
        stale = hashlib.sha256(b"old content\n").hexdigest()
        result = run_tool(
            make_registry(tmp_path),
            "edit_file",
            {
                "path": "code.py",
                "old_string": "value = 1",
                "new_string": "value = 2",
                "base_sha256": stale,
            },
            tmp_path,
        )
        assert result.is_error and "base_sha256" in result.output
        assert path.read_text(encoding="utf-8") == "value = 1\n"
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        ok = run_tool(
            make_registry(tmp_path),
            "edit_file",
            {
                "path": "code.py",
                "old_string": "value = 1",
                "new_string": "value = 2",
                "base_sha256": current,
            },
            tmp_path,
        )
        assert not ok.is_error
        assert path.read_text(encoding="utf-8") == "value = 2\n"

    def test_safety_flags_and_path_keys(self) -> None:
        tool = ToolRegistry(coding_tools()).get("edit_file")
        assert tool is not None
        assert tool.mutates and tool.requires_approval
        assert tool.path_keys == ("path",)

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        result = run_tool(
            make_registry(tmp_path),
            "edit_file",
            {"path": "missing.py", "old_string": "a", "new_string": "b"},
            tmp_path,
        )
        assert result.is_error and "does not exist" in result.output


class TestCreateFile:
    def test_creates_file_and_parent_dirs_inside_workspace(self, tmp_path: Path) -> None:
        result = run_tool(
            make_registry(tmp_path),
            "create_file",
            {"path": "src/pkg/module.py", "content": "ready = True\n"},
            tmp_path,
        )
        assert not result.is_error
        assert (tmp_path / "src/pkg/module.py").read_text(encoding="utf-8") == "ready = True\n"

    def test_refuses_to_overwrite_without_flag(self, tmp_path: Path) -> None:
        (tmp_path / "doc.md").write_text("original\n", encoding="utf-8")
        result = run_tool(
            make_registry(tmp_path),
            "create_file",
            {"path": "doc.md", "content": "new\n"},
            tmp_path,
        )
        assert result.is_error and "already exists" in result.output
        assert (tmp_path / "doc.md").read_text(encoding="utf-8") == "original\n"

    def test_overwrite_flag_replaces_existing_file(self, tmp_path: Path) -> None:
        (tmp_path / "doc.md").write_text("original\n", encoding="utf-8")
        result = run_tool(
            make_registry(tmp_path),
            "create_file",
            {"path": "doc.md", "content": "new\n", "overwrite": True},
            tmp_path,
        )
        assert not result.is_error
        assert (tmp_path / "doc.md").read_text(encoding="utf-8") == "new\n"

    def test_refuses_paths_outside_workspace(self, tmp_path: Path) -> None:
        result = run_tool(
            make_registry(tmp_path),
            "create_file",
            {"path": "../escape.txt", "content": "x"},
            tmp_path,
        )
        assert result.is_error and "parent directories" in result.output
        assert not (tmp_path.parent / "escape.txt").exists()

    def test_refuses_symlink_target(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "create-outside.txt"
        outside.write_text("original", encoding="utf-8")
        link = tmp_path / "link.md"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symbolic links unavailable on this platform")
        result = run_tool(
            make_registry(tmp_path),
            "create_file",
            {"path": "link.md", "content": "x", "overwrite": True},
            tmp_path,
        )
        assert result.is_error
        assert outside.read_text(encoding="utf-8") == "original"

    def test_enforces_one_megabyte_cap(self, tmp_path: Path) -> None:
        result = run_tool(
            make_registry(tmp_path),
            "create_file",
            {"path": "big.txt", "content": "x" * 1_000_001},
            tmp_path,
        )
        assert result.is_error and "1000000" in result.output
        assert not (tmp_path / "big.txt").exists()

    def test_safety_flags_and_path_keys(self) -> None:
        tool = ToolRegistry(coding_tools()).get("create_file")
        assert tool is not None
        assert tool.mutates and tool.requires_approval
        assert tool.path_keys == ("path",)


class TestSharedHelpers:
    def test_atomic_write_preserves_existing_mode(self, tmp_path: Path) -> None:
        from eurus.coding.paths import atomic_write_text

        path = tmp_path / "script.sh"
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(path, 0o754)
        atomic_write_text(path, "#!/bin/sh\necho hi\n")
        assert path.read_text(encoding="utf-8") == "#!/bin/sh\necho hi\n"
        assert stat.S_IMODE(path.stat().st_mode) == 0o754
        assert not list(tmp_path.glob(".eurus-write-*"))

    def test_atomic_write_cleans_up_temp_on_failure(self, tmp_path: Path) -> None:
        from eurus.coding.paths import atomic_write_text

        path = tmp_path / "file.txt"
        path.write_text("before\n", encoding="utf-8")
        with pytest.raises(TypeError):
            atomic_write_text(path, 123)  # type: ignore[arg-type]
        assert path.read_text(encoding="utf-8") == "before\n"
        assert not list(tmp_path.glob(".eurus-write-*"))

    def test_apply_patch_and_document_tools_use_atomic_replace(self, tmp_path: Path) -> None:
        path = tmp_path / "app.py"
        path.write_text("before\n", encoding="utf-8")
        os.chmod(path, 0o640)
        result = run_tool(
            make_registry(tmp_path),
            "apply_patch",
            {"path": "app.py", "expected_content": "before\n", "new_content": "after\n"},
            tmp_path,
        )
        assert not result.is_error
        assert stat.S_IMODE(path.stat().st_mode) == 0o640

    def test_apply_patch_gains_path_keys(self) -> None:
        tool = ToolRegistry(coding_tools()).get("apply_patch")
        assert tool is not None
        assert tool.path_keys == ("path",)

    def test_resolver_rejects_traversal_symlink_and_absolute(self, tmp_path: Path) -> None:
        from eurus.coding.paths import resolve_workspace_path

        with pytest.raises(ToolError):
            resolve_workspace_path(tmp_path, "../x", label="path")
        with pytest.raises(ToolError):
            resolve_workspace_path(tmp_path, "/etc/passwd", label="path")
        link = tmp_path / "link"
        target = tmp_path.parent / "resolver-target"
        target.mkdir(exist_ok=True)
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symbolic links unavailable on this platform")
        with pytest.raises(ToolError, match="symbolic links"):
            resolve_workspace_path(tmp_path, "link/file.txt", label="path")


class TestBoundaryFinishedEvents:
    def _boundary(self, tmp_path: Path, tool: Tool, events: list) -> ToolExecutionBoundary:
        def emit(event_type: str, payload: dict) -> None:
            events.append((event_type, payload.get("status")))

        async def approve(call, tool):
            return True

        return ToolExecutionBoundary(
            ToolRegistry([tool]), context(tmp_path), approver=approve, event_recorder=emit
        )

    def test_started_and_finished_pairing_on_success(self, tmp_path: Path) -> None:
        async def handler(arguments, ctx):
            return "ok"

        events: list = []
        boundary = self._boundary(tmp_path, Tool("t", "t", EMPTY, handler), events)
        outcome = asyncio.run(boundary.execute(ToolCall("c1", "t", {})))
        assert outcome.status is ToolExecutionStatus.SUCCESS
        names = [name for name, _ in events]
        assert names.count("tool.started") == 1
        assert names.count("tool.finished") == 1
        assert names.index("tool.started") < names.index("tool.finished")
        assert events[1][1] == "success"

    def test_finished_emitted_on_tool_error(self, tmp_path: Path) -> None:
        async def handler(arguments, ctx):
            raise ToolError("boom")

        events: list = []
        boundary = self._boundary(tmp_path, Tool("t", "t", EMPTY, handler), events)
        outcome = asyncio.run(boundary.execute(ToolCall("c1", "t", {})))
        assert outcome.status is ToolExecutionStatus.FAILED
        names = [name for name, _ in events]
        assert names.count("tool.started") == 1
        assert names.count("tool.finished") == 1
        assert names.index("tool.started") < names.index("tool.finished")
        assert events[1][1] == "failed"

    def test_finished_emitted_on_crash(self, tmp_path: Path) -> None:
        async def handler(arguments, ctx):
            raise RuntimeError("unexpected")

        events: list = []
        boundary = self._boundary(tmp_path, Tool("t", "t", EMPTY, handler), events)
        outcome = asyncio.run(boundary.execute(ToolCall("c1", "t", {})))
        assert outcome.status is ToolExecutionStatus.FAILED
        names = [name for name, _ in events]
        assert names.count("tool.started") == 1
        assert names.count("tool.finished") == 1
        assert names.index("tool.started") < names.index("tool.finished")
        assert events[1][1] == "failed"

    def test_finished_emitted_on_timeout(self, tmp_path: Path) -> None:
        async def handler(arguments, ctx):
            await asyncio.sleep(5)

        events: list = []
        boundary = self._boundary(tmp_path, Tool("t", "t", EMPTY, handler), events)
        outcome = asyncio.run(boundary.execute(ToolCall("c1", "t", {})))
        assert outcome.status is ToolExecutionStatus.TIMEOUT
        names = [name for name, _ in events]
        assert names.count("tool.started") == 1
        assert names.count("tool.finished") == 1
        assert names.index("tool.started") < names.index("tool.finished")
        assert next(value for name, value in events if name == "tool.finished") == "timeout"

    async def test_finished_emitted_on_cancelled_mutation(self, tmp_path: Path) -> None:
        async def handler(arguments, ctx):
            await asyncio.sleep(5)

        events: list = []
        boundary = self._boundary(tmp_path, Tool("t", "t", EMPTY, handler, mutates=True), events)
        task = asyncio.create_task(boundary.execute(ToolCall("c1", "t", {})))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        names = [name for name, _ in events]
        assert names.count("tool.started") == 1
        assert names.count("tool.finished") == 1
        assert names.index("tool.started") < names.index("tool.finished")
        status = next(value for name, value in events if name == "tool.finished")
        assert status == "cancelled"

    async def test_read_only_cancelled_also_reports_finished(self, tmp_path: Path) -> None:
        async def handler(arguments, ctx):
            await asyncio.sleep(5)

        events: list = []
        boundary = self._boundary(tmp_path, Tool("t", "t", EMPTY, handler), events)
        task = asyncio.create_task(boundary.execute(ToolCall("c1", "t", {})))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        names = [name for name, _ in events]
        assert names == ["tool.started", "tool.finished"]
        status = next(value for name, value in events if name == "tool.finished")
        assert status == "cancelled"
