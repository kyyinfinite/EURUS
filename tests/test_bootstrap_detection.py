from eurus.bootstrap.detection.architecture import (
    disk_free_gb,
    inspect_architecture,
    is_wheel_platform,
    memory_total_mb,
    normalize_machine,
)
from eurus.bootstrap.detection.capabilities import (
    browser_capability,
    cdp_endpoint_configured,
    chromium_snapshot,
    inspect_capabilities,
    inspect_environment,
    network_available,
    wheel_support,
)
from eurus.bootstrap.detection.package_manager import (
    chromium_install_hint,
    detect_package_manager,
    manager_prefix,
    needs_root,
)
from eurus.bootstrap.detection.platform import (
    classify_variant,
    detect_proot,
    detect_role,
    detect_termux_boot,
    inspect_platform,
    read_os_release,
)
from eurus.bootstrap.detection.python import detect_venv, inspect_python
from eurus.bootstrap.models import UNKNOWN


def test_normalize_machine_aliases():
    assert normalize_machine("AMD64") == "x86_64"
    assert normalize_machine("arm64") == "aarch64"
    assert normalize_machine("armv8l") == "armv7l"
    assert normalize_machine("i686") == "x86"
    assert normalize_machine("") == "unknown"
    assert normalize_machine("mystery") == "mystery"


def test_wheel_platform_classes():
    class FakeArch:
        def __init__(self, normalized):
            self.normalized = normalized

    assert is_wheel_platform(FakeArch("x86_64")) == "yes"
    assert is_wheel_platform(FakeArch("aarch64")) == "yes"
    assert is_wheel_platform(FakeArch("armv7l")) == "limited"
    assert is_wheel_platform(FakeArch("sparc")) == "unknown"


def test_memory_and_disk_are_non_negative():
    assert memory_total_mb() >= 0
    assert disk_free_gb() >= 0.0


def test_inspect_architecture_reports_this_machine():
    info = inspect_architecture()
    assert info.bits in (32, 64)
    assert info.normalized in {"x86_64", "aarch64", "armv7l", "arm", "x86", "riscv64"}


def test_classify_variant_matrix():
    assert classify_variant(
        termux=True, proot=False, container=False, android_host=False, distro_id="debian"
    ) == ("termux", "high")
    assert classify_variant(
        termux=False, proot=True, container=False, android_host=True, distro_id="debian"
    ) == ("proot", "high")
    assert classify_variant(
        termux=False, proot=True, container=False, android_host=False, distro_id="ubuntu"
    ) == ("proot", "medium")
    assert classify_variant(
        termux=False, proot=False, container=True, android_host=False, distro_id="ubuntu"
    ) == ("container", "high")
    assert classify_variant(
        termux=False, proot=False, container=False, android_host=False, distro_id="debian"
    ) == ("native", "high")
    assert classify_variant(
        termux=False, proot=False, container=False, android_host=False, distro_id="obscure"
    ) == ("native", "medium")


def test_detect_proot_env_markers(tmp_path):
    assert detect_proot({"PROOT_DISTRO_NAME": "ubuntu"}) is True
    assert detect_proot({"PROOT_TMP_DIR": "/tmp"}) is True
    assert detect_proot({"HOME": str(tmp_path)}) is False


def test_detect_termux_boot_variants(tmp_path):
    home = tmp_path / "home"
    (home / ".termux" / "boot").mkdir(parents=True)
    assert detect_termux_boot({"HOME": str(home)}) == "yes"
    prefix = tmp_path / "prefix"
    (prefix / "etc" / "termux-boot").mkdir(parents=True)
    assert detect_termux_boot({"HOME": str(tmp_path / "none"), "PREFIX": str(prefix)}) == "yes"
    assert (
        detect_termux_boot({"HOME": str(tmp_path / "none"), "TERMUX_VERSION": "0.118"}) == "unknown"
    )
    assert detect_termux_boot({"HOME": str(tmp_path / "none")}) == "no"


def test_detect_role_desktop_and_server(tmp_path, monkeypatch):
    assert detect_role({"DISPLAY": ":0"}) == "desktop"
    assert detect_role({"WAYLAND_DISPLAY": "wayland-0"}) == "desktop"
    import shutil as shutil_module

    monkeypatch.setattr(
        shutil_module, "which", lambda name: "/usr/sbin/sshd" if name == "sshd" else None
    )
    assert detect_role({}) == "server"


def test_read_os_release_parses_quotes(tmp_path, monkeypatch):
    import eurus.bootstrap.detection.platform as platform_module

    release = tmp_path / "os-release"
    release.write_text('ID=debian\nNAME="Debian GNU/Linux"\nVERSION_ID=13\n', encoding="utf-8")
    monkeypatch.setattr(platform_module, "OS_RELEASE", release)
    values = read_os_release()
    assert values["ID"] == "debian"
    assert values["NAME"] == "Debian GNU/Linux"
    assert values["VERSION_ID"] == "13"


def test_inspect_platform_on_this_host():
    info = inspect_platform({})
    assert info.system == "Linux"
    assert info.variant in {"native", "container", "proot", "termux"}
    assert info.confidence in {"high", "medium"}
    assert info.to_dict()["variant"] == info.variant


def test_inspect_platform_termux_env(tmp_path):
    environ = {
        "HOME": str(tmp_path),
        "TERMUX_VERSION": "0.118",
        "PREFIX": "/data/data/com.termux/files/usr",
    }
    info = inspect_platform(environ)
    assert info.is_termux is True
    assert info.variant == "termux"
    assert info.role == "mobile"
    assert info.distro_id == "termux"


def test_detect_venv_prefers_runtime_over_environ(tmp_path):
    import sys

    in_venv, path = detect_venv({})
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        assert in_venv is True
        assert path == sys.prefix
    else:
        assert in_venv is False
        assert detect_venv({"VIRTUAL_ENV": str(tmp_path)})[1] == str(tmp_path)


def test_inspect_python_reports_current_interpreter():
    info = inspect_python({})
    import sys

    assert info.executable == sys.executable
    assert (info.major, info.minor) >= (3, 11)
    assert info.supported is True


def test_detect_package_manager_on_this_host():
    manager = detect_package_manager({})
    assert manager.name in {"apt", "dnf", "pacman", "apk", "zypper", "pkg", ""}
    assert isinstance(manager.is_root, bool)
    assert manager.to_dict()["name"] == manager.name


def test_manager_prefix_sudo_handling():
    class FakeManager:
        name = "apt"
        path = "/usr/bin/apt"
        is_root = False

    sudo_prefix = manager_prefix(FakeManager(), sudo=True)
    assert sudo_prefix[-1] == "apt"
    assert sudo_prefix[-2].endswith("sudo")
    assert manager_prefix(FakeManager()) == ["apt"]


def test_needs_root_and_chromium_hint():
    class FakeManager:
        name = "apt"
        path = "/usr/bin/apt"
        is_root = False

    assert needs_root(FakeManager(), {}) is True
    root_manager = FakeManager()
    root_manager.is_root = True
    assert needs_root(root_manager, {}) is False
    hint = chromium_install_hint(FakeManager())
    assert "chromium" in hint.lower()


def test_network_available_honors_local_only():
    assert network_available({"EURUS_LOCAL_ONLY": "1"}) == "local-only"


def test_network_available_without_probe(monkeypatch):
    import eurus.bootstrap.detection.capabilities as capabilities_module

    monkeypatch.setattr(capabilities_module, "probe_tcp", lambda *a, **k: False)
    monkeypatch.setattr(capabilities_module, "probe_dns", lambda *a, **k: False)
    assert network_available({}) == "no"


def test_cdp_endpoint_configured():
    assert (
        cdp_endpoint_configured({"EURUS_CDP_ENDPOINT": "http://127.0.0.1:9222"})
        == "http://127.0.0.1:9222"
    )
    assert cdp_endpoint_configured({}) == ""


def test_chromium_snapshot_missing_for_bogus_path(tmp_path):
    found, kind = chromium_snapshot({"EURUS_CHROMIUM": str(tmp_path / "nope")})
    assert found == ""
    assert kind == "missing"


def test_browser_capability_without_chromium(tmp_path):
    path, kind, backend = browser_capability({"EURUS_CHROMIUM": str(tmp_path / "nope")})
    assert kind == "missing"
    assert path == ""
    assert backend == "none"


def test_inspect_capabilities_returns_report(tmp_path):
    report = inspect_capabilities(
        {"EURUS_CHROMIUM": str(tmp_path / "nope"), "EURUS_LOCAL_ONLY": "1"}
    )
    assert report.network == "local-only"
    assert report.chromium_kind == "missing"
    assert report.compiler in {"yes", "no", UNKNOWN}
    assert isinstance(report.details, dict)


def test_inspect_environment_wires_everything(tmp_path):
    report = inspect_environment(
        {
            "EURUS_HOME": str(tmp_path),
            "EURUS_CHROMIUM": str(tmp_path / "nope"),
            "EURUS_LOCAL_ONLY": "1",
        }
    )
    assert report.platform.system == "Linux"
    assert report.python.supported is True
    assert report.capabilities.chromium_kind == "missing"
    assert report.capabilities.network == "local-only"
    payload = report.to_dict()
    assert payload["platform"]["variant"] == report.platform.variant
    assert payload["architecture"]["bits"] in (32, 64)


def test_wheel_support_delegates():
    class FakeArch:
        normalized = "armv7l"

    assert wheel_support(FakeArch()) == "limited"
