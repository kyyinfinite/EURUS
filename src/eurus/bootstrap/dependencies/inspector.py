from __future__ import annotations

import importlib.metadata
import importlib.util
import re

from eurus.bootstrap.dependencies.catalog import (
    CORE_REQUIREMENTS,
    FEATURES,
    IMPORT_NAME_OVERRIDES,
    _base_name,
)
from eurus.bootstrap.models import BROKEN, MISSING, OUTDATED, READY, PackageStatus

_VERSION_PART = re.compile(r"^(\d+)(.*)$")
_SUFFIX_RANK = {
    "dev": -3,
    "a": -2,
    "alpha": -2,
    "b": -1,
    "beta": -1,
    "rc": 0,
    "c": 0,
    "": 1,
    "post": 2,
}


def version_key(version: str) -> tuple[object, ...]:
    parts: list[object] = []
    for chunk in re.split(r"[._\-+]", version.strip().lower()):
        match = _VERSION_PART.match(chunk)
        if not match:
            parts.append((1, 0, chunk))
            continue
        number = int(match.group(1))
        suffix = match.group(2).lower()
        rank = 1
        numeric = 0
        for token, value in _SUFFIX_RANK.items():
            if suffix.startswith(token) and token:
                rank = value
                tail = re.sub(r"[^0-9].*$", "", suffix[len(token) :])
                numeric = int(tail) if tail.isdigit() else 0
                break
        else:
            if suffix:
                digits = re.sub(r"[^0-9].*$", "", suffix)
                numeric = int(digits) if digits.isdigit() else 0
        parts.append((0, number, rank, numeric))
    return tuple(parts)


def _compare(left: str, right: str) -> int:
    key_left, key_right = version_key(left), version_key(right)
    if key_left < key_right:
        return -1
    if key_left > key_right:
        return 1
    return 0


_OPS = {
    ">=": lambda a, b: a >= 0,
    ">": lambda a, b: a > 0,
    "<=": lambda a, b: a <= 0,
    "<": lambda a, b: a < 0,
    "==": lambda a, b: a == 0,
    "!=": lambda a, b: a != 0,
    "~=": lambda a, b: a >= 0,
}


def satisfies(version: str, constraints: str) -> bool:
    text = constraints.strip()
    if not text:
        return True
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    for clause in text.split(","):
        clause = clause.strip()
        if not clause:
            continue
        match = re.match(r"(>=|<=|==|!=|~=|>|<)\s*(\S+)", clause)
        if match is None:
            continue
        operator, target = match.group(1), match.group(2)
        if operator == "~=":
            compatible = target.split(".")
            compatible = compatible[:-1] if len(compatible) > 1 else compatible
            upper = ".".join(
                str(int(part) + 1) if index == len(compatible) - 1 else part
                for index, part in enumerate(compatible)
            )
            if _compare(version, target) < 0 or _compare(version, upper) >= 0:
                return False
            continue
        if not _OPS[operator](_compare(version, target), True):
            return False
    return True


def _import_name(dist_name: str) -> str:
    return IMPORT_NAME_OVERRIDES.get(dist_name, dist_name.replace("-", "_")).split(".")[0]


def module_importable(dist_name: str) -> bool | None:
    name = _import_name(dist_name)
    if name in {"tzdata"}:
        return None
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def inspect_dist(dist_name: str, required_spec: str) -> PackageStatus:
    base = _base_name(required_spec) or dist_name
    constraints = required_spec[len(dist_name) :].strip()
    try:
        installed_version = importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        return PackageStatus(base, dist_name, required_spec, "", MISSING)
    if not satisfies(installed_version, constraints):
        return PackageStatus(base, dist_name, required_spec, installed_version, OUTDATED)
    if module_importable(dist_name) is False:
        return PackageStatus(base, dist_name, required_spec, installed_version, BROKEN)
    return PackageStatus(base, dist_name, required_spec, installed_version, READY)


def inspect_core() -> dict[str, PackageStatus]:
    packages: dict[str, PackageStatus] = {}
    for spec in CORE_REQUIREMENTS:
        status = inspect_dist(_base_name(spec), spec)
        packages[status.name] = status
    return packages


def inspect_features() -> dict[str, PackageStatus]:
    packages: dict[str, PackageStatus] = {}
    for feature in FEATURES:
        for spec in feature.requirements:
            base = _base_name(spec)
            if base in packages:
                continue
            status = inspect_dist(base, spec)
            packages[status.name] = status
    return packages


def inspect_all() -> dict[str, PackageStatus]:
    packages = inspect_core()
    packages.update(inspect_features())
    return packages


def broken_packages(packages: dict[str, PackageStatus]) -> list[PackageStatus]:
    return [item for item in packages.values() if item.status == BROKEN]
