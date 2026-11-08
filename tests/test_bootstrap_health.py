import json
import os
import stat

from eurus.bootstrap.health.checks import (
    check_configuration,
    check_core_dependencies,
    check_database,
    check_env_permissions,
    check_optional_dependencies,
    check_platform,
    check_provider,
    check_python,
    check_startup,
    check_storage,
    check_venv,
    run_checks,
)
from eurus.bootstrap.health.repair import (
    REPAIR_ORDER,
    actions_for,
    apply_repair_action,
    execute_repair,
)
from eurus.bootstrap.health.report import render_report, report_json, report_text
from eurus.bootstrap.models import (
    BROKEN,
    MISSING,
    OPTIONAL,
    READY,
    CheckResult,
    RepairAction,
)


def _environ(tmp_path, **extra):
    env = {"EURUS_HOME": str(tmp_path / "eurus-home")}
    env.update(extra)
    return env


def test_check_platform_reports_ready(tmp_path):
    result = check_platform(_environ(tmp_path))
    assert result.status == READY


def test_check_python_ready_on_supported_runtime(tmp_path):
    result = check_python(_environ(tmp_path))
    assert result.status == READY


def test_check_venv_reports_state(tmp_path):
    result = check_venv(_environ(tmp_path))
    assert result.status in {READY, OPTIONAL}


def test_check_core_dependencies_all_ready(tmp_path):
    result = check_core_dependencies(_environ(tmp_path))
    assert result.status == READY


def test_check_optional_dependencies_reports_features(tmp_path):
    result = check_optional_dependencies(_environ(tmp_path))
    assert result.status in {READY, OPTIONAL}
    assert result.optional is True


def test_check_configuration_missing_when_no_file(tmp_path):
    result = check_configuration(_environ(tmp_path))
    assert result.status == MISSING
    assert result.repair_key == "config_init"


def test_check_configuration_ready_after_repair(tmp_path):
    environ = _environ(tmp_path)
    apply_repair_action(
        RepairAction(key="config_init", title="t", description="", kind="config_init"), environ
    )
    result = check_configuration(environ)
    assert result.status == READY


def test_check_configuration_broken_on_invalid_toml(tmp_path):
    environ = _environ(tmp_path)
    config_path = environ["EURUS_HOME"] + "/config.toml"
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as handle:
        handle.write("not [ valid toml")
    result = check_configuration(environ)
    assert result.status == BROKEN
    assert result.repair_key == "config_repair"


def test_check_storage_read_only_reports_missing_without_creating(tmp_path):
    environ = _environ(tmp_path)
    result = check_storage(environ)
    assert result.status == MISSING
    assert result.repair_key == "home_dirs"
    assert not os.path.exists(environ["EURUS_HOME"])


def test_check_storage_ready_on_existing_private_home(tmp_path):
    environ = _environ(tmp_path)
    os.makedirs(environ["EURUS_HOME"], mode=0o700)
    result = check_storage(environ)
    assert result.status == READY
    assert os.listdir(environ["EURUS_HOME"]) == []


def test_check_storage_broken_on_world_readable_home(tmp_path):
    environ = _environ(tmp_path)
    os.makedirs(environ["EURUS_HOME"], mode=0o755)
    result = check_storage(environ)
    assert result.status == BROKEN
    assert result.repair_key == "env_perms_home"


def test_check_env_permissions_states(tmp_path):
    environ = _environ(tmp_path)
    assert check_env_permissions(environ).status == OPTIONAL
    env_path = environ["EURUS_HOME"] + "/.env"
    os.makedirs(os.path.dirname(env_path), exist_ok=True)
    with open(env_path, "w", encoding="utf-8") as handle:
        handle.write("KEY=1\n")
    os.chmod(env_path, 0o644)
    broken = check_env_permissions(environ)
    assert broken.status == BROKEN
    assert broken.repair_key == "env_perms"
    os.chmod(env_path, 0o600)
    assert check_env_permissions(environ).status == READY


def test_check_database_without_file_is_optional(tmp_path):
    assert check_database(_environ(tmp_path)).status == OPTIONAL


def test_check_database_ready_on_fresh_sqlite(tmp_path):
    import sqlite3

    environ = _environ(tmp_path)
    database = environ["EURUS_HOME"] + "/sessions.sqlite3"
    os.makedirs(os.path.dirname(database), exist_ok=True)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE t (id INTEGER)")
    connection.commit()
    connection.close()
    assert check_database(environ).status == READY


def test_check_provider_without_model_is_missing(tmp_path):
    result = check_provider(_environ(tmp_path), None)
    assert result.status == BROKEN
    assert result.optional is False


def test_check_startup_reports_optional_mechanism(tmp_path):
    result = check_startup(_environ(tmp_path))
    assert result.status in {READY, OPTIONAL}
    assert result.optional is True


def test_run_checks_full_inventory(tmp_path):
    report = run_checks(_environ(tmp_path))
    names = {result.name for result in report.results}
    assert {
        "platform",
        "python",
        "virtualenv",
        "core dependencies",
        "optional features",
        "configuration",
        "storage",
        "env permissions",
        "database",
        "network",
        "provider",
        "browser",
        "mcp",
        "telegram",
        "startup",
    } <= names
    assert report.summary in {"READY", "PARTIAL", "DEGRADED", "BLOCKED"}


def test_run_checks_blocked_without_config(tmp_path):
    report = run_checks(_environ(tmp_path))
    assert report.summary == "BLOCKED"
    by_name = {result.name: result for result in report.results}
    assert by_name["configuration"].status == MISSING


def test_run_checks_full_mode_adds_probes(tmp_path):
    report = run_checks(_environ(tmp_path), full=True)
    names = {result.name for result in report.results}
    assert "api connectivity" in names
    assert "browser runtime" in names
    assert "mcp connectivity" in names


def test_actions_for_orders_and_skips_ready():
    results = [
        CheckResult(name="a", status=READY, repair_key="env_perms"),
        CheckResult(name="b", status=BROKEN, detail="d", repair_key="mcp_reset"),
        CheckResult(name="c", status=MISSING, detail="d", repair_key="config_init"),
    ]
    actions = actions_for(results)
    assert [action.key for action in actions] == ["config_init", "mcp_reset"]
    assert set(REPAIR_ORDER) >= {"config_init", "mcp_reset", "env_perms"}


def test_actions_for_pip_install_requires_targets():
    empty = CheckResult(name="deps", status=MISSING, detail="d", repair_key="pip_install")
    assert actions_for([empty]) == []
    payload = dict(empty.repair_payload)
    payload["targets"] = ["httpx>=0.27"]
    filled = CheckResult(
        name="deps",
        status=MISSING,
        detail="d",
        repair_key="pip_install",
        repair_payload=payload,
    )
    actions = actions_for([filled])
    assert actions[0].description == "pip install httpx>=0.27"


def test_apply_repair_action_unknown_kind_fails():
    ok, message = apply_repair_action(
        RepairAction(key="x", title="t", description="", kind="unknown-kind"), {}
    )
    assert ok is False
    assert "unknown repair kind" in message


def test_apply_repair_action_env_template(tmp_path):
    environ = _environ(tmp_path)
    ok, message = apply_repair_action(
        RepairAction(key="env_template", title="t", description="", kind="env_template"), environ
    )
    assert ok is True
    assert (tmp_path / "eurus-home" / ".env").exists()
    assert stat.S_IMODE((tmp_path / "eurus-home" / ".env").stat().st_mode) == 0o600


def test_apply_repair_action_env_perms_tightens(tmp_path):
    environ = _environ(tmp_path)
    env_path = tmp_path / "eurus-home" / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("KEY=1\n", encoding="utf-8")
    os.chmod(env_path, 0o644)
    ok, message = apply_repair_action(
        RepairAction(key="env_perms", title="t", description="", kind="env_perms"), environ
    )
    assert ok is True
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_apply_repair_action_mcp_reset_backs_up(tmp_path):
    environ = _environ(tmp_path)
    home = tmp_path / "eurus-home"
    home.mkdir(parents=True)
    mcp_path = home / "mcp.json"
    mcp_path.write_text("{invalid json", encoding="utf-8")
    ok, message = apply_repair_action(
        RepairAction(key="mcp_reset", title="t", description="", kind="mcp_reset"), environ
    )
    assert ok is True
    assert json.loads(mcp_path.read_text(encoding="utf-8")) == {"servers": []}
    backups = list((home / "bootstrap" / "backups").rglob("mcp.json"))
    assert len(backups) == 1
    assert "invalid" in backups[0].read_text(encoding="utf-8")


def test_apply_repair_action_config_repair_preserves_backup(tmp_path):
    environ = _environ(tmp_path)
    home = tmp_path / "eurus-home"
    home.mkdir(parents=True)
    config = home / "config.toml"
    config.write_text('agent = { model = "groq/old" }', encoding="utf-8")
    ok, message = apply_repair_action(
        RepairAction(key="config_repair", title="t", description="", kind="config_repair"),
        environ,
    )
    assert ok is True
    assert "agent" in config.read_text(encoding="utf-8")
    backups = list((home / "bootstrap" / "backups").rglob("config.toml"))
    assert len(backups) == 1
    assert "groq/old" in backups[0].read_text(encoding="utf-8")


def test_execute_repair_captures_failures():
    actions = [RepairAction(key="x", title="t", description="", kind="bogus")]
    results = execute_repair(actions, {})
    assert results == [("x", False, "unknown repair kind: bogus")]


def test_report_text_contains_summary_and_rows(tmp_path):
    report = run_checks(_environ(tmp_path))
    text = report_text(report)
    assert text.startswith("summary: BLOCKED")
    assert "platform" in text


def test_report_json_is_parseable(tmp_path):
    report = run_checks(_environ(tmp_path))
    data = json.loads(report_json(report))
    assert data["summary"] == "BLOCKED"
    assert isinstance(data["checks"], list)


def test_render_report_prints_without_crash(tmp_path):
    from io import StringIO

    from rich.console import Console

    buffer = StringIO()
    console = Console(file=buffer, emoji=False, width=200, force_terminal=False)
    report = run_checks(_environ(tmp_path))
    render_report(report, console)
    output = buffer.getvalue()
    assert "eurus doctor" in output
    assert "platform" in output


def test_repair_cycle_restores_ready(tmp_path):
    environ = _environ(tmp_path)
    report = run_checks(environ)
    assert report.summary == "BLOCKED"
    actions = actions_for(report.results)
    assert actions, "expected at least config_init repair"
    results = execute_repair(actions, environ)
    assert all(ok for _, ok, _ in results), results
    after = run_checks(environ)
    assert after.summary != "BLOCKED"
