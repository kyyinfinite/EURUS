from __future__ import annotations

from typing import Any, cast

from eurus.tools.base import Tool

BASELINE_TOOL_NAMES = frozenset(
    {
        "calculate",
        "get_time",
        "recall_memory",
        "session_search",
        "plan_task",
    }
)

TOOL_CATEGORY_MAP: dict[str, frozenset[str]] = {
    "calculate": frozenset({"analysis"}),
    "get_time": frozenset({"analysis"}),
    "plan_task": frozenset({"none"}),
    "web_search": frozenset({"search", "web"}),
    "web_fetch": frozenset({"web", "read"}),
    "deep_search": frozenset({"search", "web", "analysis"}),
    "list_files": frozenset({"read", "code"}),
    "read_file": frozenset({"read", "code"}),
    "search_code": frozenset({"search", "read", "code"}),
    "propose_patch": frozenset({"edit", "code"}),
    "edit_file": frozenset({"edit", "write", "code"}),
    "create_file": frozenset({"write", "code"}),
    "apply_patch": frozenset({"edit", "write", "code"}),
    "apply_patchset": frozenset({"edit", "write", "code"}),
    "preview_patchset": frozenset({"edit", "code"}),
    "index_project": frozenset({"read", "code"}),
    "run_checks": frozenset({"test", "code"}),
    "git_status": frozenset({"git", "read"}),
    "git_diff": frozenset({"git", "read"}),
    "git_log": frozenset({"git", "read"}),
    "git_branches": frozenset({"git", "read"}),
    "git_show": frozenset({"git", "read"}),
    "document_read": frozenset({"read"}),
    "document_create": frozenset({"write"}),
    "document_replace": frozenset({"edit", "write"}),
    "office_specialist": frozenset({"write", "analysis"}),
    "shell": frozenset({"shell"}),
    "browser_navigate": frozenset({"browser", "web"}),
    "browser_click": frozenset({"browser"}),
    "browser_type": frozenset({"browser"}),
    "browser_press_key": frozenset({"browser"}),
    "browser_select": frozenset({"browser"}),
    "browser_hover": frozenset({"browser"}),
    "browser_scroll": frozenset({"browser"}),
    "browser_wait_for": frozenset({"browser"}),
    "browser_back": frozenset({"browser"}),
    "browser_tabs": frozenset({"browser"}),
    "browser_snapshot": frozenset({"browser", "read"}),
    "browser_read": frozenset({"browser", "read"}),
    "browser_extract": frozenset({"browser", "read"}),
    "browser_screenshot": frozenset({"browser", "write"}),
    "browser_upload": frozenset({"browser", "write"}),
    "browser_download": frozenset({"browser", "write"}),
    "browser_close": frozenset({"browser"}),
    "browser_page_text": frozenset({"browser", "read"}),
    "browser_read_element": frozenset({"browser", "read"}),
    "browser_set_dialog_policy": frozenset({"browser"}),
    "browser_elements": frozenset({"browser", "read"}),
    "remember_memory": frozenset({"memory", "write"}),
    "remember_observation": frozenset({"memory", "write"}),
    "replace_memory": frozenset({"memory", "edit"}),
    "forget_memory": frozenset({"memory", "write"}),
    "delegate": frozenset({"delegation"}),
    "delegate_task": frozenset({"delegation"}),
    "delegate_code_task": frozenset({"delegation", "code"}),
}


def categories_for_tool(name: str) -> frozenset[str]:
    return TOOL_CATEGORY_MAP.get(name, frozenset())


def describe_tool(tool: Tool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": tool.parameters,
        "categories": sorted(categories_for_tool(tool.name)),
        "mutates": tool.mutates,
        "requires_approval": tool.requires_approval,
        "parallel_safe": tool.parallel_safe,
        "interactive": tool.interactive,
        "required_capabilities": list(tool.required_capabilities),
        "path_keys": list(tool.path_keys),
        "timeout_seconds": tool.timeout_seconds,
        "has_mutation_verifier": tool.verify_mutation is not None,
    }


class ToolExposure:
    def __init__(self, inner: Any, exposed: frozenset[str]) -> None:
        self.inner = inner
        self.exposed = exposed

    def _visible(self, name: str) -> bool:
        return name in self.exposed

    def register(self, tool: Tool) -> None:
        self.inner.register(tool)

    def unregister(self, name: str) -> Tool | None:
        return cast(Tool | None, self.inner.unregister(name))

    def get(self, name: str) -> Tool | None:
        if not self._visible(name):
            return None
        return cast(Tool | None, self.inner.get(name))

    def names(self) -> list[str]:
        return [name for name in self.inner.names() if self._visible(name)]

    def all(self) -> list[Tool]:
        return [tool for tool in self.inner.all() if self._visible(tool.name)]

    def specs(self) -> tuple[Any, ...]:
        return tuple(tool.spec() for tool in self.all())

    def __len__(self) -> int:
        return len(self.all())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self._visible(name) and name in self.inner

    async def execute(self, call: Any, context: Any) -> Any:
        return await self.inner.execute(call, context)


def _plan_categories(task_state: Any) -> frozenset[str]:
    if task_state is None:
        return frozenset()
    categories: set[str] = set()
    steps = getattr(task_state, "steps", None) or []
    for step in steps:
        for category in getattr(step, "tool_categories", ()) or ():
            label = str(category).strip().lower()
            if label:
                categories.add(label)
    return frozenset(categories)


class ToolSelector:
    def __init__(self, *, baseline: frozenset[str] = BASELINE_TOOL_NAMES) -> None:
        self.baseline = frozenset(baseline)

    def exposure(self, registry: Any, task_state: Any) -> Any:
        requested = _plan_categories(task_state)
        if not requested:
            return registry
        exposed: set[str] = set(self.baseline)
        for tool in registry.all():
            name = tool.name
            if not categories_for_tool(name):
                exposed.add(name)
                continue
            if categories_for_tool(name) & requested:
                exposed.add(name)
        return ToolExposure(registry, frozenset(exposed))

    def selection_note(self, task_state: Any, exposed: Any) -> str:
        requested = sorted(_plan_categories(task_state))
        if not requested:
            return ""
        count = len(exposed) if hasattr(exposed, "__len__") else 0
        return f"tool selection: plan categories {requested}; {count} tools exposed this iteration"


__all__ = [
    "BASELINE_TOOL_NAMES",
    "TOOL_CATEGORY_MAP",
    "ToolExposure",
    "ToolSelector",
    "categories_for_tool",
    "describe_tool",
]
