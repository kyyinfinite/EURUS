from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eurus.automation.executor import AgentJobExecutor, ExecutionContext
from eurus.config import build_config
from eurus.runtime.budgets import BudgetSpec
from eurus.scheduler import ScheduleStore
from eurus.storage.database import Database
from eurus.storage.jobs import Job, JobStore


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EURUS_HOME", str(tmp_path))
    return tmp_path


def test_schedule_hermes_fields_round_trip(home: Path) -> None:
    store = ScheduleStore(home / "data" / "eurus.sqlite3")
    schedule = store.create(
        "eurus",
        "summarize the inbox",
        kind="interval",
        interval_seconds=3600,
        name="inbox-digest",
        deliver="origin",
        origin_gateway="telegram",
        origin_conversation="424242",
        skills=["antislop"],
        pre_script="date -I",
        silent=True,
    )
    loaded = store.get(schedule.schedule_id)
    assert loaded is not None
    assert loaded.name == "inbox-digest"
    assert loaded.deliver == "origin"
    assert loaded.origin_gateway == "telegram"
    assert loaded.origin_conversation == "424242"
    assert loaded.skills == ("antislop",)
    assert loaded.pre_script == "date -I"
    assert loaded.silent is True
    snapshot = json.loads(store.revisions(schedule.schedule_id)[0].definition_json)
    assert snapshot["deliver"] == "origin"
    assert snapshot["skills"] == ["antislop"]


def test_schedule_update_hermes_fields(home: Path) -> None:
    store = ScheduleStore(home / "data" / "eurus.sqlite3")
    schedule = store.create("eurus", "weekly report", kind="interval", interval_seconds=3600)
    updated = store.update(
        schedule.schedule_id,
        "eurus",
        name="report",
        deliver="telegram:100200300",
        skills=["a", "b", "a"],
        pre_script="echo hi",
        silent=False,
    )
    assert updated is not None
    assert updated.deliver == "telegram:100200300"
    assert updated.skills == ("a", "b")
    assert updated.pre_script == "echo hi"


def test_schedule_rejects_invalid_deliver(home: Path) -> None:
    store = ScheduleStore(home / "data" / "eurus.sqlite3")
    with pytest.raises(ValueError):
        store.create("eurus", "x", kind="once", run_at=_future(), deliver="carrier-pigeon")
    with pytest.raises(ValueError):
        store.create("eurus", "x", kind="once", run_at=_future(), deliver="telegram:notanumber")


def test_dispatched_job_carries_hermes_payload_and_recursion_guard(home: Path) -> None:
    from eurus.scheduler import SCHEDULED_BLOCKED_TOOLS

    store = ScheduleStore(home / "data" / "eurus.sqlite3")
    jobs = JobStore(Database(home / "data" / "eurus.sqlite3"))
    schedule = store.create(
        "eurus",
        "check status page",
        kind="interval",
        interval_seconds=3600,
        name="status-check",
        deliver="origin",
        origin_gateway="web",
        origin_conversation="session-9",
        skills=["antislop"],
        pre_script="uptime",
        silent=True,
    )
    dispatched = store.dispatch_due(now=datetime.now(UTC) + timedelta(hours=2))
    assert dispatched
    job = jobs.list_jobs(owner_id="eurus", limit=1)[0]
    assert job.correlation_id == schedule.schedule_id
    assert job.payload["deliver"] == "origin"
    assert job.payload["origin_gateway"] == "web"
    assert job.payload["origin_conversation"] == "session-9"
    assert job.payload["schedule_name"] == "status-check"
    assert job.payload["skills"] == ["antislop"]
    assert job.payload["pre_script"] == "uptime"
    assert job.payload["silent"] is True
    assert job.payload["run_mode"] == "scheduled"
    for tool in SCHEDULED_BLOCKED_TOOLS:
        assert tool in job.payload["blocked_tools"]


def test_verbatim_job_skips_the_model(home: Path, tmp_path: Path) -> None:
    config = build_config({"agent": {"model": ""}, "permissions": {"workspace": str(tmp_path)}})
    executor = AgentJobExecutor(config)
    job = JobStore(Database(home / "data" / "eurus.sqlite3")).enqueue(
        "eurus",
        "⏰ EURUS REMINDER\n\nstand up",
        payload={"verbatim": True, "deliver": "origin"},
    )
    context = ExecutionContext.from_job(job, budget=BudgetSpec())
    result = asyncio.run(executor.execute(job, context))
    assert result == "⏰ EURUS REMINDER\n\nstand up"


def test_pre_script_output_and_missing_skill_are_tolerated(home: Path, tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    class RecordingExecutor(AgentJobExecutor):
        async def execute(self, job: Job, context: ExecutionContext) -> str:
            from eurus.automation.executor import _job_prelude

            captured["prelude"] = await _job_prelude(context, self.config)
            return "ok"

    config = build_config({"agent": {"model": ""}, "permissions": {"workspace": str(tmp_path)}})
    executor = RecordingExecutor(config)
    job = JobStore(Database(home / "data" / "eurus.sqlite3")).enqueue(
        "eurus",
        "report",
        payload={"pre_script": "echo hello-from-prescript", "skills": ["no-such-skill"]},
    )
    context = ExecutionContext.from_job(job, budget=BudgetSpec())
    asyncio.run(executor.execute(job, context))
    assert "hello-from-prescript" in captured["prelude"]
    assert "[skill" not in captured["prelude"]


def test_pre_script_timeout_is_reported(home: Path, tmp_path: Path) -> None:
    from eurus.automation.executor import _run_pre_script

    config = build_config(
        {
            "agent": {"model": ""},
            "permissions": {"workspace": str(tmp_path)},
            "autonomy": {"pre_script_timeout_seconds": 1.0},
        }
    )
    output = asyncio.run(_run_pre_script("sleep 5", config))
    assert output == "[pre_script timed out]"


def _future() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()
