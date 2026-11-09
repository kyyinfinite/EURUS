from __future__ import annotations

import contextlib
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eurus import paths
from eurus.owner import OWNER_ID

log = logging.getLogger(__name__)

HOOK_EVENT_TYPES = ("gateway:startup", "agent:start", "agent:step", "agent:end")


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


class HookPublisher:
    def __init__(self, database: Any, *, owner_id: str = OWNER_ID) -> None:
        from eurus.events.bus import EventBus

        self.bus = EventBus(database) if not isinstance(database, EventBus) else database
        self.owner_id = owner_id

    @classmethod
    def for_home(cls, home: Path | None = None, *, owner_id: str = OWNER_ID) -> HookPublisher:
        from eurus.storage.database import Database

        target = home or paths.home_dir()
        return cls(Database(Path(target) / "data" / "eurus.sqlite3"), owner_id=owner_id)

    def startup(self, *, gateway: str, pid: int) -> None:
        self._publish(
            "gateway:startup",
            subject_type="gateway",
            subject_id=gateway,
            payload={"gateway": gateway, "pid": pid, "at": _utcnow()},
            dedup_key=f"gateway:startup:{gateway}:{pid}",
        )

    def agent_start(self, *, run_id: str, gateway: str, session_id: str) -> None:
        self._publish(
            "agent:start",
            subject_type="run",
            subject_id=run_id,
            payload={"gateway": gateway, "session_id": session_id},
            correlation_id=run_id,
        )

    def agent_step(self, *, run_id: str, gateway: str, step: int, detail: str = "") -> None:
        self._publish(
            "agent:step",
            subject_type="run",
            subject_id=run_id,
            payload={"gateway": gateway, "step": step, "detail": detail[:200]},
            correlation_id=run_id,
            dedup_key=f"agent:step:{run_id}:{step}",
        )

    def agent_end(self, *, run_id: str, gateway: str, status: str) -> None:
        self._publish(
            "agent:end",
            subject_type="run",
            subject_id=run_id,
            payload={"gateway": gateway, "status": status},
            correlation_id=run_id,
        )

    def _publish(self, event_type: str, **kwargs: Any) -> None:
        try:
            self.bus.publish(event_type, owner_id=self.owner_id, **kwargs)
        except Exception:
            log.debug("hook %s could not be published", event_type, exc_info=True)


class StepCounter:
    def __init__(self) -> None:
        self.value = 0

    def next(self) -> int:
        self.value += 1
        return self.value


def publish_gateway_startup(home: Path | None = None, *, gateway: str) -> None:
    with contextlib.suppress(Exception):
        HookPublisher.for_home(home).startup(gateway=gateway, pid=os.getpid())


__all__ = [
    "HOOK_EVENT_TYPES",
    "HookPublisher",
    "StepCounter",
    "publish_gateway_startup",
]
