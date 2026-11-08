from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    os.environ["EURUS_HOME"] = tempfile.mkdtemp(prefix="eurus-tokens-")
    from eurus.tools.builtin import default_registry

    registry = default_registry(include_capability_tools=True, include_workflow_tools=True)
    before_specs = [tool.spec() for tool in registry.all()]
    before_chars = sum(
        len(
            json.dumps(
                {"name": spec.name, "description": spec.description, "parameters": spec.parameters},
                ensure_ascii=False,
            )
        )
        for spec in before_specs
    )

    from eurus.schedule_tools import schedule_tool

    registry.register(schedule_tool(Path(os.environ["EURUS_HOME"]) / "schedules.sqlite3"))
    after_specs = [tool.spec() for tool in registry.all()]
    after_chars = sum(
        len(
            json.dumps(
                {"name": spec.name, "description": spec.description, "parameters": spec.parameters},
                ensure_ascii=False,
            )
        )
        for spec in after_specs
    )
    schedule_chars = after_chars - before_chars
    print(f"tools_before={len(before_specs)}")
    print(f"tools_after={len(after_specs)}")
    print(f"schema_chars_before={before_chars}")
    print(f"schema_chars_after={after_chars}")
    print(f"schedule_tool_chars={schedule_chars}")
    print(f"schema_tokens_before≈{before_chars // 4}")
    print(f"schema_tokens_after≈{after_chars // 4}")
    print(f"schedule_tool_tokens≈{schedule_chars // 4}")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
