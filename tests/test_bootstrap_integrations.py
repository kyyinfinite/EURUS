import json
import os
import stat

from eurus.bootstrap.integrations.browser import (
    browser_check_result,
    classify_candidate,
    detect_browser,
    package_state,
)
from eurus.bootstrap.integrations.documents import documents_check_result, inspect_documents
from eurus.bootstrap.integrations.mcp import (
    ensure_mcp_file,
    inspect_mcp,
    mcp_check_result,
    mcp_config_path,
)
from eurus.bootstrap.integrations.telegram import (
    inspect_telegram,
    save_allowlist,
    save_telegram_token,
    telegram_check_result,
    validate_telegram_token,
)
from eurus.bootstrap.models import BROKEN, MISSING, OPTIONAL, READY, UNKNOWN


def _environ(tmp_path, **extra):
    env = {"EURUS_HOME": str(tmp_path / "eurus-home")}
    env.update(extra)
    return env


def test_ensure_mcp_file_creates_empty_server_list(tmp_path):
    environ = _environ(tmp_path)
    path = ensure_mcp_file(environ)
    assert path == mcp_config_path(environ)
    assert json.loads(path.read_text(encoding="utf-8")) == {"servers": []}
    again = ensure_mcp_file(environ)
    assert again == path


def test_inspect_mcp_empty_is_valid(tmp_path):
    report = inspect_mcp(_environ(tmp_path))
    assert report.valid is True
    assert report.servers == []
    assert report.error == ""


def test_inspect_mcp_invalid_json_reports_error(tmp_path):
    environ = _environ(tmp_path)
    home = tmp_path / "eurus-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text("{broken", encoding="utf-8")
    report = inspect_mcp(environ)
    assert report.valid is False
    assert report.error


def test_inspect_mcp_rejects_non_dict_payload(tmp_path):
    environ = _environ(tmp_path)
    home = tmp_path / "eurus-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text("[1, 2, 3]", encoding="utf-8")
    report = inspect_mcp(environ)
    assert report.valid is False


def test_inspect_mcp_lists_servers_with_missing_executable(tmp_path):
    environ = _environ(tmp_path)
    home = tmp_path / "eurus-home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "mcp.json").write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "ghost",
                        "transport": "stdio",
                        "command": "definitely-missing-binary-xyz",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    report = inspect_mcp(environ)
    assert report.valid is True
    assert len(report.servers) == 1
    item = report.servers[0]
    assert item.name == "ghost"
    assert item.transport == "stdio"
    assert item.executable_found == MISSING


def test_mcp_check_result_states(tmp_path):
    from eurus.bootstrap.integrations.mcp import MCPSetupReport

    assert mcp_check_result(MCPSetupReport(path="p", valid=False, error="bad")).status == BROKEN
    assert mcp_check_result(MCPSetupReport(path="p", valid=True)).status == OPTIONAL


def test_mcp_check_result_broken_servers(tmp_path):
    from eurus.bootstrap.integrations.mcp import MCPServerReport, MCPSetupReport

    report = MCPSetupReport(
        path="p",
        valid=True,
        servers=[
            MCPServerReport(
                name="ghost", transport="stdio", enabled=True, target="", executable_found=MISSING
            )
        ],
    )
    result = mcp_check_result(report)
    assert result.status == BROKEN
    assert "ghost" in result.detail


def test_inspect_telegram_without_token(tmp_path):
    report = inspect_telegram(_environ(tmp_path))
    assert report.token_present is False
    assert report.token_masked == ""
    assert report.allowlist == ""
    assert report.aiogram in {READY, MISSING, BROKEN, UNKNOWN}


def test_inspect_telegram_masks_existing_token(tmp_path):
    environ = _environ(tmp_path)
    save_telegram_token(environ, "123456789:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-4c")
    report = inspect_telegram(environ)
    assert report.token_present is True
    assert report.token_valid_format is True
    assert "AAEh" not in report.token_masked
    assert "(not set)" not in report.token_masked


def test_save_telegram_token_writes_private_env(tmp_path):
    environ = _environ(tmp_path)
    path = save_telegram_token(environ, "123456789:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-4c")
    assert path.name == ".env"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert environ["EURUS_TELEGRAM_BOT_TOKEN"].startswith("123456789:")


def test_save_allowlist(tmp_path):
    environ = _environ(tmp_path)
    save_allowlist(environ, "111, 222")
    assert environ["EURUS_TELEGRAM_ALLOWED_USER_IDS"] == "111,222"
    report = inspect_telegram(environ)
    assert report.allowlist == "111,222"


def test_validate_telegram_token_shared_with_secrets():
    assert validate_telegram_token("123456789:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-4c") == ""
    assert validate_telegram_token("garbage") != ""


def test_telegram_check_result_without_token(tmp_path):
    report = inspect_telegram(_environ(tmp_path))
    result = telegram_check_result(report)
    assert result.status in {OPTIONAL, MISSING}
    assert result.optional is True


def test_telegram_check_result_with_token(tmp_path):
    environ = _environ(tmp_path)
    save_telegram_token(environ, "123456789:AAEhBOweik6ad9r_QXMENQjcrGbqCr4K-4c")
    result = telegram_check_result(inspect_telegram(environ))
    assert result.status == READY


def test_package_state_for_installed_and_missing():
    assert package_state("httpx") == READY
    assert package_state("definitely-not-a-real-package-xyz") == MISSING


def test_classify_candidate_kinds(tmp_path):
    assert classify_candidate("") == "system"
    assert (
        classify_candidate("/home/z/.cache/ms-playwright/chromium-1200/chrome-linux/chrome")
        == "playwright-managed"
    )
    assert classify_candidate("/usr/bin/chromium") == "system"


def test_detect_browser_reports_this_host(tmp_path):
    report = detect_browser(_environ(tmp_path))
    assert report.playwright_package in {READY, MISSING, BROKEN, UNKNOWN}
    assert report.websockets_package in {READY, MISSING, BROKEN, UNKNOWN}
    assert report.chromium_kind in {"system", "playwright-managed", "missing"}
    assert report.backend_recommendation
    payload = report.to_dict()
    assert payload["chromium_kind"] == report.chromium_kind


def test_detect_browser_missing_everything(tmp_path):
    environ = _environ(tmp_path, EURUS_CHROMIUM=str(tmp_path / "nope"))
    report = detect_browser(environ)
    assert report.chromium_kind == "missing"
    assert report.chromium_path == ""


def test_browser_check_result_missing_chromium(tmp_path):
    report = detect_browser(_environ(tmp_path, EURUS_CHROMIUM=str(tmp_path / "nope")))
    result = browser_check_result(report)
    assert result.status in {OPTIONAL, MISSING}
    assert result.hint


def test_inspect_documents_runs_creation_test():
    report = inspect_documents()
    assert report.docx in {READY, MISSING, BROKEN, UNKNOWN}
    assert report.creation_test in {READY, BROKEN, UNKNOWN}
    assert isinstance(report.detail, str)
    assert report.to_dict()["docx"] == report.docx


def test_documents_check_result_states():
    from eurus.bootstrap.integrations.documents import DocumentsReport

    missing = DocumentsReport(docx=MISSING, openpyxl=MISSING, pptx=MISSING)
    assert documents_check_result(missing).status == OPTIONAL
    broken = DocumentsReport(docx=READY, openpyxl=READY, pptx=READY, creation_test=BROKEN)
    result = documents_check_result(broken)
    assert result.status == BROKEN
    ready = DocumentsReport(docx=READY, openpyxl=READY, pptx=READY, creation_test=READY)
    assert documents_check_result(ready).status == READY


def test_inspect_documents_matches_this_environment():
    report = inspect_documents()
    if os.environ.get("EURUS_SKIP_OPTIONAL") == "1":
        import pytest

        pytest.skip("optional documents packages skipped")
    assert report.creation_test in {READY, UNKNOWN}
