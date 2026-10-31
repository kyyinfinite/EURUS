from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from eurus import __version__
from eurus.errors import EurusError

fcntl: ModuleType | None
try:
    import fcntl as _fcntl_module
except ImportError:
    fcntl = None
else:
    fcntl = _fcntl_module

HOME_DIR_NAME = ".eurus"
CONFIG_FILE_NAME = "config.toml"
DATABASE_FILE_NAME = "eurus.sqlite3"
LEGACY_DATABASE_FILE_NAME = "sessions.sqlite3"
ADOPTION_MARKER_NAME = "ADOPTION.json"
ADOPTION_LOCK_NAME = ".adopt.lock"
HOME_LAYOUT_VERSION = 1
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class HomeError(EurusError):
    pass


def resolve_home(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    custom = env.get("EURUS_HOME", "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path.home() / HOME_DIR_NAME


def home_manager(environ: Mapping[str, str] | None = None) -> HomeManager:
    return HomeManager(environ)


class HomeManager:
    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        root: Path | None = None,
    ) -> None:
        self._environ = environ
        self._root = root.resolve() if root is not None else None

    @property
    def root(self) -> Path:
        if self._root is None:
            self._root = resolve_home(self._environ)
        return self._root

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILE_NAME

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def database_file(self) -> Path:
        return self.data_dir / DATABASE_FILE_NAME

    @property
    def legacy_database_file(self) -> Path:
        return self.root / LEGACY_DATABASE_FILE_NAME

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def secrets_dir(self) -> Path:
        return self.root / "secrets"

    @property
    def skills_dir(self) -> Path:
        return self.root / "skills"

    @property
    def skills_quarantine_dir(self) -> Path:
        return self.skills_dir / "quarantine"

    @property
    def skills_manifests_dir(self) -> Path:
        return self.skills_dir / "manifests"

    @property
    def browser_dir(self) -> Path:
        return self.root / "browser"

    @property
    def browser_profile_dir(self) -> Path:
        return self.browser_dir / "profile"

    @property
    def browser_downloads_dir(self) -> Path:
        return self.browser_dir / "downloads"

    @property
    def browser_cache_dir(self) -> Path:
        return self.browser_dir / "cache"

    @property
    def media_dir(self) -> Path:
        return self.root / "media"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def undo_dir(self) -> Path:
        return self.root / "undo"

    @property
    def evals_dir(self) -> Path:
        return self.root / "evals"

    @property
    def themes_dir(self) -> Path:
        return self.root / "themes"

    @property
    def bootstrap_dir(self) -> Path:
        return self.root / "bootstrap"

    @property
    def auth_dir(self) -> Path:
        return self.root / "auth"

    @property
    def security_dir(self) -> Path:
        return self.root / "security"

    @property
    def workspace_dir(self) -> Path:
        return self.root / "workspace"

    @property
    def system_dir(self) -> Path:
        return self.root / "system"

    @property
    def version_file(self) -> Path:
        return self.system_dir / "version"

    @property
    def manifest_path(self) -> Path:
        return self.system_dir / "manifest.json"

    @property
    def mcp_config_path(self) -> Path:
        return self.root / "mcp.json"

    @property
    def gateway_credentials_path(self) -> Path:
        return self.root / "gateway-web.json"

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    @property
    def ui_prefs_path(self) -> Path:
        return self.root / "ui.json"

    @property
    def tui_config_path(self) -> Path:
        return self.root / "tui.toml"

    @property
    def prices_path(self) -> Path:
        return self.root / "prices.toml"

    def ensure_dir(self, path: Path, *, mode: int = PRIVATE_DIR_MODE) -> Path:
        if path.exists():
            if not path.is_dir():
                raise HomeError(f"expected a directory at {path}")
            self._tighten(path, mode)
            return path
        try:
            path.mkdir(parents=True, exist_ok=True, mode=mode)
        except OSError as exc:
            raise HomeError(f"cannot create directory {path}: {exc}") from exc
        self._tighten(path, mode)
        return path

    def ensure_layout(self) -> None:
        self.ensure_dir(self.root)
        self.ensure_dir(self.data_dir)
        self.ensure_dir(self.system_dir)
        self._write_version_marker()
        self._write_manifest()
        for directory in (
            self.secrets_dir,
            self.skills_dir,
            self.browser_dir,
            self.media_dir,
            self.logs_dir,
            self.undo_dir,
            self.evals_dir,
            self.themes_dir,
            self.bootstrap_dir,
            self.auth_dir,
            self.security_dir,
            self.workspace_dir,
        ):
            if directory.is_dir():
                self._tighten(directory, PRIVATE_DIR_MODE)

    def database_path(self) -> Path:
        self.ensure_dir(self.data_dir)
        target = self.database_file
        if target.exists():
            return target
        legacy = self.legacy_database_file
        if not legacy.is_file():
            return target
        self._adopt_legacy_database(legacy, target)
        return target

    def _tighten(self, path: Path, mode: int) -> None:
        with suppress(OSError):
            current = stat.S_IMODE(path.stat().st_mode)
            if current != mode:
                os.chmod(path, mode)

    def _write_version_marker(self) -> None:
        marker = self.version_file
        if marker.is_file():
            with suppress(OSError):
                if marker.read_text(encoding="utf-8").strip() == __version__:
                    return
        self._atomic_write(marker, __version__ + "\n")

    def _write_manifest(self) -> None:
        existing: dict[str, Any] = {}
        if self.manifest_path.is_file():
            with suppress(OSError, ValueError):
                loaded = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
        if (
            existing.get("version") == __version__
            and existing.get("home_layout") == HOME_LAYOUT_VERSION
            and "created_at" in existing
        ):
            return
        payload = {
            "version": __version__,
            "created_at": _existing_created_at(existing),
            "home_layout": HOME_LAYOUT_VERSION,
        }
        self._atomic_write(self.manifest_path, json.dumps(payload, indent=2) + "\n")

    def _atomic_write(self, path: Path, content: str, *, mode: int = PRIVATE_FILE_MODE) -> None:
        self.ensure_dir(path.parent)
        try:
            descriptor, staged = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
        except OSError as exc:
            raise HomeError(f"cannot stage write for {path}: {exc}") from exc
        temporary = Path(staged)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
            self._fsync_dir(path.parent)
        except OSError as exc:
            with suppress(OSError):
                temporary.unlink()
            raise HomeError(f"cannot write {path}: {exc}") from exc

    def _fsync_dir(self, path: Path) -> None:
        with suppress(OSError):
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @contextmanager
    def _adoption_lock(self, lock_path: Path) -> Iterator[None]:
        if fcntl is None:
            yield
            return
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, PRIVATE_FILE_MODE)
        except OSError as exc:
            raise HomeError(f"cannot open adoption lock {lock_path}: {exc}") from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _checkpoint_wal(self, legacy: Path) -> None:
        try:
            connection = sqlite3.connect(str(legacy), timeout=10.0)
        except sqlite3.Error as exc:
            raise HomeError(f"cannot open legacy database {legacy}: {exc}") from exc
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            raise HomeError(f"cannot checkpoint legacy database {legacy}: {exc}") from exc
        finally:
            with suppress(sqlite3.Error):
                connection.close()

    def _verify_copy(self, copy_path: Path) -> None:
        try:
            connection = sqlite3.connect(str(copy_path), timeout=10.0)
        except sqlite3.Error as exc:
            raise HomeError(f"adopted copy {copy_path} cannot be opened: {exc}") from exc
        try:
            result = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        except sqlite3.Error as exc:
            raise HomeError(f"adopted copy {copy_path} failed verification: {exc}") from exc
        finally:
            with suppress(sqlite3.Error):
                connection.close()
        if result != "ok":
            raise HomeError(f"adopted copy {copy_path} failed verification: {result}")

    def _adopt_legacy_database(self, legacy: Path, target: Path) -> None:
        with self._adoption_lock(self.data_dir / ADOPTION_LOCK_NAME):
            if target.exists():
                return
            self._checkpoint_wal(legacy)
            temporary = self.data_dir / f".{DATABASE_FILE_NAME}.adopt-{os.getpid()}.tmp"
            try:
                shutil.copyfile(legacy, temporary)
                os.chmod(temporary, PRIVATE_FILE_MODE)
                self._verify_copy(temporary)
                self._fsync_dir(self.data_dir)
                if target.exists():
                    return
                os.replace(temporary, target)
                self._fsync_dir(self.data_dir)
            except (OSError, sqlite3.Error) as exc:
                with suppress(OSError):
                    temporary.unlink()
                raise HomeError(f"cannot adopt legacy database {legacy}: {exc}") from exc
            finally:
                with suppress(OSError):
                    temporary.unlink()
            self._write_adoption_marker(legacy)

    def _write_adoption_marker(self, legacy: Path) -> None:
        payload = {
            "migrated_from": str(legacy),
            "adopted_at": _utc_now(),
            "version": __version__,
        }
        marker = json.dumps(payload, indent=2) + "\n"
        self._atomic_write(self.data_dir / ADOPTION_MARKER_NAME, marker)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _existing_created_at(existing: Mapping[str, Any]) -> str:
    value = existing.get("created_at")
    if isinstance(value, str) and value:
        return value
    return _utc_now()


__all__ = [
    "ADOPTION_MARKER_NAME",
    "CONFIG_FILE_NAME",
    "DATABASE_FILE_NAME",
    "HOME_DIR_NAME",
    "HOME_LAYOUT_VERSION",
    "LEGACY_DATABASE_FILE_NAME",
    "HomeError",
    "HomeManager",
    "home_manager",
    "resolve_home",
]
