from eurus.bootstrap.models import (
    BLOCKED,
    BROKEN,
    MANDATORY_BAD,
    MISSING,
    OPTIONAL,
    OUTDATED,
    READY,
    UNKNOWN,
    ArchitectureInfo,
    CapabilityReport,
    CheckResult,
    FeatureStatus,
    HealthReport,
    PackageStatus,
    PlatformInfo,
    PythonInfo,
    RepairAction,
)


def _result(name="demo", status=READY, optional=False):
    return CheckResult(name=name, status=status, detail="d", optional=optional)


def test_status_constants_are_canonical():
    assert {MISSING, BROKEN, BLOCKED} == MANDATORY_BAD
    assert set([READY, MISSING, BROKEN, OUTDATED, OPTIONAL, BLOCKED, UNKNOWN]) <= set(
        [READY, MISSING, BROKEN, OUTDATED, OPTIONAL, BLOCKED, UNKNOWN]
    )


def test_mandatory_bad_result_blocks_report():
    report = HealthReport(results=[_result(status=READY), _result(status=MISSING)])
    assert report.summary == "BLOCKED"


def test_optional_missing_does_not_block():
    report = HealthReport(results=[_result(status=READY), _result(status=MISSING, optional=True)])
    assert report.summary == "PARTIAL"


def test_broken_without_mandatory_bad_is_degraded():
    report = HealthReport(results=[_result(status=BROKEN, optional=True)])
    assert report.summary == "DEGRADED"


def test_all_ready_and_optional_is_ready():
    report = HealthReport(results=[_result(), _result(status=OPTIONAL, optional=True)])
    assert report.summary == "READY"


def test_bad_property_respects_optional_flag():
    assert _result(status=BLOCKED).bad is True
    assert _result(status=BLOCKED, optional=True).bad is False


def test_by_name_finds_first_match():
    report = HealthReport(results=[_result(name="a"), _result(name="b")])
    assert report.by_name("b").name == "b"
    assert report.by_name("missing") is None


def test_hints_text_skips_ready_results():
    good = _result(name="ok")
    bad = CheckResult(name="broken", status=BROKEN, detail="d", hint="fix me")
    report = HealthReport(results=[good, bad])
    assert report.hints_text() == "broken: fix me"


def test_check_result_to_dict_round_trip_fields():
    data = _result().to_dict()
    assert data["status"] == READY
    assert data["name"] == "demo"
    assert "repair_payload" not in data


def test_platform_label_variants():
    base = dict(
        system="Linux",
        distro_id="debian",
        distro_name="Debian",
        distro_version="13",
        pretty_name="Debian 13",
        role="server",
        is_termux=False,
        is_proot=False,
        is_container=False,
        has_systemd=True,
        termux_boot="no",
        android_host=False,
        confidence="high",
    )
    termux = PlatformInfo(variant="termux", **base)
    proot = PlatformInfo(variant="proot", **base)
    container = PlatformInfo(variant="container", **base)
    native = PlatformInfo(variant="native", **base)
    assert "Termux native" in termux.label
    assert "Proot" in proot.label
    assert "Container" in container.label
    assert "VPS" in native.label
    assert native.to_dict()["distro_id"] == "debian"


def test_python_supported_boundary():
    ok = PythonInfo(
        version="3.11.0",
        major=3,
        minor=11,
        executable="/usr/bin/python3",
        is_venv=False,
        venv_path="",
        pip_available=True,
        pip_version="24.0",
    )
    old = PythonInfo(
        version="3.10.4",
        major=3,
        minor=10,
        executable="/usr/bin/python3",
        is_venv=False,
        venv_path="",
        pip_available=True,
        pip_version="24.0",
    )
    assert ok.supported is True
    assert old.supported is False


def test_repair_action_to_dict():
    action = RepairAction(key="k", title="t", description="d", kind="config_init")
    assert action.to_dict() == {"key": "k", "title": "t", "description": "d", "kind": "config_init"}


def test_package_and_feature_status_helpers():
    package = PackageStatus(
        name="httpx",
        dist_name="httpx",
        required_spec="httpx>=0.27",
        installed_version="0.28.1",
        status=READY,
    )
    assert package.ok is True
    feature = FeatureStatus(
        feature="web", label="Web", status=READY, missing=(), installed=("ddgs>=9",)
    )
    assert feature.ready is True
    assert feature.to_dict()["installed"] == ["ddgs>=9"]


def test_architecture_and_capability_to_dict():
    arch = ArchitectureInfo(
        machine="x86_64", normalized="x86_64", bits=64, ram_total_mb=1024, disk_free_gb=1.5
    )
    assert arch.to_dict()["normalized"] == "x86_64"
    cap = CapabilityReport(
        network="yes",
        compiler="no",
        browser_backend="cdp",
        chromium="missing",
        chromium_kind="missing",
        wheel_support="yes",
    )
    assert cap.to_dict()["details"] == {}


def test_repairs_hook_imports_actions_for():
    from eurus.bootstrap.health.repair import actions_for

    report = HealthReport(results=[_result(status=READY)])
    assert report.repairs() == actions_for(report.results)
