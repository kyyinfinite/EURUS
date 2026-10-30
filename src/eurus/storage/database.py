from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TypeAlias

from eurus.runtime import instrumentation
from eurus.storage.migrations import apply_migrations
from eurus.storage.sqlite import configure

DatabasePath: TypeAlias = str | Path

log = logging.getLogger(__name__)


class _StatementCounter:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self, statement: str) -> None:
        head = statement.lstrip()[:6].upper()
        if head.startswith("BEGIN") or head.startswith("COMMIT"):
            return
        self.count += 1


class Database:
    """File-backed SQLite runtime storage.

    Connections are pooled and reused across operations. Every connection runs
    with WAL journalling, ``synchronous=NORMAL`` and a busy timeout, so durable
    writes stay cheap and multiple processes can read while one writes.
    """

    def __init__(self, path: DatabasePath) -> None:
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("durable runtime storage requires a file-backed SQLite database")
        target = Path(self.path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.touch(mode=0o600, exist_ok=True)
        self.path = str(target)
        with suppress(OSError):
            os.chmod(target, 0o600)
        self._pool: list[sqlite3.Connection] = []
        self._pool_lock = threading.Lock()
        with self.connection() as connection:
            apply_migrations(connection)

    def _new_connection(self) -> sqlite3.Connection:
        # check_same_thread=False: pooled connections may be used from
        # asyncio.to_thread workers. Exclusive use is guaranteed by the pool.
        connection = sqlite3.connect(self.path, timeout=10.0, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        if configure(connection, foreign_keys=True) != "wal":
            log.warning("WAL journalling unavailable for %s; using rollback journal", self.path)
        return connection

    def connect(self) -> sqlite3.Connection:
        with self._pool_lock:
            if self._pool:
                return self._pool.pop()
        return self._new_connection()

    def _release(self, connection: sqlite3.Connection) -> None:
        with self._pool_lock:
            self._pool.append(connection)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        timed = instrumentation.enabled()
        if timed:
            started = time.perf_counter()
            counter = _StatementCounter()
            connection.set_trace_callback(counter)
        try:
            yield connection
            connection.commit()
        except BaseException:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise
        finally:
            if timed:
                connection.set_trace_callback(None)
                instrumentation.record_sql(counter.count, time.perf_counter() - started)
            self._release(connection)

    def close(self) -> None:
        """Close every pooled connection (WAL checkpoints on close)."""
        with self._pool_lock:
            while self._pool:
                connection = self._pool.pop()
                with suppress(sqlite3.Error):
                    connection.close()
