"""Tests for secure credential storage and multi-account management."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from eurus.mcp.auth.accounts import (
    STATUS_LOGGED_OUT,
    GoogleAccountManager,
    account_id_for_email,
)
from eurus.mcp.auth.token_store import (
    PASSPHRASE_ENV_VAR,
    CredentialStore,
    CredentialStoreError,
    EncryptedFileBackend,
    RefusingBackend,
    StoredCredentials,
)


def _credentials(account_id: str = "acct-1", **changes: object) -> StoredCredentials:
    data: dict[str, object] = {
        "account_id": account_id,
        "email": "user@example.com",
        "client_id": "cid",
        "refresh_token": "refresh-secret",
        "access_token": "access-secret",
        "expires_at": time.time() + 3600,
        "scope": "openid email",
    }
    data.update(changes)
    return StoredCredentials(**data)  # type: ignore[arg-type]


def test_encrypted_store_round_trip(tmp_path: Path) -> None:
    store = CredentialStore(EncryptedFileBackend(tmp_path, "passphrase-1"))
    assert store.backend_name == "encrypted-file"
    store.store(_credentials())
    loaded = store.load("acct-1")
    assert loaded is not None
    assert loaded.refresh_token == "refresh-secret"
    assert loaded.email == "user@example.com"


def test_encrypted_store_rejects_wrong_passphrase(tmp_path: Path) -> None:
    CredentialStore(EncryptedFileBackend(tmp_path, "right")).store(_credentials())
    wrong = CredentialStore(EncryptedFileBackend(tmp_path, "wrong"))
    try:
        wrong.load("acct-1")
        raise AssertionError("wrong passphrase must not decrypt")
    except CredentialStoreError as exc:
        assert "wrong passphrase" in str(exc)


def test_stored_tokens_are_not_plaintext(tmp_path: Path) -> None:
    store = CredentialStore(EncryptedFileBackend(tmp_path, "passphrase-1"))
    store.store(_credentials())
    raw = (tmp_path / "acct-1.token").read_text(encoding="ascii")
    assert "refresh-secret" not in raw
    assert "access-secret" not in raw
    assert (tmp_path / "acct-1.token").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / ".salt").stat().st_mode & 0o777 == 0o600


def test_store_update_changes_fields(tmp_path: Path) -> None:
    store = CredentialStore(EncryptedFileBackend(tmp_path, "p"))
    store.store(_credentials())
    updated = store.update("acct-1", access_token="new-access", expires_at=1.5)
    assert updated.access_token == "new-access"
    assert updated.expires_at == 1.5
    assert store.load("acct-1").refresh_token == "refresh-secret"


def test_store_update_rejects_unknown_field(tmp_path: Path) -> None:
    store = CredentialStore(EncryptedFileBackend(tmp_path, "p"))
    store.store(_credentials())
    try:
        store.update("acct-1", nonsense="x")
        raise AssertionError("unknown field must be rejected")
    except CredentialStoreError:
        pass


def test_store_delete_and_missing(tmp_path: Path) -> None:
    store = CredentialStore(EncryptedFileBackend(tmp_path, "p"))
    assert store.load("nope") is None
    assert store.delete("nope") is False
    store.store(_credentials("gone"))
    assert store.delete("gone") is True
    assert store.load("gone") is None


def test_refusing_backend_never_stores() -> None:
    store = CredentialStore(RefusingBackend())
    try:
        store.store(_credentials())
        raise AssertionError("refusing backend must not store credentials")
    except CredentialStoreError as exc:
        assert "no secure credential storage" in str(exc)


def test_open_prefers_keyring_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeKeyringModule:
        class backends:
            class fail:
                class Keyring:
                    pass

        @staticmethod
        def get_keyring() -> object:
            return object()

        @staticmethod
        def set_password(service: str, user: str, value: str) -> None:
            FakeKeyringModule.saved[(service, user)] = value

        @staticmethod
        def get_password(service: str, user: str) -> str | None:
            return FakeKeyringModule.saved.get((service, user))

        @staticmethod
        def delete_password(service: str, user: str) -> None:
            FakeKeyringModule.saved.pop((service, user), None)

        saved: dict[tuple[str, str], str] = {}

    import sys

    monkeypatch.setitem(sys.modules, "keyring", FakeKeyringModule)  # type: ignore[arg-type]
    store = CredentialStore.open(tmp_path)
    assert store.backend_name == "keyring"
    store.store(_credentials("keyring-acct"))
    loaded = store.load("keyring-acct")
    assert loaded is not None and loaded.refresh_token == "refresh-secret"
    assert store.delete("keyring-acct") is True


def test_open_uses_passphrase_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PASSPHRASE_ENV_VAR, "env-passphrase")
    monkeypatch.setattr(
        "eurus.mcp.auth.token_store.KeyringBackend.available",
        classmethod(lambda cls: False),
    )
    store = CredentialStore.open(tmp_path, allow_prompt=False)
    assert store.backend_name == "encrypted-file"
    store.store(_credentials("env-acct"))
    assert store.load("env-acct") is not None


def test_open_refuses_without_any_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PASSPHRASE_ENV_VAR, raising=False)
    monkeypatch.setattr(
        "eurus.mcp.auth.token_store.KeyringBackend.available",
        classmethod(lambda cls: False),
    )
    monkeypatch.setattr(
        "eurus.mcp.auth.token_store.EncryptedFileBackend.available",
        classmethod(lambda cls: False),
    )
    store = CredentialStore.open(tmp_path, allow_prompt=False)
    assert store.backend_name == "unavailable"
    try:
        store.store(_credentials())
        raise AssertionError("must refuse")
    except CredentialStoreError:
        pass


def test_payload_rejects_unexpected_fields() -> None:
    import json

    payload = json.dumps({"account_id": "a", "evil": True})
    try:
        StoredCredentials.from_payload(payload)
        raise AssertionError("must reject unknown fields")
    except CredentialStoreError:
        pass


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def test_account_id_is_stable_and_sanitized() -> None:
    first = account_id_for_email("Alice.Example+Tag@Gmail.com")
    second = account_id_for_email("alice.example+tag@gmail.com")
    assert first == second
    assert first.replace("-", "").replace("_", "").isalnum()
    assert len(first) <= 48


def test_account_manager_add_list_remove(tmp_path: Path) -> None:
    manager = GoogleAccountManager(tmp_path / "accounts.json")
    assert manager.list() == []
    account = manager.add("alice@example.com")
    assert account.email == "alice@example.com"
    manager.add("bob@example.com")
    assert [item.email for item in manager.list()] == [
        "alice@example.com",
        "bob@example.com",
    ]
    assert manager.active is not None
    assert manager.active.email == "alice@example.com"


def test_account_manager_set_active_and_reload(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    manager = GoogleAccountManager(path)
    alice = manager.add("alice@example.com")
    manager.add("bob@example.com")
    manager.set_active(account_id_for_email("bob@example.com"))
    reloaded = GoogleAccountManager(path)
    assert reloaded.active is not None
    assert reloaded.active.email == "bob@example.com"
    try:
        reloaded.set_active("unknown")
        raise AssertionError("unknown account must raise")
    except ValueError:
        pass
    del alice


def test_account_manager_remove_updates_active(tmp_path: Path) -> None:
    manager = GoogleAccountManager(tmp_path / "accounts.json")
    manager.add("alice@example.com")
    bob = manager.add("bob@example.com")
    manager.set_active(bob.account_id)
    assert manager.remove(bob.account_id) is True
    assert manager.active is not None
    assert manager.active.email == "alice@example.com"
    assert manager.remove("ghost") is False


def test_account_manager_status_tracking(tmp_path: Path) -> None:
    manager = GoogleAccountManager(tmp_path / "accounts.json")
    account = manager.add("alice@example.com")
    assert manager.status_of(account.account_id) == STATUS_LOGGED_OUT
    manager.set_status(account.account_id, "connected")
    assert manager.status_of(account.account_id) == "connected"
