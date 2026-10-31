from pathlib import Path

from eurus.browser.integration import browser_manager_for, registry_manager
from eurus.browser.manager import BrowserManager, BrowserSettings
from eurus.tools.base import ToolRegistry


def test_manager_defaults_to_cwd_without_workspace(tmp_path: Path) -> None:
    manager = BrowserManager(BrowserSettings(enabled=True, backend="cdp"))
    assert manager.workspace == Path.cwd()


def test_rebind_moves_download_root_into_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "agent-workspace"
    workspace.mkdir()
    manager = BrowserManager(BrowserSettings(enabled=True, backend="cdp"))
    assert manager.download_root == Path.cwd() / "downloads"
    manager.set_workspace(workspace)
    assert manager.workspace == workspace
    assert manager.download_root == workspace / "downloads"


def test_rebind_with_same_workspace_is_stable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = BrowserManager(BrowserSettings(enabled=True, backend="cdp"), workspace=workspace)
    manager.set_workspace(workspace)
    assert manager.download_root == workspace / "downloads"


def test_cached_manager_receives_workspace_argument(tmp_path: Path) -> None:
    workspace = tmp_path / "run-workspace"
    workspace.mkdir()
    registry = ToolRegistry()
    first = browser_manager_for(registry, BrowserSettings(enabled=True, backend="cdp"))
    assert first.workspace == Path.cwd()
    second = browser_manager_for(
        registry, BrowserSettings(enabled=True, backend="cdp"), workspace=workspace
    )
    assert second is first
    assert first.workspace == workspace
    assert registry_manager(registry) is first


def test_configured_absolute_download_dir_is_preserved(tmp_path: Path) -> None:
    configured = tmp_path / "custom-downloads"
    manager = BrowserManager(
        BrowserSettings(enabled=True, backend="cdp", download_dir=str(configured)),
        workspace=tmp_path / "workspace",
    )
    assert manager.download_root == configured
