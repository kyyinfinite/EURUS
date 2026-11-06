from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eurus import paths
from eurus.config import Config
from eurus.owner import OWNER_ID

log = logging.getLogger(__name__)

LOCK_FILE_NAME = "autonomy.lock"
STATE_FILE_NAME = "autonomy_state.json"
AUTONOMY_SUBSYSTEMS = ("goals", "workflows", "events", "specialists", "learning")
SUPERVISOR_SUBSYSTEM_MAP: dict[str, tuple[str, ...]] = {
    "goals": ("goals",),
    "workflows": ("workflows",),
    "events": ("events",),
    "specialists": ("assignments", "missions"),
    "learning": ("learning",),
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class RuntimeLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.path.parent, 0o700)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()

    @property
    def held(self) -> bool:
        return self._handle is not None


class DailyDispatchLimiter:
    def __init__(self, scheduler: Any, jobs: Any, *, limit: int, owner_id: str) -> None:
        self.scheduler = scheduler
        self.jobs = jobs
        self.limit = max(1, limit)
        self.owner_id = owner_id

    def dispatch_due(
        self, *, now: datetime | None = None, limit: int = 20, owner_id: str | None = None
    ) -> list[str]:
        current = now or _utcnow()
        if self._dispatched_today(current) >= self.limit:
            return []
        return list(self.scheduler.dispatch_due(now=now, limit=limit, owner_id=owner_id))

    def _dispatched_today(self, current: datetime | None = None) -> int:
        moment = current or _utcnow()
        since = _iso(moment.replace(hour=0, minute=0, second=0, microsecond=0))
        deadline = _iso(moment + timedelta(days=1))
        try:
            with self.jobs.database.connection() as connection:
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM eurus_jobs WHERE source='schedule' "
                    "AND owner_id=? AND created_at>=? AND created_at<?",
                    (self.owner_id, since, deadline),
                ).fetchone()
            return int(row["total"]) if row is not None else 0
        except Exception:
            log.exception("daily dispatch accounting failed; allowing dispatch")
            return 0


class AutonomyService:
    def __init__(
        self,
        config: Config,
        *,
        owner_id: str = OWNER_ID,
        notification_sink: Any = None,
        channels: Mapping[str, Any] | None = None,
        home: Path | None = None,
    ) -> None:
        self.config = config
        self.owner_id = owner_id
        self.notification_sink = notification_sink
        self.channels = dict(channels or {})
        home = home or paths.home_dir()
        self.home = Path(home)
        self._lock = RuntimeLock(self.home / "data" / LOCK_FILE_NAME)
        self._state_path = self.home / "data" / STATE_FILE_NAME
        self.mode = "stopped"
        self.started_at: float | None = None
        self._stack: Any = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._dispatcher_tasks: list[asyncio.Task[Any]] = []
        self._dispatchers: list[Any] = []
        self._delivery_store: Any = None
        self._client_database: Any = None
        self._stopping = asyncio.Event()
        self._toggles: dict[str, bool] = {name: True for name in AUTONOMY_SUBSYSTEMS}
        self._load_state()

    async def start(self) -> dict[str, Any]:
        if self.mode in ("owner", "client"):
            return self.status()
        if not self.config.autonomy.enabled:
            self.mode = "disabled"
            return self.status()
        paths.ensure_private_dir(self.home / "data")
        from eurus.storage.database import Database

        self._client_database = Database(paths.database_path())
        self._start_delivery_dispatchers()
        if self._lock.acquire():
            shared, self._client_database = self._client_database, None
            with contextlib.suppress(Exception):
                shared.close()
            await self._start_owner()
        else:
            self.mode = "client"
            log.info("autonomy runtime in client mode; another process owns the runtime")
        self.started_at = time.monotonic()
        from eurus.autonomy_hooks import publish_gateway_startup

        publish_gateway_startup(self.home, gateway="autonomy")
        self._migrate_legacy_reminders()
        return self.status()

    def _migrate_legacy_reminders(self) -> None:
        from eurus.reminders import ReminderStore, migrate_reminders_to_schedules
        from eurus.scheduler import ScheduleStore

        with contextlib.suppress(Exception):
            migrated = migrate_reminders_to_schedules(
                ReminderStore(paths.database_path()),
                ScheduleStore(paths.database_path()),
            )
            if migrated:
                log.info("Migrated %d legacy reminder(s) to the scheduler", migrated)

    async def _start_owner(self) -> None:
        from eurus.cli.autonomy import build_stack

        autonomy_config = self.config.autonomy
        sink = self._notification_sink()
        stack = build_stack(
            self.config,
            workers=autonomy_config.workers,
            poll_seconds=autonomy_config.poll_seconds,
            owner_id=self.owner_id,
            enable_goals=autonomy_config.goals and self._toggles["goals"],
            enable_workflows=autonomy_config.workflows and self._toggles["workflows"],
            enable_events=autonomy_config.events and self._toggles["events"],
            enable_specialists=autonomy_config.specialists and self._toggles["specialists"],
            enable_learning=autonomy_config.learning and self._toggles["learning"],
            notification_sink=sink,
        )
        self._stack = stack
        stack.automation.scheduler = DailyDispatchLimiter(
            stack.schedule_store,
            stack.automation.jobs,
            limit=autonomy_config.daily_job_limit,
            owner_id=self.owner_id,
        )
        stopping = self._stopping
        interval = max(0.05, min(autonomy_config.poll_seconds, 300.0))
        supervisor = stack.supervisor

        async def supervision_loop() -> None:
            consecutive_failures = 0
            while not stopping.is_set():
                wait_seconds = interval
                try:
                    report = await supervisor.supervise_async()
                except Exception:
                    consecutive_failures += 1
                    wait_seconds = min(2.0 * (2 ** (consecutive_failures - 1)), 300.0)
                    log.exception(
                        "autonomy supervision tick failed (consecutive=%s, retrying in %.1fs)",
                        consecutive_failures,
                        wait_seconds,
                    )
                else:
                    if consecutive_failures:
                        log.info(
                            "autonomy supervision recovered after %s consecutive failures",
                            consecutive_failures,
                        )
                    consecutive_failures = 0
                    for name, error in report.subsystem_errors.items():
                        log.warning("autonomy subsystem %s reported an error: %s", name, error)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stopping.wait(), timeout=wait_seconds)

        workers_task = asyncio.create_task(
            stack.automation.run_workers(), name="eurus-autonomy-workers"
        )
        supervisor_task = asyncio.create_task(supervision_loop(), name="eurus-autonomy-supervisor")
        self._tasks = [workers_task, supervisor_task]
        self._seed_specialists()
        self.mode = "owner"
        log.info(
            "autonomy runtime owns the lock pid=%s workers=%s poll=%.1fs",
            os.getpid(),
            autonomy_config.workers,
            autonomy_config.poll_seconds,
        )

    def _seed_specialists(self) -> None:
        from eurus.multiagent.registry import SpecialistRegistry, seed_builtin_specialists

        with contextlib.suppress(Exception):
            seed_builtin_specialists(SpecialistRegistry(paths.database_path()))

    def _start_client(self) -> None:
        from eurus.storage.database import Database

        if self._client_database is None:
            self._client_database = Database(paths.database_path())
        self.mode = "client"
        log.info("autonomy runtime in client mode; another process owns the runtime")

    def _notification_sink(self) -> Any:
        if self.notification_sink is not None:
            return self.notification_sink
        from eurus.automation.deliveries import DeliveryStore, RoutingNotificationSink

        self._delivery_store = DeliveryStore(paths.database_path())
        return RoutingNotificationSink(self._delivery_store)

    def _start_delivery_dispatchers(self) -> None:
        from eurus.automation.deliveries import (
            DeliveryStore,
            LocalFileSink,
            TuiInboxSink,
        )

        if self._delivery_store is None:
            self._delivery_store = DeliveryStore(paths.database_path())
        poll = self._dispatcher_poll_seconds()
        senders: dict[str, Any] = {
            "local": LocalFileSink(self.home / "inbox"),
            "tui": TuiInboxSink(),
        }
        for channel, sender in {**senders, **self.channels}.items():
            self._spawn_dispatcher(channel, sender, poll)

    def _dispatcher_poll_seconds(self) -> float:
        return min(5.0, max(0.5, float(self.config.autonomy.poll_seconds) / 3.0))

    def _spawn_dispatcher(self, channel: str, sender: Any, poll: float) -> None:
        from eurus.automation.deliveries import DeliveryDispatcher

        dispatcher = DeliveryDispatcher(self._delivery_store, channel, sender, poll_seconds=poll)
        self._dispatchers.append(dispatcher)
        self._dispatcher_tasks.append(
            asyncio.create_task(dispatcher.run(), name=f"eurus-delivery-{channel}")
        )

    def register_channel(self, name: str, sender: Any) -> None:
        from eurus.automation.deliveries import CHANNELS, DeliveryStore

        if name not in CHANNELS:
            raise ValueError(f"unknown delivery channel: {name}")
        self.channels[name] = sender
        if self.mode in ("owner", "client"):
            if self._delivery_store is None:
                self._delivery_store = DeliveryStore(paths.database_path())
            self._spawn_dispatcher(name, sender, self._dispatcher_poll_seconds())

    async def stop(self) -> None:
        self._stopping.set()
        tasks, self._tasks = self._tasks, []
        stack, self._stack = self._stack, None
        database, self._client_database = self._client_database, None
        dispatchers, self._dispatchers = self._dispatchers, []
        dispatcher_tasks, self._dispatcher_tasks = self._dispatcher_tasks, []
        try:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for dispatcher in dispatchers:
                with contextlib.suppress(Exception):
                    await dispatcher.stop()
            for task in dispatcher_tasks:
                task.cancel()
            await asyncio.gather(*dispatcher_tasks, return_exceptions=True)
            if database is not None:
                with contextlib.suppress(Exception):
                    database.close()
            if stack is not None:
                with contextlib.suppress(Exception):
                    await stack.automation.stop_workers()
                with contextlib.suppress(Exception):
                    stack.supervisor.close()
                with contextlib.suppress(Exception):
                    stack.database.close()
            self._lock.release()
            self.mode = "stopped"
            self.started_at = None
            self._stopping = asyncio.Event()

    def status(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "mode": self.mode,
            "owner_id": self.owner_id,
            "pid": os.getpid() if self.mode == "owner" else None,
            "uptime_seconds": round(time.monotonic() - self.started_at, 1)
            if self.started_at is not None
            else 0.0,
            "subsystems": dict(self._toggles),
            "channels": sorted(self._dispatcher_channels()),
            "config": {
                "enabled": self.config.autonomy.enabled,
                "workers": self.config.autonomy.workers,
                "poll_seconds": self.config.autonomy.poll_seconds,
                "daily_job_limit": self.config.autonomy.daily_job_limit,
            },
        }
        if self.mode == "owner" and self._stack is not None:
            with contextlib.suppress(Exception):
                data["metrics"] = self._stack.automation.metrics()
            with contextlib.suppress(Exception):
                data["health"] = self._stack.supervisor.health()
        elif self.mode == "client" and self._client_database is not None:
            data["metrics"] = self._client_metrics()
        return data

    def health(self) -> dict[str, Any]:
        if self.mode == "owner" and self._stack is not None:
            with contextlib.suppress(Exception):
                return dict(self._stack.supervisor.health())
        return {"mode": self.mode, "running": self.mode in ("owner", "client")}

    def _dispatcher_channels(self) -> list[str]:
        from eurus.automation.deliveries import CHANNELS

        return [name for name in ("local", "tui", *self.channels) if name in CHANNELS]

    def set_subsystem(self, name: str, enabled: bool) -> bool:
        if name not in AUTONOMY_SUBSYSTEMS:
            raise ValueError(f"unknown autonomy subsystem: {name}")
        return self._apply_toggles({name: bool(enabled)})

    def _apply_toggles(self, changes: dict[str, bool]) -> bool:
        applied = False
        for name, value in changes.items():
            if name not in self._toggles or self._toggles[name] == value:
                continue
            self._toggles[name] = value
            applied = True
            if self.mode == "owner" and self._stack is not None:
                supervisor_names = SUPERVISOR_SUBSYSTEM_MAP.get(name, ())
                supervisor = getattr(self._stack, "supervisor", None)
                if supervisor is not None and supervisor_names:
                    current = supervisor.enabled_subsystems
                    if current is None:
                        from eurus.automation.supervision import SUBSYSTEM_NAMES

                        current = set(SUBSYSTEM_NAMES)
                        supervisor.enabled_subsystems = current
                    for supervisor_name in supervisor_names:
                        if value:
                            current.add(supervisor_name)
                        else:
                            current.discard(supervisor_name)
        if applied:
            self._save_state()
        return applied

    def _load_state(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            toggles = raw.get("toggles", {})
            for name, value in toggles.items():
                if name in self._toggles:
                    self._toggles[name] = bool(value)
        except (OSError, ValueError):
            return

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._state_path.write_text(
                json.dumps({"toggles": self._toggles}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            with contextlib.suppress(OSError):
                os.chmod(self._state_path, 0o600)
        except OSError:
            log.exception("failed to persist autonomy toggles")

    def _client_metrics(self) -> dict[str, Any]:
        database = self._client_database
        counts: dict[str, int] = {}
        try:
            with database.connection() as connection:
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS total FROM eurus_jobs GROUP BY status"
                ).fetchall():
                    counts[str(row["status"])] = int(row["total"])
        except Exception:
            log.exception("client status query failed")
        return {"jobs_by_status": counts}


__all__ = [
    "AUTONOMY_SUBSYSTEMS",
    "AutonomyService",
    "DailyDispatchLimiter",
    "RuntimeLock",
]
