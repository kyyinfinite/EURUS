from __future__ import annotations

import os
import platform as platform_module
import shutil
from pathlib import Path

from eurus.bootstrap.models import PlatformInfo
from eurus.termux import is_termux

OS_RELEASE = Path("/etc/os-release")
PROC_VERSION = Path("/proc/version")
DOCKER_MARKERS = ("/.dockerenv", "/run/.containerenv")
CGROUP_FILE = Path("/proc/self/cgroup")
SYSTEMD_RUN_DIR = Path("/run/systemd/system")

CONTAINER_HINTS = ("docker", "lxc", "kubepods", "containerd", "podman")

_DISTRO_ALIASES = {
    "ubuntu": "Ubuntu",
    "debian": "Debian",
    "fedora": "Fedora",
    "almalinux": "AlmaLinux",
    "rocky": "Rocky Linux",
    "rhel": "RHEL",
    "centos": "CentOS",
    "arch": "Arch Linux",
    "archarm": "Arch Linux ARM",
    "alpine": "Alpine Linux",
    "opensuse-leap": "openSUSE Leap",
    "opensuse-tumbleweed": "openSUSE Tumbleweed",
    "opensuse": "openSUSE",
    "sles": "SLES",
    "kali": "Kali Linux",
    "raspbian": "Raspbian",
    "amzn": "Amazon Linux",
    "manjaro": "Manjaro",
}


def read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = OS_RELEASE.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep:
            values[name.strip()] = value.strip().strip('"')
    return values


def proc_version_text() -> str:
    try:
        return PROC_VERSION.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def android_host_signature() -> bool:
    text = proc_version_text().lower()
    return "android" in text or "linaro" in text and "aarch64" in text


def detect_proot(environ: dict[str, str]) -> bool:
    for name in ("PROOT_DISTRO_NAME", "PROOT_TMP_DIR", "PROOT_LOADER", "PROOT_NO_SECCOMP"):
        if environ.get(name, "").strip():
            return True
    return False


def detect_container() -> bool:
    for marker in DOCKER_MARKERS:
        if Path(marker).exists():
            return True
    try:
        cgroup = CGROUP_FILE.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return any(hint in cgroup for hint in CONTAINER_HINTS)


def detect_systemd(environ: dict[str, str]) -> bool:
    if SYSTEMD_RUN_DIR.exists():
        return True
    init_comm = Path("/proc/1/comm")
    try:
        return init_comm.read_text(encoding="utf-8", errors="replace").strip() == "systemd"
    except OSError:
        return False


def detect_termux_boot(environ: dict[str, str]) -> str:
    home = Path(environ.get("HOME", str(Path.home())))
    boot_dir = home / ".termux" / "boot"
    if boot_dir.is_dir():
        return "yes"
    prefix = environ.get("PREFIX", "")
    if prefix and (Path(prefix) / "etc" / "termux-boot").is_dir():
        return "yes"
    if is_termux(environ):
        return "unknown"
    return "no"


def detect_role(environ: dict[str, str]) -> str:
    if environ.get("DISPLAY", "").strip() or environ.get("WAYLAND_DISPLAY", "").strip():
        return "desktop"
    if shutil.which("sshd") or Path("/usr/sbin/sshd").exists() or Path("/etc/ssh").is_dir():
        return "server"
    return "unknown"


def classify_variant(
    *,
    termux: bool,
    proot: bool,
    container: bool,
    android_host: bool,
    distro_id: str,
) -> tuple[str, str]:
    if termux:
        return "termux", "high"
    if proot and android_host:
        return "proot", "high"
    if proot:
        return "proot", "medium"
    if android_host:
        return "proot", "medium"
    if container:
        return "container", "high"
    if distro_id in {
        "debian",
        "ubuntu",
        "fedora",
        "rhel",
        "rocky",
        "almalinux",
        "arch",
        "alpine",
        "opensuse-leap",
        "opensuse-tumbleweed",
    }:
        return "native", "high"
    return "native", "medium"


def inspect_platform(environ: dict[str, str] | None = None) -> PlatformInfo:
    env = dict(os.environ) if environ is None else dict(environ)
    release = read_os_release()
    distro_id = release.get("ID", "").lower()
    distro_name = _DISTRO_ALIASES.get(
        distro_id, release.get("NAME", "").strip() or platform_module.system()
    )
    distro_version = release.get("VERSION_ID", release.get("BUILD_ID", "")).strip()
    pretty = release.get("PRETTY_NAME", "").strip() or f"{distro_name} {distro_version}".strip()
    termux = is_termux(env)
    proot = detect_proot(env)
    container = detect_container()
    android = android_host_signature()
    systemd = detect_systemd(env)
    termux_boot = detect_termux_boot(env)
    role = detect_role(env)
    if termux:
        distro_id, distro_name = "termux", "Termux"
        if not distro_version:
            distro_version = env.get("TERMUX_VERSION", "")
    variant, confidence = classify_variant(
        termux=termux,
        proot=proot,
        container=container,
        android_host=android and not termux,
        distro_id=distro_id,
    )
    if termux:
        role = "mobile"
    return PlatformInfo(
        system=platform_module.system(),
        distro_id=distro_id,
        distro_name=distro_name,
        distro_version=distro_version,
        pretty_name=pretty,
        variant=variant,
        role=role,
        is_termux=termux,
        is_proot=variant == "proot",
        is_container=container,
        has_systemd=systemd,
        termux_boot=termux_boot,
        android_host=android,
        confidence=confidence,
    )


VARIANT_LABELS = {
    "termux": "Termux native",
    "proot": "Proot",
    "container": "Container",
    "native": "Native",
    "unknown": "Unknown",
}
