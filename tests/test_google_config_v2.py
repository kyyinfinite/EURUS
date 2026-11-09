from __future__ import annotations

import json
from pathlib import Path

import pytest

from eurus.mcp.providers.google_workspace.services import (
    CONFIG_VERSION,
    GoogleConfigError,
    GoogleWorkspaceSettings,
)


def test_v1_file_loads_and_migrates_on_save(tmp_path: Path) -> None:
    path = tmp_path / "google-workspace.json"
    path.write_text(
        json.dumps({"services": {"gmail": {"enabled": True, "timeout_seconds": 60}}}),
        encoding="utf-8",
    )
    settings = GoogleWorkspaceSettings.load(path)
    assert settings.is_enabled("gmail")
    assert settings.services["gmail"].timeout_seconds == 60.0
    assert not settings.load_warnings

    settings.save()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == CONFIG_VERSION == 2
    assert payload["services"]["gmail"]["enabled"] is True, "no data may be lost"
    assert payload["services"]["gmail"]["timeout_seconds"] == 60.0


def test_v2_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "google-workspace.json"
    settings = GoogleWorkspaceSettings.defaults()
    settings.preset = "student"
    settings.access = "read"
    settings.startup = "eager"
    settings.oauth_publish_status = "production"
    settings.oauth_redirect_port = 8765
    settings.tools_deny = ["delete_*", "trash_*"]
    settings.set_enabled("drive", True)
    settings.save(path)

    loaded = GoogleWorkspaceSettings.load(path)
    assert loaded.preset == "student"
    assert loaded.access == "read"
    assert loaded.startup == "eager"
    assert loaded.oauth_publish_status == "production"
    assert loaded.oauth_redirect_port == 8765
    assert loaded.tools_deny == ["delete_*", "trash_*"]
    assert loaded.is_enabled("drive")


def test_unknown_top_level_key_is_rejected_with_key_name(tmp_path: Path) -> None:
    path = tmp_path / "google-workspace.json"
    path.write_text(json.dumps({"servises": {}}), encoding="utf-8")
    with pytest.raises(GoogleConfigError, match="servises"):
        GoogleWorkspaceSettings.load(path)


def test_unknown_service_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "google-workspace.json"
    path.write_text(json.dumps({"services": {"gmailx": {"enabled": True}}}), encoding="utf-8")
    with pytest.raises(GoogleConfigError, match="unknown service 'gmailx'"):
        GoogleWorkspaceSettings.load(path)


def test_invalid_enum_values_name_key_and_allowed_values(tmp_path: Path) -> None:
    path = tmp_path / "google-workspace.json"
    path.write_text(json.dumps({"access": "everything"}), encoding="utf-8")
    with pytest.raises(GoogleConfigError, match="'access'"):
        GoogleWorkspaceSettings.load(path)
    path.write_text(json.dumps({"oauth": {"publish_status": "maybe"}}), encoding="utf-8")
    with pytest.raises(GoogleConfigError, match="publish_status"):
        GoogleWorkspaceSettings.load(path)
    path.write_text(json.dumps({"timeout": 9999}), encoding="utf-8")
    with pytest.raises(GoogleConfigError, match="'timeout'"):
        GoogleWorkspaceSettings.load(path)


def test_invalid_service_timeout_names_the_service(tmp_path: Path) -> None:
    path = tmp_path / "google-workspace.json"
    path.write_text(json.dumps({"services": {"gmail": {"timeout_seconds": 999}}}), encoding="utf-8")
    with pytest.raises(GoogleConfigError, match="services.gmail.timeout_seconds"):
        GoogleWorkspaceSettings.load(path)


def test_corrupt_file_is_backed_up_not_silently_replaced(tmp_path: Path) -> None:
    path = tmp_path / "google-workspace.json"
    path.write_text("{oops", encoding="utf-8")
    settings = GoogleWorkspaceSettings.load(path)
    assert not settings.is_enabled("gmail")
    assert settings.load_warnings
    assert list(tmp_path.glob("google-workspace.json.corrupt-*"))


def test_defaults_when_file_missing(tmp_path: Path) -> None:
    settings = GoogleWorkspaceSettings.load(tmp_path / "absent.json")
    assert settings.enabled_ids() == []
    assert settings.oauth_publish_status == "testing"
    assert settings.timeout_for("gmail") == 45.0
