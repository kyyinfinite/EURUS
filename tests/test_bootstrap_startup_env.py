import stat
import subprocess
from pathlib import Path

from eurus.bootstrap.startup.manager import (
    install_cron,
    install_launchers,
    install_termux_boot,
    service_env_lines,
)
from eurus.bootstrap.startup.proot import cron_entry, shell_profile_path
from eurus.bootstrap.startup.systemd import quote_unit_argument, quote_unit_environment, render_unit
from eurus.bootstrap.startup.termux import render_boot_script, render_launcher


def _environ(tmp_path, **extra):
    env = {
        "EURUS_HOME": str(tmp_path / "data"),
        "HOME": str(tmp_path / "home"),
    }
    env.update(extra)
    return env


def test_boot_script_preserves_home_and_sets_eurus_home(tmp_path):
    real_home = tmp_path / "home"
    data_home = tmp_path / "data"
    script = render_boot_script(
        prefix="/data/data/com.termux/files/usr",
        home=real_home,
        python_exe="/data/data/com.termux/files/usr/bin/python",
        enabled=["telegram-gateway"],
        eurus_home=data_home,
    )
    assert f"export HOME={real_home}" in script
    assert f"export EURUS_HOME={data_home}" in script
    assert "EURUS_NO_DOTENV" not in script
    assert str(data_home / "bootstrap" / "logs" / "telegram-gateway.log") in script


def test_boot_script_survives_paths_with_spaces(tmp_path):
    real_home = tmp_path / "my home dir"
    data_home = tmp_path / "eurus data"
    script = render_boot_script(
        prefix="/data/data/com.termux/files/usr",
        home=real_home,
        python_exe="/data/data/com.termux/files/usr/bin/python",
        enabled=["scheduler"],
        eurus_home=data_home,
    )
    assert f"export HOME='{real_home}'" in script
    assert f"export EURUS_HOME='{data_home}'" in script
    parsed = subprocess.run(["sh", "-n"], input=script, text=True, capture_output=True, check=False)
    assert parsed.returncode == 0, parsed.stderr


def test_boot_script_is_valid_shell_and_dry_runs(tmp_path):
    real_home = tmp_path / "home"
    data_home = tmp_path / "data"
    script = render_boot_script(
        prefix="/tmp",
        home=real_home,
        python_exe="/bin/true",
        enabled=["telegram-gateway", "scheduler"],
        eurus_home=data_home,
    )
    parsed = subprocess.run(["sh", "-n"], input=script, text=True, capture_output=True, check=False)
    assert parsed.returncode == 0, parsed.stderr
    assert script.count("-m eurus") == 2


def test_launcher_exports_environment_and_quotes(tmp_path):
    real_home = Path("/home/some user")
    data_home = Path("/home/some user/eurus data")
    text = render_launcher(
        python_exe="/usr/bin/python3", home=real_home, service="scheduler", eurus_home=data_home
    )
    assert f"export HOME='{real_home}'" in text
    assert f"export EURUS_HOME='{data_home}'" in text
    assert "/usr/bin/python3" in text
    assert "EURUS_NO_DOTENV" not in text
    parsed = subprocess.run(["sh", "-n"], input=text, text=True, capture_output=True, check=False)
    assert parsed.returncode == 0, parsed.stderr


def test_install_termux_boot_uses_real_home_for_boot_dir(tmp_path):
    environ = _environ(tmp_path)
    path = install_termux_boot(environ, ["scheduler"])
    assert path == Path(environ["HOME"]) / ".termux" / "boot" / "eurus-bootstrap.sh"
    text = path.read_text(encoding="utf-8")
    assert f"export HOME={environ['HOME']}" in text
    assert f"export EURUS_HOME={environ['EURUS_HOME']}" in text


def test_install_launchers_and_cron_agree_on_path(tmp_path, monkeypatch):
    import eurus.bootstrap.startup.proot as proot_module

    current = {"text": ""}
    monkeypatch.setattr(proot_module, "crontab_available", lambda: True)
    monkeypatch.setattr(proot_module, "read_crontab", lambda: current["text"])
    monkeypatch.setattr(
        proot_module,
        "write_crontab",
        lambda content: current.__setitem__("text", content) or True,
    )
    environ = _environ(tmp_path)
    launchers = install_launchers(environ, ["scheduler"])
    ok, message = install_cron(["scheduler"], environ=environ)
    assert ok is True
    launcher = launchers[0]
    expected = f"@reboot {launcher}"
    assert expected in current["text"]
    assert launcher.exists()


def test_cron_entry_quotes_paths_with_spaces():
    entry = cron_entry("/home/a b/.eurus/services/start-scheduler.sh")
    assert entry == "@reboot '/home/a b/.eurus/services/start-scheduler.sh'"


def test_shell_profile_path_uses_given_environ(tmp_path):
    environ = {"HOME": str(tmp_path / "someone"), "SHELL": "/bin/zsh"}
    path = shell_profile_path(environ)
    assert path == tmp_path / "someone" / ".zshrc"


def test_service_env_lines_point_at_data_home(tmp_path):
    environ = _environ(tmp_path)
    assert service_env_lines(environ) == [f"EURUS_HOME={tmp_path / 'data'}"]


def test_systemd_unit_escapes_arguments():
    unit = render_unit(
        service="scheduler",
        description="desc",
        command=["/opt/my tools/python", "-m", "eurus", "schedule", "daemon"],
        workdir="/home/u/eurus data",
        env_lines=["EURUS_HOME=/home/u/eurus data", "EURUS_LOG_LEVEL=info"],
    )
    assert 'ExecStart="/opt/my tools/python" -m eurus schedule daemon' in unit
    assert 'WorkingDirectory="/home/u/eurus data"' in unit
    assert 'Environment=EURUS_HOME="/home/u/eurus data"' in unit
    assert "Environment=EURUS_LOG_LEVEL=info" in unit


def test_systemd_unit_escapes_dollar_and_percent():
    assert quote_unit_argument("$HOME/bin") == '"$$HOME/bin"'
    assert quote_unit_argument("50%") == '"50%%"'
    assert quote_unit_argument('say "hi"') == '"say \\"hi\\""'
    assert quote_unit_environment("TOKEN=a$b") == 'TOKEN="a$$b"'
    assert quote_unit_argument("/plain/path") == "/plain/path"


def test_systemd_unit_uses_xdg_config_home(monkeypatch, tmp_path):
    import eurus.bootstrap.startup.systemd as systemd_module

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert systemd_module._default_user_dir() == tmp_path / "xdg" / "systemd" / "user"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert systemd_module._default_user_dir() == (
        tmp_path / "home" / ".config" / "systemd" / "user"
    )


def test_generated_launcher_is_executable(tmp_path):
    environ = _environ(tmp_path)
    created = install_launchers(environ, ["scheduler"])
    mode = stat.S_IMODE(created[0].stat().st_mode)
    assert mode & 0o111
    body = created[0].read_text(encoding="utf-8")
    assert "mkdir -p" in body
    assert str(tmp_path / "data" / "bootstrap" / "logs") in body
