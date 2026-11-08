from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

FRONTEND_DIST = "frontend/dist"
WHEEL_STATIC_TARGET = "eurus/gateway/web/static"


class EurusFrontendBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        dist = Path(self.root) / FRONTEND_DIST
        if not dist.is_dir() or not (dist / "index.html").is_file():
            self.app.display_warning(
                f"{FRONTEND_DIST} is missing or incomplete; "
                "the wheel/sdist will be built without web UI assets. "
                "Run 'npm ci && npm run build' inside frontend/ first to embed them."
            )
            return
        if self.target_name == "wheel":
            build_data["force_include"][str(dist)] = WHEEL_STATIC_TARGET
        else:
            build_data["force_include"][str(dist)] = FRONTEND_DIST
        self.app.display_info(f"included web UI assets from {FRONTEND_DIST}")
