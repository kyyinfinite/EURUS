from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "ClaimFence",
    "claim_token_or_none",
    "fenced_transition",
    "new_claim_token",
]


@dataclass(frozen=True, slots=True)
class ClaimFence:
    key: str
    token: str
    claimed_at: str


def new_claim_token() -> str:
    return secrets.token_hex(16)


def claim_token_or_none(value: object) -> str | None:
    if value is None:
        return None
    token = str(value).strip()
    return token or None


def fenced_transition(
    connection: sqlite3.Connection,
    *,
    table: str,
    key_column: str,
    key_value: str,
    assignments: Mapping[str, object],
    expect: Mapping[str, object],
    extra_where: str = "",
    extra_params: Sequence[object] = (),
) -> bool:
    if not assignments:
        raise ValueError("assignments must not be empty")
    if not expect:
        raise ValueError("expect must not be empty")
    set_sql = ", ".join(f"{column}=?" for column in assignments)
    where_sql = f"{key_column}=? AND " + " AND ".join(f"{column}=?" for column in expect)
    if extra_where:
        where_sql = f"{where_sql} {extra_where}"
    params = [
        *assignments.values(),
        key_value,
        *expect.values(),
        *extra_params,
    ]
    cursor = connection.execute(
        f"UPDATE {table} SET {set_sql} WHERE {where_sql}",
        params,
    )
    return cursor.rowcount == 1
