from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from eurus.agent.events import AgentEvent
from eurus.config import LoggingConfig
from eurus.errors import (
    AccessDeniedError,
    AllProvidersFailedError,
    ConfigError,
    InvalidRequestError,
    PolicyError,
    ProviderError,
    ProviderUnavailableError,
    RateLimitError,
    ToolError,
)
from eurus.gateway.approval import ApprovalBroker
from eurus.gateway.contracts import GatewayMessage
from eurus.gateway.health import describe_gateway
from eurus.gateway.manager import BackgroundTaskManager
from eurus.gateway.router import GatewayRouter
from eurus.gateway.tasks import DurableTask, TaskStore
from eurus.logs import RedactingFormatter, collect_secrets, setup_logging, shutdown_logging
from eurus.models import Message
from eurus.observability import (
    CorrelationFilter,
    bind_context,
    classify_error,
    correlation_suffix,
    current_context,
    sanitize_field,
    unbind_context,
)
from eurus.runtime.application import AgentRuntime
from eurus.sessions import SessionStore

TELEGRAM_TOKEN = "123456789:" + "A" * 35


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []
        self.addFilter(CorrelationFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def find(self, fragment: str) -> logging.LogRecord:
        return next(item for item in self.records if fragment in item.getMessage())


@contextmanager
def capture(*names: str) -> Iterator[Capture]:
    handler = Capture()
    loggers = [logging.getLogger(name) for name in names]
    levels = [item.level for item in loggers]
    for item in loggers:
        item.setLevel(logging.DEBUG)
        item.addHandler(handler)
    try:
        yield handler
    finally:
        for item, level in zip(loggers, levels, strict=True):
            item.removeHandler(handler)
            item.setLevel(level)


class RecordingAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    @property
    def platform(self) -> str:
        return "test"

    async def send_text(self, conversation_id: str, text: str) -> None:
        self.sent.append((conversation_id, text))

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def make_manager(tmp_path: Path, runner, **options: object) -> BackgroundTaskManager:
    adapter = RecordingAdapter()
    return BackgroundTaskManager(
        TaskStore(tmp_path / "tasks.sqlite3"), runner, adapter.send_text, **options
    )


def test_sanitize_field_prevents_log_injection_and_bounds_length() -> None:
    assert sanitize_field("abc\ninjected line\r\tx") == "abc_injected_line__x"
    assert sanitize_field("  padded  ") == "padded"
    assert len(sanitize_field("x" * 1000)) == 128
    assert sanitize_field("telegram:12345") == "telegram:12345"


def test_bind_context_merges_nests_and_restores() -> None:
    assert current_context() == {}
    outer = bind_context(platform="test", task_id="t1")
    inner = bind_context(session_id="s1", task_id="t2", correlation_id=None)
    assert current_context() == {"platform": "test", "task_id": "t2", "session_id": "s1"}
    unbind_context(inner)
    assert current_context() == {"platform": "test", "task_id": "t1"}
    unbind_context(outer)
    assert current_context() == {}


def test_bind_context_rejects_unknown_fields_and_skips_empty_values() -> None:
    with pytest.raises(ValueError):
        bind_context(user_message="secret text")
    token = bind_context(task_id="", session_id="   ")
    assert current_context() == {}
    unbind_context(token)


@pytest.mark.asyncio
async def test_context_flows_into_child_tasks_and_stays_isolated_between_siblings() -> None:
    seen: dict[str, dict[str, str]] = {}

    async def worker(name: str) -> None:
        token = bind_context(task_id=name)
        try:
            await asyncio.sleep(0.01)
            child = asyncio.create_task(asyncio.sleep(0, result=current_context()))
            seen[name] = await child
        finally:
            unbind_context(token)

    await asyncio.gather(worker("task-a"), worker("task-b"))
    assert seen == {"task-a": {"task_id": "task-a"}, "task-b": {"task_id": "task-b"}}
    assert current_context() == {}


def test_correlation_filter_uses_context_and_explicit_fields_without_raising() -> None:
    record = logging.LogRecord("eurus.x", logging.INFO, "f", 1, "msg", None, None)
    record.correlation = {"task_id": "explicit", "bogus": "ignored", "session_id": ""}
    token = bind_context(platform="test", task_id="ctx")
    try:
        assert CorrelationFilter().filter(record)
    finally:
        unbind_context(token)
    assert record.correlation == {"platform": "test", "task_id": "explicit"}
    assert correlation_suffix(record.correlation) == "task_id=explicit platform=test"


def test_correlation_suffix_has_stable_field_order() -> None:
    fields = {"session_id": "s", "platform": "p", "task_id": "t", "correlation_id": "c"}
    assert correlation_suffix(fields) == "correlation_id=c task_id=t session_id=s platform=p"
    assert correlation_suffix({}) == ""


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (asyncio.CancelledError(), "cancelled"),
        (TimeoutError(), "timeout"),
        (AccessDeniedError("no"), "authentication"),
        (RateLimitError("slow"), "rate_limit"),
        (InvalidRequestError("bad"), "invalid_request"),
        (ProviderUnavailableError("down"), "provider_unavailable"),
        (ProviderError("other"), "provider"),
        (AllProvidersFailedError([("a", ProviderError("x"))]), "provider"),
        (ToolError("tool"), "tool"),
        (PolicyError("policy"), "policy"),
        (ConfigError("config"), "config"),
        (ValueError("value"), "validation"),
        (KeyError("key"), "validation"),
        (ConnectionError("net"), "network"),
        (RuntimeError("boom"), "internal"),
    ],
)
def test_classify_error_maps_exceptions_to_stable_categories(
    error: BaseException, category: str
) -> None:
    assert classify_error(error) == category


def test_text_formatter_appends_correlation_and_neutralizes_injected_newlines() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    handler.addFilter(CorrelationFilter())
    logger = logging.getLogger("eurus.test.text")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    token = bind_context(task_id="abc\nFAKE LINE level=ERROR", session_id="s1")
    try:
        logger.info("hello")
    finally:
        unbind_context(token)
        logger.removeHandler(handler)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("hello [task_id=abc_FAKE_LINE_level_ERROR")
    assert "session_id=s1" in lines[0]


def test_json_log_lines_carry_correlation_fields_and_redact_bot_tokens(tmp_path: Path) -> None:
    target = tmp_path / "logs" / "eurus.log"
    config = SimpleNamespace(providers={})
    secrets = collect_secrets(config, {"EURUS_TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN})
    assert secrets == (TELEGRAM_TOKEN,)
    token = bind_context(task_id="task-1", session_id="session-1", platform="telegram")
    try:
        logger = setup_logging(LoggingConfig("INFO", str(target), True), secrets=secrets)
        logging.getLogger("eurus.test.json").info(
            "request to https://api.telegram.org/bot%s/getMe failed", TELEGRAM_TOKEN
        )
        logging.getLogger("eurus.test.json").info("pattern only 987654321:%s", "B" * 35)
        for handler in logger.handlers:
            handler.flush()
        lines = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    finally:
        unbind_context(token)
        shutdown_logging()
    assert lines[0]["task_id"] == "task-1"
    assert lines[0]["session_id"] == "session-1"
    assert lines[0]["platform"] == "telegram"
    raw = target.read_text(encoding="utf-8")
    assert TELEGRAM_TOKEN not in raw
    assert "B" * 35 not in raw
    assert "bot[REDACTED]/getMe" in lines[0]["message"]


def test_collect_secrets_ignores_missing_extra_tokens() -> None:
    assert collect_secrets(SimpleNamespace(providers={}), {}) == ()


@pytest.mark.asyncio
async def test_task_logs_carry_task_and_session_ids_with_timing(tmp_path: Path) -> None:
    async def runner(task: DurableTask) -> str:
        await asyncio.sleep(0.01)
        return "done"

    manager = make_manager(tmp_path, runner)
    with capture("eurus.gateway.manager") as handler:
        task = await manager.submit("1", "session-x", "work", notify_chat_id="10")
        await manager.active[task.task_id]
    started = handler.find("Task started")
    completed = handler.find("Task completed")
    for record in (started, completed):
        assert record.correlation["task_id"] == task.task_id
        assert record.correlation["session_id"] == "session-x"
    assert "attempt=1" in started.getMessage()
    assert "duration_ms=" in completed.getMessage()
    assert current_context() == {}
    await manager.shutdown()


@pytest.mark.asyncio
async def test_task_failures_report_a_category_and_never_the_exception_text(
    tmp_path: Path,
) -> None:
    async def runner(task: DurableTask) -> str:
        raise ToolError("secret path /home/user/.ssh/id_rsa")

    manager = make_manager(tmp_path, runner)
    with capture("eurus.gateway.manager") as handler:
        task = await manager.submit("1", "s", "work", notify_chat_id="10")
        await manager.active[task.task_id]
    failed = handler.find("Background task failed")
    assert "category=tool" in failed.getMessage()
    assert "error_type=ToolError" in failed.getMessage()
    assert ".ssh" not in failed.getMessage()
    assert failed.correlation["task_id"] == task.task_id
    await manager.shutdown()


@pytest.mark.asyncio
async def test_task_timeouts_are_categorized(tmp_path: Path) -> None:
    async def runner(task: DurableTask) -> str:
        await asyncio.Event().wait()
        return "never"

    manager = make_manager(tmp_path, runner, task_timeout=0.05)
    with capture("eurus.gateway.manager") as handler:
        task = await manager.submit("1", "s", "work", notify_chat_id="10")
        await manager.active[task.task_id]
    assert "category=timeout" in handler.find("Background task failed").getMessage()
    await manager.shutdown()


@pytest.mark.asyncio
async def test_cancelled_tasks_log_their_duration(tmp_path: Path) -> None:
    started = asyncio.Event()

    async def runner(task: DurableTask) -> str:
        started.set()
        await asyncio.Event().wait()
        return "never"

    manager = make_manager(tmp_path, runner)
    with capture("eurus.gateway.manager") as handler:
        task = await manager.submit("1", "s", "work", notify_chat_id="10")
        await started.wait()
        await manager.cancel(task.task_id, "1")
        await asyncio.gather(*manager.active.values(), return_exceptions=True)
    stopped = handler.find("Task stopped by cancellation")
    assert "duration_ms=" in stopped.getMessage()
    assert stopped.correlation["task_id"] == task.task_id
    await manager.shutdown()


@pytest.mark.asyncio
async def test_inbound_message_correlation_reaches_task_logs(tmp_path: Path) -> None:
    async def runner(task: DurableTask) -> str:
        return "done"

    adapter = RecordingAdapter()
    store = TaskStore(tmp_path / "tasks.sqlite3")
    manager = BackgroundTaskManager(store, runner, adapter.send_text)
    broker = ApprovalBroker()
    router = GatewayRouter(
        adapter=adapter,
        access=lambda message: True,
        store=store,
        manager=manager,
        broker=broker,
    )
    with capture("eurus.gateway.manager") as handler:
        await router.handle(GatewayMessage("test", "1", "10", "msg-77", "do it"))
        await asyncio.gather(*manager.active.values())
    started = handler.find("Task started")
    assert started.correlation["correlation_id"] == "test:msg-77"
    assert started.correlation["platform"] == "test"
    assert started.correlation["task_id"]
    assert current_context() == {}
    await manager.shutdown()
    await broker.close()


class StubAgent:
    def __init__(self, workspace: Path, fail: bool = False) -> None:
        self.history = [Message.system("system")]
        self.context = SimpleNamespace(workspace=workspace)
        self.fail = fail

    async def run(self, text: str) -> AsyncIterator[AgentEvent]:
        self.history.append(Message.user(text))
        yield AgentEvent.text_delta("working")
        if self.fail:
            raise ToolError("tool exploded with private details")
        self.history.append(Message.assistant("reply"))


def make_runtime(tmp_path: Path, fail: bool = False) -> AgentRuntime:
    return AgentRuntime(
        lambda session_id: StubAgent(tmp_path, fail),
        SessionStore(tmp_path / "sessions.sqlite3"),
    )


@pytest.mark.asyncio
async def test_runtime_logs_runs_with_session_task_and_duration(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    with capture("eurus.runtime.application") as handler:
        async for _ in runtime.run("session-1", "hello", task_id="task-9"):
            pass
    finished = handler.find("Run finished")
    assert finished.correlation == {"session_id": "session-1", "task_id": "task-9"}
    assert "events=1" in finished.getMessage()
    assert "duration_ms=" in finished.getMessage()
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_failures_log_category_without_exception_text(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, fail=True)
    with capture("eurus.runtime.application") as handler, pytest.raises(ToolError):
        async for _ in runtime.run("session-1", "hello"):
            pass
    failed = handler.find("Run failed")
    assert "category=tool" in failed.getMessage()
    assert "private details" not in failed.getMessage()
    assert failed.correlation == {"session_id": "session-1"}
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_and_manager_health_snapshots(tmp_path: Path) -> None:
    gate = asyncio.Event()
    entered = asyncio.Event()

    class GatedAgent(StubAgent):
        async def run(self, text: str) -> AsyncIterator[AgentEvent]:
            entered.set()
            await gate.wait()
            async for event in super().run(text):
                yield event

    runtime = AgentRuntime(
        lambda session_id: GatedAgent(tmp_path), SessionStore(tmp_path / "sessions.sqlite3")
    )
    assert runtime.health() == {
        "active_runs": 0,
        "cached_sessions": 0,
        "session_locks": 0,
        "subscribers": 0,
        "dropped_events": 0,
        "closed": False,
    }

    async def consume() -> None:
        async for _ in runtime.run("session-1", "hello"):
            pass

    worker = asyncio.create_task(consume())
    await entered.wait()
    during = runtime.health()
    assert during["active_runs"] == 1 and during["session_locks"] == 1
    assert runtime.health() == during
    gate.set()
    await worker
    after = runtime.health()
    assert after["active_runs"] == 0 and after["cached_sessions"] == 1
    await runtime.close()
    assert runtime.health()["closed"] is True

    async def runner(task: DurableTask) -> str:
        return "ok"

    manager = make_manager(tmp_path, runner, max_concurrent=3)
    assert manager.health() == {
        "active_tasks": 0,
        "max_concurrent": 3,
        "max_pending": 24,
        "pending_approvals": 0,
        "closed": False,
    }
    await manager.shutdown()
    assert manager.health()["closed"] is True


@pytest.mark.asyncio
async def test_manager_shutdown_logs_a_health_summary(tmp_path: Path) -> None:
    async def runner(task: DurableTask) -> str:
        return "ok"

    manager = make_manager(tmp_path, runner)
    with capture("eurus.gateway.manager") as handler:
        await manager.shutdown()
    assert "active_tasks=0" in handler.find("shutting down").getMessage()


def test_gateway_status_reports_review_items_and_failures_without_task_content(
    tmp_path: Path,
) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    risky = store.create("1", "s", "SECRET objective about passwords")
    store.transition(risky.task_id, "running")
    safe = store.create("1", "s", "never started objective")
    failed = store.create("1", "s", "SECRET failing objective")
    store.transition(failed.task_id, "running")
    store.transition(failed.task_id, "failed", error="TimeoutError")
    store.interrupt(risky.task_id, "gateway shutdown")
    store.interrupt(safe.task_id, "gateway shutdown")

    def snapshot() -> list[tuple[str, str, str]]:
        return [(task.task_id, task.status, task.updated_at) for task in store.list_owner("1", 100)]

    before = snapshot()
    status = describe_gateway(store)
    assert snapshot() == before

    assert status.counts == {"interrupted": 2, "failed": 1}
    assert [item.task_id for item in status.needs_review] == [risky.task_id[:8]]
    assert status.needs_review[0].recovery_action == "manual_review"
    assert [item.error for item in status.recent_failures] == ["TimeoutError"]
    rendered = repr(status)
    assert "SECRET" not in rendered and "passwords" not in rendered


def test_status_queries_validate_limits(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.sqlite3")
    assert store.count_by_status() == {}
    assert store.list_by_status("failed") == []
    for bad in (0, 101):
        with pytest.raises(ValueError):
            store.list_by_status("failed", bad)
