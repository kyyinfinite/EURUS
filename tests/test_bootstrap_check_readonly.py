import json
from pathlib import Path

from click.testing import CliRunner

from eurus.bootstrap.orchestrator import BootstrapOrchestrator
from eurus.bootstrap.wizard import BootstrapIO
from eurus.cli.base import main


def _snapshot(root: Path) -> list[str]:
    return sorted(str(item.relative_to(root)) for item in root.rglob("*"))


def _io() -> BootstrapIO:
    from io import StringIO

    from rich.console import Console

    console = Console(file=StringIO(), emoji=False, width=200, force_terminal=False)
    return BootstrapIO(console=console, non_interactive=True)


def test_orchestrator_check_never_touches_filesystem(tmp_path):
    environ = {"EURUS_HOME": str(tmp_path / "missing-home"), "HOME": str(tmp_path)}
    before = _snapshot(tmp_path)
    orchestrator = BootstrapOrchestrator(_io(), environ)
    report = orchestrator.run_check(render=False)
    assert _snapshot(tmp_path) == before
    assert not (tmp_path / "missing-home").exists()
    assert report.results


def test_orchestrator_check_existing_home_is_untouched(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    marker = home / "keep-me.txt"
    marker.write_text("data", encoding="utf-8")
    environ = {"EURUS_HOME": str(home), "HOME": str(tmp_path)}
    before = _snapshot(tmp_path)
    modes_before = {str(item): item.stat().st_mode for item in tmp_path.rglob("*")}
    BootstrapOrchestrator(_io(), environ).run_check(render=False)
    assert _snapshot(tmp_path) == before
    assert marker.read_text(encoding="utf-8") == "data"
    modes_after = {str(item): item.stat().st_mode for item in tmp_path.rglob("*")}
    assert modes_before == modes_after
    assert not (home / "bootstrap").exists()


def test_cli_bootstrap_check_missing_home_json(tmp_path):
    runner = CliRunner(env={"EURUS_HOME": str(tmp_path / "home"), "HOME": str(tmp_path)})
    result = runner.invoke(main, ["bootstrap", "--check", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["summary"] == "BLOCKED"
    storage = next(item for item in data["checks"] if item["name"] == "storage")
    assert storage["status"] in {"MISSING", "BROKEN"}
    assert not (tmp_path / "home").exists()


def test_cli_bootstrap_check_repeated_is_idempotent(tmp_path):
    runner = CliRunner(env={"EURUS_HOME": str(tmp_path / "home"), "HOME": str(tmp_path)})
    first = runner.invoke(main, ["bootstrap", "check", "--text"])
    second = runner.invoke(main, ["bootstrap", "check", "--text"])
    assert first.exit_code == second.exit_code
    assert not (tmp_path / "home").exists()


def test_doctor_still_records_state(tmp_path):
    runner = CliRunner(env={"EURUS_HOME": str(tmp_path / "home"), "HOME": str(tmp_path)})
    result = runner.invoke(main, ["doctor", "--text"])
    assert result.exit_code == 1
    state = json.loads((tmp_path / "home" / "bootstrap" / "state.json").read_text())
    assert state["doctor"]["summary"] == "BLOCKED"


def test_quick_setup_still_persists_state(tmp_path):
    runner = CliRunner(env={"EURUS_HOME": str(tmp_path / "home"), "HOME": str(tmp_path)})
    result = runner.invoke(main, ["bootstrap", "--non-interactive", "--quick"])
    assert result.exit_code == 0
    state = json.loads((tmp_path / "home" / "bootstrap" / "state.json").read_text())
    assert state["last_quick_setup"]["status"] == "done"
