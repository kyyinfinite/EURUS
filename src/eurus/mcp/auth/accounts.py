from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from eurus.mcp.jsonio import atomic_write_text, read_json_dict

STATUS_CONNECTED = "connected"
STATUS_REAUTH_REQUIRED = "reauth_required"
STATUS_LOGGED_OUT = "logged_out"
STATUS_STORE_UNAVAILABLE = "store_unavailable"
STATUS_TRANSIENT_ERROR = "transient_error"

_KNOWN_STATUSES = {
    STATUS_CONNECTED,
    STATUS_REAUTH_REQUIRED,
    STATUS_LOGGED_OUT,
    STATUS_STORE_UNAVAILABLE,
    STATUS_TRANSIENT_ERROR,
}

_SAFE_ID = re.compile(r"[^a-z0-9_-]+")


def _as_float(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


@dataclass(slots=True)
class GoogleAccount:
    account_id: str
    email: str
    created_at: float = field(default_factory=time.time)
    last_used: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> GoogleAccount:
        return cls(
            account_id=str(data.get("account_id", "")),
            email=str(data.get("email", "")),
            created_at=_as_float(data.get("created_at", 0.0)),
            last_used=_as_float(data.get("last_used", 0.0)),
        )


def account_id_for_email(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    slug = _SAFE_ID.sub("-", local).strip("-") or "account"
    digest = hashlib.sha256(email.lower().encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{digest}"[:48]


class GoogleAccountManager:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._accounts: dict[str, GoogleAccount] = {}
        self._active: str = ""
        self._statuses: dict[str, str] = {}
        self.load_warnings: list[str] = []
        self._load()

    def _load(self) -> None:
        raw, warning = read_json_dict(self.path)
        if warning:
            self.load_warnings.append(warning)
        if raw is None:
            return
        for item in raw.get("accounts", []):
            if isinstance(item, dict):
                account = GoogleAccount.from_dict(item)
                if account.account_id:
                    self._accounts[account.account_id] = account
        active = raw.get("active")
        if isinstance(active, str) and active in self._accounts:
            self._active = active
        statuses = raw.get("statuses")
        if isinstance(statuses, dict):
            for account_id, status in statuses.items():
                if (
                    isinstance(account_id, str)
                    and account_id in self._accounts
                    and str(status) in _KNOWN_STATUSES
                ):
                    self._statuses[account_id] = str(status)

    def save(self) -> None:
        payload = {
            "active": self._active,
            "accounts": [account.to_dict() for account in self._accounts.values()],
            "statuses": dict(self._statuses),
        }
        atomic_write_text(self.path, json.dumps(payload, indent=2) + "\n")

    def add(self, email: str) -> GoogleAccount:
        account_id = account_id_for_email(email)
        account = self._accounts.get(account_id)
        if account is None:
            account = GoogleAccount(account_id=account_id, email=email)
            self._accounts[account_id] = account
        if not self._active:
            self._active = account_id
        self.save()
        return account

    def remove(self, account_id: str) -> bool:
        removed = self._accounts.pop(account_id, None)
        if removed is None:
            return False
        if self._active == account_id:
            remaining = sorted(self._accounts)
            self._active = remaining[0] if remaining else ""
        self._statuses.pop(account_id, None)
        self.save()
        return True

    def list(self) -> list[GoogleAccount]:
        return sorted(self._accounts.values(), key=lambda item: item.email)

    def get(self, account_id: str) -> GoogleAccount | None:
        return self._accounts.get(account_id)

    def by_email(self, email: str) -> GoogleAccount | None:
        account_id = account_id_for_email(email)
        return self._accounts.get(account_id)

    @property
    def active(self) -> GoogleAccount | None:
        return self._accounts.get(self._active) if self._active else None

    @property
    def active_id(self) -> str:
        return self._active

    def set_active(self, account_id: str) -> GoogleAccount:
        account = self._accounts.get(account_id)
        if account is None:
            raise ValueError(f"unknown Google account: {account_id}")
        self._active = account_id
        account.last_used = time.time()
        self.save()
        return account

    def mark_used(self, account_id: str) -> None:
        account = self._accounts.get(account_id)
        if account is not None:
            account.last_used = time.time()
            self.save()

    def set_status(self, account_id: str, status: str) -> None:
        if status not in _KNOWN_STATUSES:
            raise ValueError(f"unknown account status: {status}")
        if account_id not in self._accounts:
            return
        if self._statuses.get(account_id) == status:
            return
        self._statuses[account_id] = status
        self.save()

    def status_of(self, account_id: str) -> str:
        if account_id not in self._accounts:
            return STATUS_LOGGED_OUT
        return self._statuses.get(account_id, STATUS_LOGGED_OUT)
