from eurus.bootstrap.dependencies.catalog import (
    CORE_DIST_NAMES,
    CORE_REQUIREMENTS,
    FEATURES,
    _base_name,
    feature_by_name,
    feature_for_extra,
    feature_status,
    requirements_for,
)
from eurus.bootstrap.dependencies.inspector import (
    broken_packages,
    inspect_core,
    inspect_dist,
    inspect_features,
    module_importable,
    satisfies,
    version_key,
)
from eurus.bootstrap.dependencies.resolver import plan_for_features, plan_update
from eurus.bootstrap.dependencies.verifier import (
    summarize_features,
    verify_core,
    verify_features,
    verify_install,
)
from eurus.bootstrap.models import (
    BROKEN,
    MISSING,
    OPTIONAL,
    OUTDATED,
    READY,
    InstallOutcome,
    PackageStatus,
)


def test_core_requirements_cover_core_dist_names():
    for spec in CORE_REQUIREMENTS:
        assert _base_name(spec) in CORE_DIST_NAMES


def test_base_name_strips_constraints():
    assert _base_name("httpx>=0.27,<1") == "httpx"
    assert _base_name("mcp>=1.9,<2") == "mcp"
    assert _base_name("python-docx>=1.1") == "python-docx"
    assert _base_name("ddgs[extra]>=1") == "ddgs"
    assert _base_name("tzdata; sys_platform == 'win32'") == "tzdata"
    assert _base_name("plain") == "plain"


def test_feature_catalog_matches_pyproject_extras():
    extras = {item.extra for item in FEATURES}
    assert {
        "web",
        "groq",
        "genai",
        "mcp",
        "telegram",
        "browser",
        "browser-cdp",
        "documents",
    } <= extras


def test_feature_for_extra_lookup():
    spec = feature_for_extra("browser")
    assert spec is not None
    assert spec.dist_names == ("playwright",)
    assert feature_for_extra("does-not-exist") is None


def test_feature_by_name_covers_all_features():
    for item in FEATURES:
        assert feature_by_name[item.name] is item


def test_version_key_orders_numerically():
    assert version_key("1.2.0") < version_key("1.10.0")
    assert version_key("1.9.9") < version_key("1.10.0")
    assert version_key("2.0.0") > version_key("1.99.99")
    assert version_key("1.0.0rc1") < version_key("1.0.0")
    assert version_key("1.0.0a1") < version_key("1.0.0rc1")
    assert version_key("1.0.0b2") < version_key("1.0.0rc1")
    assert version_key("1.0.0") == version_key("1.0.0")


def test_satisfies_operators():
    assert satisfies("1.5.0", ">=1.0") is True
    assert satisfies("0.9.0", ">=1.0") is False
    assert satisfies("1.5.0", "<2") is True
    assert satisfies("2.0.0", "<2") is False
    assert satisfies("1.5.0", "==1.5.0") is True
    assert satisfies("1.5.1", "!=1.5.1") is False
    assert satisfies("1.4.2", ">=1.0,<2") is True
    assert satisfies("2.1.0", ">=1.0,<2") is False
    assert satisfies("1.4.2", "~=1.4.1") is True
    assert satisfies("1.5.0", "~=1.4.1") is False
    assert satisfies("1.0.0", "(>=0.5)") is True
    assert satisfies("1.0.0", "") is True
    assert satisfies("1.0.0", "not-a-constraint") is True


def test_module_importable_overrides_import_names():
    assert module_importable("python-docx") is True
    assert module_importable("definitely-not-a-real-package-xyz") is False
    assert module_importable("tzdata") is None


def test_inspect_dist_reports_ready_for_installed_package():
    status = inspect_dist("httpx", "httpx>=0.27,<1")
    assert status.status == READY
    assert status.installed_version
    assert status.ok is True


def test_inspect_dist_reports_missing_for_unknown_package():
    status = inspect_dist(
        "definitely-not-a-real-package-xyz", "definitely-not-a-real-package-xyz>=1"
    )
    assert status.status == MISSING
    assert status.ok is False


def test_inspect_dist_reports_outdated():
    status = inspect_dist("httpx", "httpx>=999")
    assert status.status == OUTDATED


def test_inspect_core_all_ready_in_this_environment():
    packages = inspect_core()
    assert set(packages) == set(CORE_DIST_NAMES)
    for name, status in packages.items():
        assert status.status == READY, f"{name}: {status.status}"


def test_inspect_features_covers_all_feature_requirements():
    packages = inspect_features()
    assert "playwright" in packages
    assert "aiogram" in packages
    assert "python-docx" in packages


def test_feature_status_states():
    spec = feature_by_name["web"]
    ready = {
        "ddgs": PackageStatus("ddgs", "ddgs", "ddgs>=9", "9.0", READY),
        "trafilatura": PackageStatus("trafilatura", "trafilatura", "trafilatura>=2", "2.0", READY),
    }
    assert feature_status(spec, ready).status == READY
    partial = {**ready, "ddgs": PackageStatus("ddgs", "ddgs", "ddgs>=9", "", MISSING)}
    assert feature_status(spec, partial).status == MISSING
    assert feature_status(spec, {}).status == OPTIONAL


def test_requirements_for_dedupes_by_base_name():
    targets = requirements_for(("browser", "browser-cdp"))
    bases = [_base_name(item) for item in targets]
    assert len(bases) == len(set(bases))
    assert requirements_for(("unknown-feature",)) == []


def test_broken_packages_filters():
    packages = {
        "ok": PackageStatus("ok", "ok", "ok", "1", READY),
        "bad": PackageStatus("bad", "bad", "bad", "1", BROKEN),
    }
    assert [item.name for item in broken_packages(packages)] == ["bad"]


def test_plan_for_features_targets_missing_only():
    packages = {
        "playwright": PackageStatus("playwright", "playwright", "playwright>=1.50", "", MISSING),
        "websockets": PackageStatus("websockets", "websockets", "websockets>=12", "12.0", READY),
    }
    plan = plan_for_features(("browser-cdp",), packages)
    assert plan.empty is True
    plan2 = plan_for_features(("browser",), packages)
    assert plan2.targets == ("playwright>=1.50",)
    assert plan2.features == ("browser",)


def test_plan_for_features_ignores_unknown_names():
    plan = plan_for_features(("nope",), {})
    assert plan.empty is True
    assert plan.features == ("nope",)


def test_plan_for_features_records_satisfied_requirements():
    packages = {
        "websockets": PackageStatus("websockets", "websockets", "websockets>=12", "12.0", READY)
    }
    plan = plan_for_features(("browser-cdp",), packages)
    assert plan.skipped == ("websockets>=12",)


def test_plan_update_targets_installed_feature_packages():
    packages = {
        "ddgs": PackageStatus("ddgs", "ddgs", "ddgs>=9", "9.14.0", READY),
        "httpx": PackageStatus("httpx", "httpx", "httpx>=0.27", "0.28.1", READY),
    }
    plan = plan_update(packages)
    assert "ddgs" in plan.targets
    assert "httpx" not in plan.targets


def test_verify_core_matches_inspect():
    assert verify_core() == inspect_core()


def test_verify_features_returns_all_features():
    statuses = verify_features()
    assert {item.feature for item in statuses} == {item.name for item in FEATURES}


def test_verify_install_failure_reports_features():
    outcome = InstallOutcome(success=False, targets=("ddgs>=9",), returncode=1, output_tail="boom")
    ok, statuses = verify_install(outcome)
    assert ok is False
    assert len(statuses) == len(FEATURES)


def test_verify_install_success():
    outcome = InstallOutcome(
        success=True, targets=("websockets>=12",), returncode=0, output_tail=""
    )
    ok, statuses = verify_install(outcome)
    assert ok is True


def test_summarize_features_counts_ready():
    statuses = [
        feature_status(feature_by_name["web"], {}),
        feature_status(
            feature_by_name["web"],
            {
                "ddgs": PackageStatus("ddgs", "ddgs", "ddgs>=9", "9", READY),
                "trafilatura": PackageStatus(
                    "trafilatura", "trafilatura", "trafilatura>=2", "2", READY
                ),
            },
        ),
    ]
    assert summarize_features(statuses) == "1/2 features ready"


def test_inspect_dist_uses_base_name_for_spec():
    status = inspect_dist("python-docx", "python-docx>=1.1")
    assert status.name == "python-docx"
    assert status.dist_name == "python-docx"
