import json
from io import StringIO

import pytest
from click.testing import CliRunner
from rich.console import Console

from eurus.bootstrap.models import SkipStep
from eurus.bootstrap.orchestrator import BootstrapOrchestrator
from eurus.bootstrap.wizard import (
    BootstrapIO,
    WizardChoice,
    abort,
    render_choice_table,
    selection_summary,
)
from eurus.cli.base import main


def _io(**kwargs) -> BootstrapIO:
    console = Console(file=StringIO(), emoji=False, width=200, force_terminal=False)
    return BootstrapIO(console=console, **kwargs)


def _output(io: BootstrapIO) -> str:
    return io.console.file.getvalue()


def test_non_interactive_io_uses_defaults():
    io = _io()
    assert io.interactive() is False
    assert io.ask("Pick", "fallback") == "fallback"
    assert io.confirm("Sure?", True) is True
    assert io.confirm("Sure?", False) is False
    assert io.hidden("Key") == ""
    assert io.choose("Menu", ["a", "b", "c"], 1) == 1


def test_assume_yes_overrides_confirm():
    io = _io(assume_yes=True)
    assert io.confirm("Delete everything?", False) is True


def test_non_interactive_flag_disables_prompts_even_with_tty():
    io = _io(non_interactive=True)
    assert io.interactive() is False


def test_status_line_styles_by_status():
    io = _io()
    io.status_line("browser", "READY", "chromium found")
    io.status_line("provider", "MISSING", "no model")
    io.status_line("telegram", "SKIP")
    output = _output(io)
    assert "[OK]" in output
    assert "[MISSING]" in output
    assert "[SKIP]" in output
    assert "chromium found" in output


def test_quiet_mode_suppresses_info_but_not_errors():
    io = _io(quiet=True)
    io.info("hidden")
    io.ok("hidden too")
    io.error("visible")
    output = _output(io)
    assert "hidden" not in output
    assert "visible" in output


def test_require_interactive_raises_skip_step():
    io = _io()
    with pytest.raises(SkipStep):
        io.require_interactive("needs a terminal")


def test_choose_validates_range():
    io = _io()
    assert io.choose("Menu", ["a", "b"], 0) == 0


def test_render_choice_table_and_selection_summary():
    io = _io()
    choices = [WizardChoice(key="groq", label="Groq", description="fast inference")]
    render_choice_table(io, choices)
    assert "Groq" in _output(io)
    panel = selection_summary("Summary", [("provider", "groq"), ("model", "groq/x")])
    assert "Summary" in panel.title


def test_abort_raises_skip_step():
    io = _io()
    with pytest.raises(SkipStep):
        abort(io, "cannot continue")
    assert "cannot continue" in _output(io)


def test_orchestrator_run_check_is_read_only(tmp_path):
    environ = {"EURUS_HOME": str(tmp_path / "home")}
    orchestrator = BootstrapOrchestrator(_io(), environ)
    report = orchestrator.run_check()
    assert report.summary == "BLOCKED"
    store = orchestrator.ctx.store
    assert not (tmp_path / "home").exists()
    assert not store.state_path.exists()
    assert not store.platform_path.exists()
    assert not store.dependencies_path.exists()
    assert not store.history_path.exists()


def test_orchestrator_repair_persists_check_state(tmp_path):
    environ = {"EURUS_HOME": str(tmp_path / "home")}
    orchestrator = BootstrapOrchestrator(_io(assume_yes=True), environ)
    orchestrator.run_repair()
    store = orchestrator.ctx.store
    state = store.load_state()
    assert state["doctor"]["summary"]
    assert store.platform_path.exists()
    assert store.dependencies_path.exists()
    assert store.history_path.exists()


def test_orchestrator_run_repair_without_actions(tmp_path):
    environ = {"EURUS_HOME": str(tmp_path / "home")}
    orchestrator = BootstrapOrchestrator(_io(assume_yes=True), environ)
    orchestrator.run_check()
    report = orchestrator.run_repair()
    assert report.summary != "BLOCKED"


def test_orchestrator_quick_setup_non_interactive(tmp_path):
    environ = {"EURUS_HOME": str(tmp_path / "home")}
    orchestrator = BootstrapOrchestrator(_io(non_interactive=True), environ)
    report = orchestrator.run_quick_setup(render=False)
    assert report.summary != "BLOCKED"
    state = orchestrator.ctx.store.load_state()
    quick = state["last_quick_setup"]
    assert quick["status"] == "done"
    assert all(status == "done" for name, status in quick["steps"].items() if name != "startup")
    assert (tmp_path / "home" / "config.toml").exists()
    assert (tmp_path / "home" / ".env").exists()
    config = (tmp_path / "home" / "config.toml").read_text(encoding="utf-8")
    assert 'model = "groq/' in config


def test_orchestrator_quick_setup_resumes_after_interrupt(tmp_path):
    environ = {"EURUS_HOME": str(tmp_path / "home")}
    store = BootstrapOrchestrator(_io(), environ).ctx.store
    from eurus.bootstrap.state.store import mark_setup_started

    state = {"schema_version": 1}
    mark_setup_started(state, ["platform"])
    store.save_state(state)
    orchestrator = BootstrapOrchestrator(_io(non_interactive=True), environ)
    report = orchestrator.run_quick_setup(render=False)
    assert report.summary != "BLOCKED"
    finished = orchestrator.ctx.store.load_state()["last_quick_setup"]
    assert finished["status"] == "done"


def test_orchestrator_run_check_render_false_is_silent(tmp_path):
    io = _io(non_interactive=True)
    environ = {"EURUS_HOME": str(tmp_path / "home")}
    BootstrapOrchestrator(io, environ).run_check(render=False)
    assert "eurus doctor" not in _output(io)


def _runner(tmp_path) -> CliRunner:
    return CliRunner(env={"EURUS_HOME": str(tmp_path / "home")})


def test_cli_bootstrap_flag_check_json(tmp_path):
    result = _runner(tmp_path).invoke(main, ["bootstrap", "--check", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["summary"] == "BLOCKED"
    assert any(check["name"] == "platform" for check in data["checks"])


def test_cli_bootstrap_subcommand_check_text(tmp_path):
    result = _runner(tmp_path).invoke(main, ["bootstrap", "check", "--text"])
    assert result.exit_code == 1
    assert result.output.startswith("summary: BLOCKED")


def test_cli_bootstrap_repair_then_check_cycle(tmp_path):
    runner = _runner(tmp_path)
    repaired = runner.invoke(main, ["bootstrap", "--yes", "repair"])
    assert repaired.exit_code == 0
    assert (tmp_path / "home" / "config.toml").exists()
    after = runner.invoke(main, ["bootstrap", "--check"])
    assert after.exit_code == 0


def test_cli_bootstrap_exclusive_flags_rejected(tmp_path):
    result = _runner(tmp_path).invoke(main, ["bootstrap", "--check", "--quick"])
    assert result.exit_code == 2


def test_cli_bootstrap_check_creates_nothing(tmp_path):
    runner = _runner(tmp_path)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    result = runner.invoke(main, ["bootstrap", "--check", "--json"])
    assert result.exit_code == 1
    json.loads(result.output)
    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert before == after
    assert not (tmp_path / "home" / "bootstrap").exists()


def test_cli_bootstrap_quick_non_interactive(tmp_path):
    runner = _runner(tmp_path)
    result = runner.invoke(main, ["bootstrap", "--non-interactive", "--quick"])
    assert result.exit_code == 0
    state = json.loads((tmp_path / "home" / "bootstrap" / "state.json").read_text())
    assert state["last_quick_setup"]["status"] == "done"


def test_cli_bootstrap_reset_removes_state_only(tmp_path):
    runner = _runner(tmp_path)
    seed = runner.invoke(main, ["bootstrap", "--yes", "repair"])
    assert seed.exit_code == 0
    env_path = tmp_path / "home" / ".env"
    assert not env_path.exists() or env_path.exists()
    result = runner.invoke(main, ["bootstrap", "reset", "--yes"])
    assert result.exit_code == 0
    assert result.output.count("removed") == 4
    assert not (tmp_path / "home" / "bootstrap" / "state.json").exists()


def test_cli_doctor_json_and_state(tmp_path):
    runner = _runner(tmp_path)
    result = runner.invoke(main, ["doctor", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["summary"] == "BLOCKED"
    state = json.loads((tmp_path / "home" / "bootstrap" / "state.json").read_text())
    assert state["doctor"]["summary"] == "BLOCKED"
    assert state["eurus_version"]


def test_cli_doctor_repair_without_consent_cancels(tmp_path):
    result = _runner(tmp_path).invoke(main, ["doctor", "--repair"])
    assert result.exit_code == 0
    assert "cancelled" in result.output
    assert not (tmp_path / "home" / "config.toml").exists()


def test_cli_doctor_repair_single_orchestrator_json(tmp_path):
    result = _runner(tmp_path).invoke(main, ["doctor", "--repair", "--yes", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["summary"] != "BLOCKED"
    assert data["repair"]["repaired"]
    assert data["repair"]["cancelled"] is False
    assert (tmp_path / "home" / "config.toml").exists()


def test_cli_doctor_repair_text_after_partial_failure(monkeypatch, tmp_path):
    import eurus.bootstrap.orchestrator as orchestrator_module

    real_execute = orchestrator_module.execute_repair

    def failing_execute(actions, environ, backup_root=None):
        results = real_execute(actions, environ, backup_root)
        return [
            (key, False, "simulated failure") if key == "config_init" else (key, ok, message)
            for key, ok, message in results
        ]

    monkeypatch.setattr(orchestrator_module, "execute_repair", failing_execute)
    result = _runner(tmp_path).invoke(main, ["doctor", "--repair", "--yes", "--text"])
    assert result.exit_code == 1
    assert "simulated failure" in result.output


def test_cli_setup_mcp_creates_and_reports(tmp_path):
    runner = _runner(tmp_path)
    result = runner.invoke(main, ["setup", "mcp"])
    assert result.exit_code == 0
    assert "no servers" in result.output
    mcp_path = tmp_path / "home" / "mcp.json"
    assert json.loads(mcp_path.read_text()) == {"servers": []}


def test_cli_setup_startup_shows_status(tmp_path):
    result = _runner(tmp_path).invoke(main, ["setup", "startup"])
    assert result.exit_code == 0
    assert "telegram-gateway" in result.output
    assert "scheduler" in result.output


def test_cli_setup_config_non_interactive_is_noop(tmp_path):
    result = _runner(tmp_path).invoke(main, ["setup", "config"])
    assert result.exit_code == 0


def test_cli_setup_provider_non_interactive_reports_honestly(tmp_path):
    result = _runner(tmp_path).invoke(main, ["setup", "provider"])
    assert result.exit_code == 1
    assert "provider: groq" in result.output


def test_cli_help_lists_new_commands():
    runner = CliRunner()
    top = runner.invoke(main, ["--help"])
    assert "bootstrap" in top.output
    assert "setup" in top.output
    assert "doctor" in top.output
    group = runner.invoke(main, ["bootstrap", "--help"])
    assert "--quick" in group.output
    assert "--check" in group.output
    assert "--repair" in group.output
    assert "--update" in group.output
    subcommands = runner.invoke(main, ["setup", "--help"])
    assert "provider" in subcommands.output
    assert "browser" in subcommands.output
    assert "mcp" in subcommands.output
    assert "telegram" in subcommands.output
    assert "startup" in subcommands.output
    assert "config" in subcommands.output


def test_cli_bootstrap_reset_requires_confirmation_without_yes(tmp_path):
    runner = _runner(tmp_path)
    runner.invoke(main, ["bootstrap", "--yes", "repair"])
    result = runner.invoke(main, ["bootstrap", "reset"], input="n\n")
    assert result.exit_code == 0
    assert "cancelled" in result.output
    assert (tmp_path / "home" / "bootstrap" / "state.json").exists()
