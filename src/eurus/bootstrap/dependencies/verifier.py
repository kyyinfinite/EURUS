from __future__ import annotations

from eurus.bootstrap.dependencies.catalog import FEATURES, feature_status
from eurus.bootstrap.dependencies.inspector import inspect_core, inspect_features
from eurus.bootstrap.models import READY, FeatureStatus, InstallOutcome, PackageStatus


def verify_core() -> dict[str, PackageStatus]:
    return inspect_core()


def verify_features() -> list[FeatureStatus]:
    packages = inspect_features()
    return [feature_status(spec, packages) for spec in FEATURES]


def verify_install(outcome: InstallOutcome) -> tuple[bool, list[FeatureStatus]]:
    if not outcome.success:
        return False, verify_features()
    statuses = verify_features()
    installed_names = {_base_name_of(target) for target in outcome.targets}
    for feature in statuses:
        if any(_base_name_of(item) in installed_names for item in feature.missing):
            continue
    return True, statuses


def _base_name_of(requirement: str) -> str:
    for separator in (">", "<", "=", "!", ";", "["):
        index = requirement.find(separator)
        if index != -1:
            requirement = requirement[:index]
    return requirement.strip()


def summarize_features(statuses: list[FeatureStatus]) -> str:
    ready = sum(1 for item in statuses if item.status == READY)
    return f"{ready}/{len(statuses)} features ready"
