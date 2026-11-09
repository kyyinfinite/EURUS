from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

READY = "READY"
MISSING = "MISSING"
BROKEN = "BROKEN"
OUTDATED = "OUTDATED"
OPTIONAL = "OPTIONAL"
BLOCKED = "BLOCKED"
UNKNOWN = "UNKNOWN"

STATUSES: tuple[str, ...] = (READY, MISSING, BROKEN, OUTDATED, OPTIONAL, BLOCKED, UNKNOWN)

MANDATORY_BAD = frozenset({MISSING, BROKEN, BLOCKED})


class ConfigUnset(Enum):
    UNSET = "unset"


CONFIG_UNSET: Final = ConfigUnset.UNSET


class BootstrapError(Exception):
    pass


class SkipStep(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PlatformInfo:
    system: str
    distro_id: str
    distro_name: str
    distro_version: str
    pretty_name: str
    variant: str
    role: str
    is_termux: bool
    is_proot: bool
    is_container: bool
    has_systemd: bool
    termux_boot: str
    android_host: bool
    confidence: str

    @property
    def label(self) -> str:
        base = self.distro_name or self.system or "unknown"
        version = f" {self.distro_version}" if self.distro_version else ""
        suffix = ""
        if self.variant == "termux":
            suffix = " / Termux native"
        elif self.variant == "proot":
            suffix = " / Proot"
        elif self.variant == "container":
            suffix = " / Container"
        elif self.variant == "native" and self.role == "server":
            suffix = " / VPS"
        return f"{base}{version}{suffix}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "distro_id": self.distro_id,
            "distro_name": self.distro_name,
            "distro_version": self.distro_version,
            "pretty_name": self.pretty_name,
            "variant": self.variant,
            "role": self.role,
            "is_termux": self.is_termux,
            "is_proot": self.is_proot,
            "is_container": self.is_container,
            "has_systemd": self.has_systemd,
            "termux_boot": self.termux_boot,
            "android_host": self.android_host,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class ArchitectureInfo:
    machine: str
    normalized: str
    bits: int
    ram_total_mb: int
    disk_free_gb: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "machine": self.machine,
            "normalized": self.normalized,
            "bits": self.bits,
            "ram_total_mb": self.ram_total_mb,
            "disk_free_gb": self.disk_free_gb,
        }


@dataclass(frozen=True, slots=True)
class PythonInfo:
    version: str
    major: int
    minor: int
    executable: str
    is_venv: bool
    venv_path: str
    pip_available: bool
    pip_version: str

    @property
    def supported(self) -> bool:
        return (self.major, self.minor) >= (3, 11)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "executable": self.executable,
            "is_venv": self.is_venv,
            "venv_path": self.venv_path,
            "pip_available": self.pip_available,
            "pip_version": self.pip_version,
        }


@dataclass(frozen=True, slots=True)
class PackageManagerInfo:
    name: str
    path: str
    is_root: bool

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": self.path, "is_root": self.is_root}


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    network: str
    compiler: str
    browser_backend: str
    chromium: str
    chromium_kind: str
    wheel_support: str
    details: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "network": self.network,
            "compiler": self.compiler,
            "browser_backend": self.browser_backend,
            "chromium": self.chromium,
            "chromium_kind": self.chromium_kind,
            "wheel_support": self.wheel_support,
            "details": dict(self.details),
        }


@dataclass(slots=True)
class CheckResult:
    name: str
    status: str
    detail: str = ""
    hint: str = ""
    optional: bool = False
    repair_key: str = ""
    repair_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def bad(self) -> bool:
        return not self.optional and self.status in MANDATORY_BAD

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "hint": self.hint,
            "optional": self.optional,
        }


@dataclass(slots=True)
class RepairAction:
    key: str
    title: str
    description: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    backup_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "kind": self.kind,
        }


@dataclass(slots=True)
class HealthReport:
    results: list[CheckResult]

    @property
    def summary(self) -> str:
        if any(result.bad for result in self.results):
            return "BLOCKED"
        if any(result.status == BROKEN for result in self.results):
            return "DEGRADED"
        if all(result.status in {READY, OPTIONAL} for result in self.results):
            return "READY"
        return "PARTIAL"

    def by_name(self, name: str) -> CheckResult | None:
        return next((item for item in self.results if item.name == name), None)

    def repairs(self) -> list[RepairAction]:
        from eurus.bootstrap.health.repair import actions_for

        return actions_for(self.results)

    def hints_text(self) -> str:
        lines = [
            f"{result.name}: {result.hint}"
            for result in self.results
            if result.hint and result.status not in {READY, OPTIONAL}
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "checks": [item.to_dict() for item in self.results]}


@dataclass(slots=True)
class ModuleResult:
    name: str
    status: str
    details: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    verification: str = ""


@dataclass(frozen=True, slots=True)
class PackageStatus:
    name: str
    dist_name: str
    required_spec: str
    installed_version: str
    status: str

    @property
    def ok(self) -> bool:
        return self.status == READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dist_name": self.dist_name,
            "required_spec": self.required_spec,
            "installed_version": self.installed_version,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class FeatureStatus:
    feature: str
    label: str
    status: str
    missing: tuple[str, ...]
    installed: tuple[str, ...]
    description: str = ""

    @property
    def ready(self) -> bool:
        return self.status == READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "label": self.label,
            "status": self.status,
            "missing": list(self.missing),
            "installed": list(self.installed),
        }


@dataclass(slots=True)
class InstallOutcome:
    success: bool
    targets: tuple[str, ...]
    returncode: int
    output_tail: str
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "targets": list(self.targets),
            "returncode": self.returncode,
            "error": self.error,
        }
