from __future__ import annotations

import asyncio
import json
import re
import shutil
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from eurus.errors import ToolError

IMAGE_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9_.-]{1,128})?(@sha256:[0-9a-f]{64})?$"
)
LABEL_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9_.-]")
PS_FORMAT = "{{.ID}}\t{{.Labels}}"
MANAGED_LABEL = "eurus.managed"
MANAGED_LABEL_FILTER = "label=eurus.managed=true"


class DockerSecurityError(ToolError):
    def __init__(self, message: str, *, code: str = "docker_security") -> None:
        super().__init__(message, code=code)


@dataclass(frozen=True, slots=True)
class RuntimeInfo:
    runtime: str
    server_version: str
    rootless: bool
    security_options: tuple[str, ...]


def _reference_name(image: str) -> str:
    return image.split("@", 1)[0]


def validate_image_reference(
    image: str,
    *,
    allowlist: tuple[str, ...] | list[str] = (),
    digest_required: bool = False,
) -> str:
    candidate = image.strip()
    if not candidate:
        raise DockerSecurityError("sandbox image must not be empty")
    if any(character.isspace() for character in candidate) or candidate.startswith("-"):
        raise DockerSecurityError(f"unsafe sandbox image reference: {image!r}")
    if not IMAGE_PATTERN.match(candidate):
        raise DockerSecurityError(f"invalid sandbox image reference: {image!r}")
    if digest_required and "@sha256:" not in candidate:
        raise DockerSecurityError(
            f"sandbox image '{candidate}' must be pinned by digest (name@sha256:...)"
        )
    entries = tuple(entry.strip() for entry in allowlist if entry.strip())
    if entries:
        for entry in entries:
            if not IMAGE_PATTERN.match(entry):
                raise DockerSecurityError(f"invalid allowlist entry: {entry!r}")
        name = _reference_name(candidate)
        matched = False
        for entry in entries:
            entry_name = _reference_name(entry)
            if "@" in entry:
                matched = matched or candidate == entry
            else:
                matched = matched or name == entry_name or candidate == entry
        if not matched:
            raise DockerSecurityError(
                f"sandbox image '{candidate}' is not in the configured allowlist"
            )
    return candidate


def sanitize_label_value(value: str) -> str:
    cleaned = LABEL_SAFE_PATTERN.sub("-", value).strip("-.")
    return cleaned[:63] if cleaned else "unknown"


@dataclass(frozen=True, slots=True)
class ManagedContainer:
    container_id: str
    labels: dict[str, str]


def _parse_ps_output(output: str) -> list[ManagedContainer]:
    containers: list[ManagedContainer] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        container_id = parts[0].strip()
        if not container_id:
            continue
        labels: dict[str, str] = {}
        if len(parts) > 1:
            for item in parts[1].split(","):
                key, sep, value = item.partition("=")
                if sep:
                    labels[key.strip()] = value.strip()
        containers.append(ManagedContainer(container_id=container_id, labels=labels))
    return containers


async def probe_runtime(runtime: str = "docker", *, timeout_seconds: float = 10.0) -> RuntimeInfo:
    if not runtime or shutil.which(runtime) is None:
        raise DockerSecurityError(
            f"sandbox runtime '{runtime}' is unavailable", code="sandbox_unavailable"
        )
    try:
        process = await asyncio.create_subprocess_exec(
            runtime,
            "info",
            "--format",
            "{{json .}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except (OSError, TimeoutError) as exc:
        raise DockerSecurityError(
            f"sandbox runtime '{runtime}' probe failed: {type(exc).__name__}",
            code="sandbox_unavailable",
        ) from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[:200]
        raise DockerSecurityError(
            f"sandbox runtime '{runtime}' is unhealthy: {detail or 'info failed'}",
            code="sandbox_unavailable",
        )
    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise DockerSecurityError(
            f"sandbox runtime '{runtime}' returned unreadable info output",
            code="sandbox_unavailable",
        ) from exc
    if not isinstance(payload, dict):
        raise DockerSecurityError(
            f"sandbox runtime '{runtime}' returned unexpected info output",
            code="sandbox_unavailable",
        )
    options_raw = payload.get("SecurityOptions", [])
    options = (
        tuple(str(item) for item in options_raw if isinstance(item, str))
        if isinstance(options_raw, list)
        else ()
    )
    version = payload.get("ServerVersion", "")
    return RuntimeInfo(
        runtime=runtime,
        server_version=str(version),
        rootless=any("rootless" in option.lower() for option in options),
        security_options=options,
    )


async def sweep_orphans(
    runtime: str = "docker",
    *,
    run_id: str | None = None,
    timeout_seconds: float = 30.0,
) -> list[str]:
    if shutil.which(runtime) is None:
        return []
    try:
        listing = await asyncio.create_subprocess_exec(
            runtime,
            "ps",
            "-a",
            "--filter",
            MANAGED_LABEL_FILTER,
            "--format",
            PS_FORMAT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(listing.communicate(), timeout=timeout_seconds)
    except (OSError, TimeoutError):
        return []
    if listing.returncode != 0:
        return []
    containers = _parse_ps_output(stdout.decode("utf-8", errors="replace"))
    targets = [
        container
        for container in containers
        if run_id is None or container.labels.get("eurus.run_id") == run_id
    ]
    removed: list[str] = []
    for container in targets:
        try:
            remover = await asyncio.create_subprocess_exec(
                runtime,
                "rm",
                "-f",
                container.container_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(remover.wait(), timeout=timeout_seconds)
        except (OSError, TimeoutError):
            continue
        if remover.returncode == 0:
            removed.append(container.container_id)
    return removed


def default_seccomp_profile_path() -> Path:
    return Path(__file__).with_name("seccomp-default.json")


def resolve_seccomp_profile(setting: str, *, system_dir: Path) -> str:
    if not setting:
        return ""
    if setting == "default":
        target_dir = system_dir / "docker"
        target_dir.mkdir(parents=True, exist_ok=True)
        with suppress(OSError):
            system_dir.chmod(0o700)
            target_dir.chmod(0o700)
        target = target_dir / "seccomp-default.json"
        source = default_seccomp_profile_path()
        payload = source.read_bytes()
        if not target.exists() or target.read_bytes() != payload:
            target.write_bytes(payload)
        target.chmod(0o600)
        return str(target)
    candidate = Path(setting).expanduser()
    if not candidate.is_file():
        raise DockerSecurityError(f"seccomp profile does not exist: {setting}")
    return str(candidate)
