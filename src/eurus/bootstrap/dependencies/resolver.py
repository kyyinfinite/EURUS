from __future__ import annotations

from dataclasses import dataclass, field

from eurus.bootstrap.dependencies.catalog import FEATURES, _base_name
from eurus.bootstrap.models import PackageStatus


@dataclass(slots=True)
class ResolvePlan:
    targets: tuple[str, ...] = ()
    features: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    actions: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.targets

    def to_dict(self) -> dict[str, object]:
        return {
            "targets": list(self.targets),
            "features": list(self.features),
            "skipped": list(self.skipped),
            "notes": list(self.notes),
        }


def plan_for_features(
    feature_names: tuple[str, ...],
    packages: dict[str, PackageStatus],
) -> ResolvePlan:
    targets: list[str] = []
    skipped: list[str] = []
    for name in feature_names:
        spec = next((item for item in FEATURES if item.name == name), None)
        if spec is None:
            continue
        for requirement in spec.requirements:
            base = _base_name(requirement)
            status = packages.get(base)
            if status is not None and status.ok:
                skipped.append(requirement)
            else:
                targets.append(requirement)
    unique_targets: list[str] = []
    for item in targets:
        base = _base_name(item)
        if not any(_base_name(existing) == base for existing in unique_targets):
            unique_targets.append(item)
    return ResolvePlan(
        targets=tuple(unique_targets),
        features=tuple(feature_names),
        skipped=tuple(skipped),
    )


def plan_repair(packages: dict[str, PackageStatus]) -> ResolvePlan:
    targets: list[str] = []
    notes: list[str] = []
    for status in packages.values():
        if status.status in {"MISSING", "BROKEN"}:
            targets.append(status.required_spec)
        elif status.status == "OUTDATED":
            targets.append(status.required_spec)
            notes.append(f"{status.name}: installed {status.installed_version} is outdated")
    return ResolvePlan(targets=tuple(targets), notes=tuple(notes))


def plan_update(packages: dict[str, PackageStatus]) -> ResolvePlan:
    feature_dists: set[str] = set()
    for spec in FEATURES:
        for requirement in spec.requirements:
            feature_dists.add(_base_name(requirement))
    targets: list[str] = []
    for status in packages.values():
        if status.installed_version and status.name in feature_dists:
            targets.append(status.name)
    return ResolvePlan(
        targets=tuple(targets),
        notes=("upgrade installed feature packages to the latest compatible release",),
    )
