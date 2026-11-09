from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from os import PathLike
from typing import Literal

BUSY_TIMEOUT_SECONDS = 10.0
PathArg = str | PathLike[str]


def configure(connection: sqlite3.Connection, *, foreign_keys: bool = False) -> str:
    connection.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
    if foreign_keys:
        connection.execute("PRAGMA foreign_keys = ON")
    mode = "delete"
    with suppress(sqlite3.OperationalError):
        mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
    if mode == "wal":
        connection.execute("PRAGMA synchronous = NORMAL")
    return mode


def connect(
    path: PathArg,
    *,
    rows: bool = False,
    foreign_keys: bool = False,
    autocommit: bool = False,
) -> sqlite3.Connection:
    level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None = None if autocommit else "DEFERRED"
    connection = sqlite3.connect(
        str(path),
        timeout=BUSY_TIMEOUT_SECONDS,
        isolation_level=level,
    )
    try:
        if rows:
            connection.row_factory = sqlite3.Row
        configure(connection, foreign_keys=foreign_keys)
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def managed(
    path: PathArg,
    *,
    rows: bool = False,
    foreign_keys: bool = False,
) -> Iterator[sqlite3.Connection]:
    connection = connect(path, rows=rows, foreign_keys=foreign_keys)
    try:
        yield connection
        connection.commit()
    except BaseException:
        with suppress(sqlite3.Error):
            connection.rollback()
        raise
    finally:
        connection.close()
