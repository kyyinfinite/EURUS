from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from eurus.config import Config, load_config
from eurus.memory.embeddings import (
    HttpEmbeddingProvider,
    NullEmbeddingProvider,
    build_embedding_provider,
)


def test_fenced_transition_rejects_stale_claimer(tmp_path: Path) -> None:
    from eurus.storage.leases import fenced_transition, new_claim_token

    connection = sqlite3.connect(tmp_path / "fence.sqlite3")
    connection.execute("CREATE TABLE rows (id TEXT PRIMARY KEY, status TEXT, token TEXT)")
    connection.execute("INSERT INTO rows VALUES ('r1', 'claimed', '')")
    connection.commit()
    first = new_claim_token()
    assert fenced_transition(
        connection,
        table="rows",
        key_column="id",
        key_value="r1",
        assignments={"token": first},
        expect={"status": "claimed"},
    )
    second = new_claim_token()
    assert fenced_transition(
        connection,
        table="rows",
        key_column="id",
        key_value="r1",
        assignments={"token": second},
        expect={"status": "claimed"},
    )
    assert (
        fenced_transition(
            connection,
            table="rows",
            key_column="id",
            key_value="r1",
            assignments={"token": "done"},
            expect={"status": "claimed", "token": first},
        )
        is False
    ), "the first (stale) claimer must be fenced out after the token rotated"
    assert (
        fenced_transition(
            connection,
            table="rows",
            key_column="id",
            key_value="r1",
            assignments={"token": "done"},
            expect={"status": "claimed", "token": second},
        )
        is True
    )
    with pytest.raises(ValueError):
        fenced_transition(
            connection,
            table="rows",
            key_column="id",
            key_value="r1",
            assignments={},
            expect={"status": "claimed"},
        )
    connection.close()


async def test_event_delivery_claim_token_fencing(tmp_path: Path) -> None:
    from eurus.events.store import DomainEventStore
    from eurus.storage.database import Database

    store = DomainEventStore(Database(tmp_path / "events.sqlite3"))
    event = store.publish("PING", subject_id="s1")
    assert event is not None
    subscription = store.create_subscription("sub", event_types=["PING"], handler_kind="notify")
    assert store.fan_out(event) == 1
    delivery = store.due_deliveries()[0]

    first = store.mark_delivering(delivery.delivery_id)
    assert first is not None and first.claim_token
    assert store.ack_delivery(delivery.delivery_id, claim_token="wrong-token") is False
    assert store.ack_delivery(delivery.delivery_id, claim_token=first.claim_token) is True
    _ = subscription

    event2 = store.publish("PING", subject_id="s2")
    assert event2 is not None
    store.fan_out(event2)
    delivery2 = store.due_deliveries()[0]
    claimed2 = store.mark_delivering(delivery2.delivery_id)
    assert claimed2 is not None
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime
    from datetime import timedelta as _timedelta

    future = _datetime.now(_UTC) + _timedelta(seconds=400)
    assert store.recover_stale_delivering(now=future) == 1
    reclaimed = store.mark_delivering(delivery2.delivery_id)
    assert reclaimed is not None
    assert reclaimed.claim_token != claimed2.claim_token
    assert (
        store.fail_delivery(
            delivery2.delivery_id,
            error="late failure from stale claimer",
            claim_token=claimed2.claim_token,
        )
        == "fenced"
    ), "a stale claimer must not mutate the state owned by the new claimer"
    assert store.fail_delivery(
        delivery2.delivery_id,
        error="real failure",
        claim_token=reclaimed.claim_token,
        max_attempts=1,
    ) in {"retry", "dead"}


async def test_hub_backpressure_counters(tmp_path: Path) -> None:
    from eurus.gateway.web.hub import SessionHub
    from eurus.storage.events import EventStore
    from eurus.storage.runs import RunStore

    database_path = tmp_path / "web.sqlite3"
    hub = SessionHub(RunStore(database_path), EventStore(database_path))

    class StuckSocket:
        def __init__(self) -> None:
            self.closed = False

        async def send_text(self, text: str) -> None:
            await asyncio.sleep(30)

        async def close(self) -> None:
            self.closed = True

    class FastSocket:
        sent: list[str] = []

        async def send_text(self, text: str) -> None:
            FastSocket.sent.append(text)

        async def close(self) -> None:
            pass

    hub.send_timeout_seconds = 0.05
    stuck = StuckSocket()
    fast = FastSocket()
    await hub.attach(stuck)
    await hub.attach(fast)
    hub.subscribe(stuck, ["session-1"])
    hub.subscribe(fast, ["session-1"])
    await hub.broadcast("session-1", {"type": "ping"})
    snapshot = hub.backpressure()
    assert snapshot.clients == 1, "the stuck client must be dropped"
    assert snapshot.broadcasts_total == 1
    assert snapshot.sends_total == 2
    assert snapshot.send_timeouts_total == 1
    assert snapshot.dropped_clients_total == 1
    assert snapshot.in_flight_sends == 0
    assert snapshot.send_timeout_seconds == 0.05
    assert FastSocket.sent, "the healthy client must still receive the message"
    assert stuck.closed

    diagnostics = hub.backpressure().to_dict()
    assert diagnostics["clients"] == 1
    assert set(diagnostics) == {
        "clients",
        "watched_sessions",
        "tracked_runs",
        "broadcasts_total",
        "sends_total",
        "send_timeouts_total",
        "send_failures_total",
        "dropped_clients_total",
        "in_flight_sends",
        "slowest_send_seconds",
        "send_timeout_seconds",
    }


def test_embedding_provider_defaults_to_lexical() -> None:
    null_provider = build_embedding_provider(None)
    assert isinstance(null_provider, NullEmbeddingProvider)
    assert null_provider.available is False
    with pytest.raises(RuntimeError):
        null_provider.embed(["text"])

    config = Config()
    assert isinstance(build_embedding_provider(config), NullEmbeddingProvider)


def test_http_embedding_provider_validates_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from eurus.config import ConfigError

    with pytest.raises(ConfigError):
        load_config(
            overrides={
                "memory": {
                    "embedding": {
                        "provider": "http",
                        "endpoint": "ftp://nope",
                        "model": "m",
                        "api_key_env": "K",
                    }
                }
            }
        )
    with pytest.raises(ConfigError):
        load_config(
            overrides={
                "memory": {
                    "embedding": {
                        "provider": "http",
                        "endpoint": "https://embed.example/v1/embeddings",
                    }
                }
            }
        )
    config = load_config(
        overrides={
            "memory": {
                "embedding": {
                    "provider": "http",
                    "endpoint": "https://embed.example/v1/embeddings",
                    "model": "text-embed-3",
                    "api_key_env": "EURUS_EMBED_KEY",
                    "dimension": 3,
                }
            }
        }
    )
    assert build_embedding_provider(config).available is False, (
        "a missing api key must fall back to lexical retrieval"
    )
    monkeypatch.setenv("EURUS_EMBED_KEY", "test-key")
    provider = build_embedding_provider(config)
    assert isinstance(provider, HttpEmbeddingProvider)
    assert provider.dimension == 3

    calls: list[dict[str, Any]] = []

    def transport(url: str, *, json: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        calls.append({"url": url, "json": json, "headers": headers})
        return {
            "data": [
                {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                {"index": 0, "embedding": [1.0, 0.0, 0.0]},
            ]
        }

    provider._transport = transport
    vectors = provider.embed(["query", "document"])
    assert len(vectors) == 2
    assert vectors[0] == [1.0, 0.0, 0.0]
    assert calls[0]["headers"]["authorization"] == "Bearer test-key"
    assert calls[0]["json"]["input"] == ["query", "document"]
    assert provider.similarity(vectors[0], vectors[1]) == pytest.approx(0.0)
    assert provider.similarity(vectors[0], vectors[0]) == pytest.approx(1.0)


def test_skill_effectiveness_gates_auto_loading(tmp_path: Path) -> None:
    from eurus.automation.executor import _load_skill_blocks
    from eurus.learning.effectiveness import SkillOutcomeStore

    workspace = tmp_path / "workspace"
    skills_root = workspace / ".eurus" / "skills"
    skills_root.mkdir(parents=True)
    (skills_root / "reliable").mkdir()
    (skills_root / "reliable" / "SKILL.md").write_text(
        "---\nname: reliable\ndescription: always works\n---\n\ndo the right thing\n",
        encoding="utf-8",
    )
    (skills_root / "broken").mkdir()
    (skills_root / "broken" / "SKILL.md").write_text(
        "---\nname: broken\ndescription: keeps failing\n---\n\ndo the wrong thing\n",
        encoding="utf-8",
    )

    config = load_config(overrides={"permissions": {"workspace": str(workspace)}})

    store = SkillOutcomeStore(tmp_path / "agent.sqlite3")
    for _ in range(6):
        store.record("broken", False)
    for _ in range(6):
        store.record("reliable", True)

    blocks = _load_skill_blocks(
        ["reliable", "broken"], config, outcome_store=store, owner_id="local"
    )
    joined = "".join(blocks)
    assert "[skill reliable]" in joined
    assert "[skill broken]" not in joined, "a demoted skill must not be auto-loaded"

    ungated = _load_skill_blocks(["reliable", "broken"], config)
    assert "[skill reliable]" in "".join(ungated)
    assert "[skill broken]" in "".join(ungated)


def test_memory_manager_consolidate_dry_run_and_bounds(tmp_path: Path) -> None:
    from eurus.memory.manager import MemoryManager

    manager = MemoryManager(tmp_path / "agent.sqlite3", owner_id="alice")
    manager.remember("alpha fact one", category="fact")
    dry = manager.consolidate(dry_run=True)
    assert dry == {"merged": 0, "deleted": 0, "groups": [], "dry_run": True}
    with pytest.raises(ValueError):
        manager.consolidate(max_merges=0)
    with pytest.raises(ValueError):
        MemoryManager(tmp_path / "agent.sqlite3", owner_id="x" * 201)


async def test_supervise_async_offloads_storage_but_keeps_events_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eurus.automation.engine import AutomationEngine
    from eurus.automation.supervision import RuntimeSupervisor
    from eurus.storage.database import Database

    database = Database(tmp_path / "supervision.sqlite3")

    class NoopExecutor:
        async def execute(self, job: Any, context: Any) -> str:
            return "done"

    automation = AutomationEngine(database, NoopExecutor(), poll_seconds=0.05)
    supervisor = RuntimeSupervisor(automation, owner_id="local")
    threads: dict[str, int] = {}
    loop_thread = threading.get_ident()

    original = supervisor._run_subsystem

    def tracking_run_subsystem(name: str, report: Any, operation: Any, current: Any) -> None:
        threads[name] = threading.get_ident()
        original(name, report, operation, current)

    monkeypatch.setattr(supervisor, "_run_subsystem", tracking_run_subsystem)
    report = await supervisor.supervise_async()
    assert isinstance(report, object)
    assert "events" in threads, "the event subsystem must run on the event loop thread"
    assert threads["events"] == loop_thread
    offloaded = {"leases", "assignments", "workflows", "goals", "runs", "trajectory"}
    for name in offloaded:
        assert threads.get(name) != loop_thread, f"{name} must run off the event loop"
    sync_report = supervisor.supervise()
    assert isinstance(sync_report, object)
