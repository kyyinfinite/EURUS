"""Secure storage for Google OAuth credentials.

Backends, in order of preference:

1. OS keyring - when the ``keyring`` package is installed and a real backend
   is available (Secret Service on Linux, Keychain on macOS, Credential
   Manager on Windows).
2. Encrypted file - when ``cryptography`` is installed. The encryption key is
   derived with PBKDF2-HMAC-SHA256 from a user-supplied passphrase that is
   never stored anywhere; only a random salt lives next to the ciphertext.

If neither backend is usable the store refuses to persist credentials. It
never silently downgrades to plaintext.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PASSPHRASE_ENV_VAR = "EURUS_GOOGLE_TOKEN_PASSPHRASE"
KEYRING_SERVICE = "eurus-google-oauth"
PBKDF2_ITERATIONS = 600_000

CREDENTIAL_FIELDS = (
    "account_id",
    "email",
    "client_id",
    "refresh_token",
    "access_token",
    "expires_at",
    "scope",
)


class CredentialStoreError(RuntimeError):
    pass


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(data, encoding="utf-8")
    with suppress(OSError):
        os.chmod(temporary, 0o600)
    temporary.replace(path)


class KeyringBackend:
    name = "keyring"

    def __init__(self) -> None:
        import keyring

        self._keyring = keyring

    @classmethod
    def available(cls) -> bool:
        try:
            import keyring
        except ImportError:
            return False
        try:
            backend = keyring.get_keyring()
        except Exception:
            return False
        return "fail" not in type(backend).__module__.lower() and not isinstance(
            backend, keyring.backends.fail.Keyring
        )

    def store(self, account_id: str, payload: str) -> None:
        self._keyring.set_password(KEYRING_SERVICE, account_id, payload)

    def load(self, account_id: str) -> str | None:
        try:
            value = self._keyring.get_password(KEYRING_SERVICE, account_id)
        except Exception:
            return None
        return value

    def delete(self, account_id: str) -> bool:
        try:
            self._keyring.delete_password(KEYRING_SERVICE, account_id)
        except Exception:
            return False
        return True

    def list_accounts(self) -> list[str]:
        try:
            # keyring has no portable enumeration; the account index lives in
            # accounts.json, so this is a probe of availability only.
            return []
        except Exception:
            return []


class EncryptedFileBackend:
    name = "encrypted-file"

    def __init__(self, directory: Path, passphrase: str) -> None:
        from cryptography.fernet import Fernet

        self._directory = directory
        self._salt_path = directory / ".salt"
        self._fernet = Fernet(self._derive_key(passphrase))

    @classmethod
    def available(cls) -> bool:
        try:
            import cryptography.fernet  # noqa: F401
        except ImportError:
            return False
        return True

    def _derive_key(self, passphrase: str) -> bytes:
        import base64
        import hashlib

        if not passphrase:
            raise CredentialStoreError(
                f"token encryption passphrase is empty; set {PASSPHRASE_ENV_VAR}"
            )
        if self._salt_path.exists():
            salt = self._salt_path.read_bytes()
        else:
            salt = os.urandom(16)
            self._salt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._salt_path.write_bytes(salt)
            with suppress(OSError):
                os.chmod(self._salt_path, 0o600)
        digest = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        return base64.urlsafe_b64encode(digest)

    def _token_path(self, account_id: str) -> Path:
        safe = "".join(
            character for character in account_id if character.isalnum() or character in "-_"
        )
        if not safe:
            raise CredentialStoreError("invalid account id")
        return self._directory / f"{safe}.token"

    def store(self, account_id: str, payload: str) -> None:
        encrypted = self._fernet.encrypt(payload.encode("utf-8")).decode("ascii")
        _atomic_write(self._token_path(account_id), encrypted)

    def load(self, account_id: str) -> str | None:
        path = self._token_path(account_id)
        if not path.exists():
            return None
        from cryptography.fernet import InvalidToken

        try:
            return self._fernet.decrypt(path.read_text(encoding="ascii").encode("ascii")).decode(
                "utf-8"
            )
        except InvalidToken as exc:
            raise CredentialStoreError(
                "cannot decrypt stored tokens: wrong passphrase or corrupted store"
            ) from exc

    def delete(self, account_id: str) -> bool:
        path = self._token_path(account_id)
        if not path.exists():
            return False
        path.unlink(missing_ok=True)
        return True

    def list_accounts(self) -> list[str]:
        if not self._directory.exists():
            return []
        return sorted(path.stem for path in self._directory.glob("*.token") if path.is_file())


class RefusingBackend:
    name = "unavailable"

    def store(self, account_id: str, payload: str) -> None:
        raise CredentialStoreError(
            "no secure credential storage available; install Eurus with the "
            "'google' extra (keyring + cryptography) or configure an OS keyring"
        )

    def load(self, account_id: str) -> str | None:
        return None

    def delete(self, account_id: str) -> bool:
        return False

    def list_accounts(self) -> list[str]:
        return []


@dataclass(frozen=True, slots=True)
class StoredCredentials:
    account_id: str
    email: str
    client_id: str
    refresh_token: str
    access_token: str
    expires_at: float
    scope: str

    def to_payload(self) -> str:
        return json.dumps(
            {
                "account_id": self.account_id,
                "email": self.email,
                "client_id": self.client_id,
                "refresh_token": self.refresh_token,
                "access_token": self.access_token,
                "expires_at": self.expires_at,
                "scope": self.scope,
            },
            indent=2,
        )

    @classmethod
    def from_payload(cls, payload: str) -> StoredCredentials:
        data = json.loads(payload)
        unknown = set(data) - set(CREDENTIAL_FIELDS)
        if unknown:
            raise CredentialStoreError(
                f"unexpected fields in stored credentials: {sorted(unknown)}"
            )
        return cls(
            account_id=str(data["account_id"]),
            email=str(data.get("email", "")),
            client_id=str(data.get("client_id", "")),
            refresh_token=str(data.get("refresh_token", "")),
            access_token=str(data.get("access_token", "")),
            expires_at=float(data.get("expires_at", 0.0)),
            scope=str(data.get("scope", "")),
        )


class CredentialStore:
    """Persistent credential storage with a strict no-plaintext guarantee."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    @property
    def backend_name(self) -> str:
        return str(getattr(self._backend, "name", "unknown"))

    @classmethod
    def open(
        cls,
        directory: Path,
        *,
        passphrase: str | None = None,
        prefer_keyring: bool = True,
        allow_prompt: bool = True,
    ) -> CredentialStore:
        if prefer_keyring and KeyringBackend.available():
            return cls(KeyringBackend())
        if EncryptedFileBackend.available():
            secret = (
                passphrase if passphrase is not None else os.environ.get(PASSPHRASE_ENV_VAR, "")
            )
            if not secret and allow_prompt:
                secret = _prompt_passphrase()
            if secret:
                return cls(EncryptedFileBackend(directory, secret))
        return cls(RefusingBackend())

    def store(self, credentials: StoredCredentials) -> None:
        self._backend.store(credentials.account_id, credentials.to_payload())

    def load(self, account_id: str) -> StoredCredentials | None:
        payload = self._backend.load(account_id)
        if payload is None:
            return None
        return StoredCredentials.from_payload(payload)

    def update(self, account_id: str, **changes: Any) -> StoredCredentials:
        current = self.load(account_id)
        if current is None:
            raise CredentialStoreError(f"no stored credentials for account '{account_id}'")
        data = {
            "account_id": current.account_id,
            "email": current.email,
            "client_id": current.client_id,
            "refresh_token": current.refresh_token,
            "access_token": current.access_token,
            "expires_at": current.expires_at,
            "scope": current.scope,
        }
        for key, value in changes.items():
            if key not in data:
                raise CredentialStoreError(f"unknown credential field: {key}")
            data[key] = value
        updated = StoredCredentials(**data)  # type: ignore[arg-type]
        self.store(updated)
        return updated

    def delete(self, account_id: str) -> bool:
        return bool(self._backend.delete(account_id))

    def list_accounts(self) -> list[str]:
        return list(self._backend.list_accounts())


def _prompt_passphrase() -> str:
    if not sys_stdin_is_tty():
        return ""
    import getpass

    try:
        return getpass.getpass("Google token encryption passphrase: ")
    except (EOFError, KeyboardInterrupt):
        return ""


def sys_stdin_is_tty() -> bool:
    import sys

    try:
        return sys.stdin.isatty()
    except Exception:
        return False
