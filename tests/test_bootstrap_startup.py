import stat
import sys
from pathlib import Path

import pytest

import eurus.bootstrap.startup.proot as proot_module
import eurus.bootstrap.startup.systemd as systemd_module
from eurus.bootstrap.models import UNKNOWN, BootstrapError
from eurus.bootstrap.startup.manager import (
    SERVICE_NAMES,
    ServiceStatus,
    detect_mechanisms,
    install_cron,
    install_launchers,
    install_systemd,
    install_termux_boot,
    recommend_backend,
    service_command,
    status_all,
    uninstall_all,
    validate_service,
)
from eurus.bootstrap.startup.proot import (
    CRON_MARKER_BEGIN,
    CRON_MARKER_END,
    build_cron_block,
    cron_entry,
    shell_profile_path,
    upsert_cron_block,
)
from eurus.bootstrap.startup.systemd import (
    render_unit,
    systemd_available,
    unit_path,
    write_unit,
)
from eurus.bootstrap.startup.termux import render_boot_script, render_launcher


def _environ(tmp_path, **extra):
    env = {"EURUS_HOME": str(tmp_path / "eurus-home"), "HOME": str(tmp_path / "home")}
    env.update(extra)
    return env


def test_service_names_and_validation():
    assert "telegram-gateway" in SERVICE_NAMES
    assert "scheduler" in SERVICE_NAMES
    validate_service("telegram-gateway")
    validate_service("scheduler")
    with pytest.raises(BootstrapError):
        validate_service("not-a-service")


def test_service_command_uses_active_interpreter():
    assert service_command("telegram-gateway") == [
        sys.executable,
        "-m",
        "eurus",
        "gateway",
        "telegram",
    ]
    assert service_command("scheduler") == [sys.executable, "-m", "eurus", "schedule", "daemon"]
    with pytest.raises(BootstrapError):
        service_command("nope")


def test_detect_mechanisms_on_this_host(tmp_path):
    mechanisms = detect_mechanisms(_environ(tmp_path))
    assert isinstance(mechanisms.systemd, bool)
    assert isinstance(mechanisms.cron, bool)
    assert mechanisms.termux_boot in {"yes", "no", UNKNOWN}
    assert mechanisms.recommended in {"systemd", "cron", "manual", "termux-boot", "termux-manual"}
    assert mechanisms.to_dict()["recommended"] == mechanisms.recommended


def test_recommend_backend_matrix():
    from eurus.bootstrap.startup.manager import StartupMechanisms

    assert recommend_backend("termux", StartupMechanisms(termux_boot="yes")) == "termux-boot"
    assert recommend_backend("termux", StartupMechanisms(termux_boot="unknown")) == "termux-manual"
    assert recommend_backend("linux", StartupMechanisms(systemd_user_manager=True)) == "systemd"
    assert recommend_backend("proot", StartupMechanisms(cron=True)) == "cron"
    assert recommend_backend("proot", StartupMechanisms()) == "manual"


def test_install_termux_boot_writes_boot_script(tmp_path):
    environ = _environ(tmp_path)
    path = install_termux_boot(environ, ["scheduler"])
    assert path == Path(environ["HOME"]) / ".termux" / "boot" / "eurus-bootstrap.sh"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!")
    assert "termux-wake-lock" in text
    assert "schedule daemon" in text
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & 0o111
    with pytest.raises(BootstrapError):
        install_termux_boot(environ, [])


def test_install_launchers_writes_into_eurus_services(tmp_path):
    environ = _environ(tmp_path)
    created = install_launchers(environ, ["scheduler"])
    assert len(created) == 1
    launcher = created[0]
    assert launcher == Path(environ["EURUS_HOME"]) / "services" / "start-scheduler.sh"
    assert launcher.exists()
    assert stat.S_IMODE(launcher.stat().st_mode) & 0o111
    text = launcher.read_text(encoding="utf-8")
    assert "schedule daemon" in text
    assert "mkdir -p" in text
    assert f"export HOME={environ['HOME']}" in text
    assert f"export EURUS_HOME={environ['EURUS_HOME']}" in text
    assert "EURUS_NO_DOTENV" not in text


def test_render_boot_script_staggers_services(tmp_path):
    script = render_boot_script(
        prefix="/data/data/com.termux/files/usr",
        home=tmp_path,
        python_exe="/usr/bin/python",
        enabled=["telegram-gateway", "scheduler"],
    )
    assert script.startswith("#!/data/data/com.termux/files/usr/bin/sh")
    assert "sleep 3" in script
    assert "gateway telegram" in script
    assert "schedule daemon" in script
    assert "logs/telegram-gateway.log" in script


def test_render_launcher_redirects_to_log(tmp_path):
    text = render_launcher(python_exe="/usr/bin/python", home=tmp_path, service="scheduler")
    assert text.startswith("#!/bin/sh")
    assert "mkdir -p" in text
    assert "logs/scheduler.log" in text


def test_cron_entry_and_block():
    entry = cron_entry("/home/u/.eurus/services/start-scheduler.sh")
    assert entry == "@reboot /home/u/.eurus/services/start-scheduler.sh"
    block = build_cron_block([entry])
    lines = block.splitlines()
    assert lines[0] == CRON_MARKER_BEGIN
    assert lines[-1] == CRON_MARKER_END
    assert entry in lines


def test_upsert_cron_block_idempotent(monkeypatch):
    current = {"text": "0 9 * * * /usr/bin/other-job\n"}
    monkeypatch.setattr(proot_module, "crontab_available", lambda: True)
    monkeypatch.setattr(proot_module, "read_crontab", lambda: current["text"])
    writes = []

    def fake_write(content):
        writes.append(content)
        current["text"] = content
        return True

    monkeypatch.setattr(proot_module, "write_crontab", fake_write)
    entry = cron_entry("/home/u/.eurus/services/start-scheduler.sh")
    ok, message = upsert_cron_block([entry])
    assert ok is True
    assert CRON_MARKER_BEGIN in current["text"]
    assert "other-job" in current["text"]
    assert current["text"].count("@reboot") == 1
    ok2, message2 = upsert_cron_block([entry])
    assert ok2 is True
    assert current["text"].count("@reboot") == 1


def test_upsert_cron_block_replaces_stale_entries(monkeypatch):
    entry = cron_entry("/home/u/.eurus/services/start-scheduler.sh")
    stale = cron_entry("/old/path/start-scheduler.sh")
    current = {"text": build_cron_block([stale])}
    monkeypatch.setattr(proot_module, "crontab_available", lambda: True)
    monkeypatch.setattr(proot_module, "read_crontab", lambda: current["text"])
    monkeypatch.setattr(
        proot_module,
        "write_crontab",
        lambda content: current.__setitem__("text", content) or True,
    )
    upsert_cron_block([entry])
    assert "/old/path" not in current["text"]
    assert "/home/u/.eurus/services" in current["text"]


def test_upsert_cron_block_without_crontab(monkeypatch):
    monkeypatch.setattr(proot_module, "crontab_available", lambda: False)
    ok, message = upsert_cron_block([cron_entry("/tmp/x.sh")])
    assert ok is False
    assert "not available" in message


def test_install_cron_uses_proot_helpers(monkeypatch, tmp_path):
    current = {"text": ""}
    monkeypatch.setattr(proot_module, "crontab_available", lambda: True)
    monkeypatch.setattr(proot_module, "read_crontab", lambda: current["text"])
    monkeypatch.setattr(
        proot_module,
        "write_crontab",
        lambda content: current.__setitem__("text", content) or True,
    )
    install_launchers(_environ(tmp_path), ["scheduler"])
    eurus_home = Path(tmp_path) / "eurus-home" / ".eurus"
    ok, message = install_cron(["scheduler"], eurus_home=eurus_home)
    assert ok is True
    entry_line = [line for line in current["text"].splitlines() if line.startswith("@reboot")]
    assert len(entry_line) == 1
    assert str(eurus_home / "services" / "start-scheduler.sh") in entry_line[0]
    assert current["text"].count(CRON_MARKER_END) == 1


def test_install_cron_requires_crontab(monkeypatch):
    monkeypatch.setattr(proot_module, "crontab_available", lambda: False)
    ok, message = install_cron(["scheduler"])
    assert ok is False
    assert "not available" in message


def test_shell_profile_path_prefers_termux(tmp_path):
    assert (
        shell_profile_path(
            {"HOME": str(tmp_path), "PREFIX": "/data/data/com.termux/files/usr"}
        ).name
        == ".profile"
    )
    assert shell_profile_path({"HOME": str(tmp_path)}).name in {".profile", ".zshrc", ".bashrc"}


def test_systemd_render_unit_contents():
    unit = render_unit(
        service="scheduler",
        description="Eurus Scheduler Daemon",
        command=["python", "-m", "eurus", "schedule", "daemon"],
        workdir="/home/u/.eurus",
        env_lines=["EURUS_LOG_LEVEL=info"],
    )
    assert "[Unit]" in unit
    assert "Description=Eurus Scheduler Daemon" in unit
    assert "ExecStart=python -m eurus schedule daemon" in unit
    assert "WorkingDirectory=/home/u/.eurus" in unit
    assert "Environment=EURUS_LOG_LEVEL=info" in unit
    assert "WantedBy=default.target" in unit
    assert "Restart=on-failure" in unit


def test_systemd_write_unit_to_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(systemd_module, "SYSTEMD_USER_DIR", tmp_path / "systemd" / "user")
    path = write_unit("scheduler", "unit-text")
    assert path.read_text(encoding="utf-8") == "unit-text"
    assert unit_path("scheduler") == path


def test_systemd_available_false_without_run_dir():
    if Path("/run/systemd/system").exists():
        pytest.skip("systemd run dir exists in this environment")
    assert systemd_available() is False


def test_uninstall_all_cleans_created_files(tmp_path):
    environ = _environ(tmp_path)
    install_launchers(environ, SERVICE_NAMES)
    removed = uninstall_all(environ)
    assert len(removed) == len(SERVICE_NAMES)
    for service in SERVICE_NAMES:
        launcher = Path(environ["EURUS_HOME"]) / "services" / f"start-{service}.sh"
        assert not launcher.exists()
    assert all("start-" in item for item in removed)


def test_status_all_reports_both_services(tmp_path):
    environ = _environ(tmp_path)
    statuses = status_all(environ)
    assert {item.service for item in statuses} == set(SERVICE_NAMES)
    assert all(isinstance(item, ServiceStatus) for item in statuses)
    assert all(not item.installed for item in statuses)
    install_launchers(environ, ["scheduler"])
    after = {item.service: item for item in status_all(environ)}
    assert after["scheduler"].installed is True
    assert after["scheduler"].mechanism == "script"
    assert after["scheduler"].state == "manual"
    assert after["telegram-gateway"].installed is False
    assert after["telegram-gateway"].to_dict()["service"] == "telegram-gateway"


def test_install_systemd_writes_unit_without_daemon(monkeypatch, tmp_path):
    monkeypatch.setattr(systemd_module, "SYSTEMD_USER_DIR", tmp_path / "systemd" / "user")
    monkeypatch.setattr(systemd_module, "daemon_reload", lambda: True)
    path = install_systemd("scheduler", workdir=str(tmp_path), enable=False, start=False)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "ExecStart=" in text
    assert "schedule daemon" in text
