from __future__ import annotations

from dataclasses import dataclass, field

MANAGER_ORDER: tuple[str, ...] = ("pkg", "apt", "dnf", "yum", "pacman", "apk", "zypper")

INSTALL_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "pkg": ("install",),
    "apt": ("install",),
    "dnf": ("install",),
    "yum": ("install",),
    "pacman": ("--needed", "-S"),
    "apk": ("add",),
    "zypper": ("install",),
}


@dataclass(frozen=True, slots=True)
class SystemPackage:
    key: str
    label: str
    description: str
    provides: tuple[str, ...]
    packages: dict[str, str]
    manual_hint: str = "install it with your system package manager"
    optional: bool = True
    extra_notes: dict[str, str] = field(default_factory=dict)

    def package_for(self, manager: str) -> str | None:
        return self.packages.get(manager)

    def note_for(self, manager: str) -> str:
        return self.extra_notes.get(manager, "")


SYSTEM_PACKAGES: tuple[SystemPackage, ...] = (
    SystemPackage(
        key="chromium",
        label="Chromium browser",
        description="local chromium executable for the browser automation backend",
        provides=("chromium", "chromium-browser", "google-chrome", "chrome"),
        packages={
            "pkg": "chromium",
            "apt": "chromium",
            "dnf": "chromium",
            "yum": "chromium",
            "pacman": "chromium",
            "apk": "chromium",
            "zypper": "chromium",
        },
        extra_notes={
            "pkg": "Termux needs the x11-repo repository first: pkg install x11-repo",
            "apt": "on Debian the package is chromium; on Ubuntu it may be chromium-browser (snap)",
        },
    ),
    SystemPackage(
        key="git",
        label="Git",
        description="version control used by the coding tools (optional)",
        provides=("git",),
        packages={
            "pkg": "git",
            "apt": "git",
            "dnf": "git",
            "yum": "git",
            "pacman": "git",
            "apk": "git",
            "zypper": "git",
        },
    ),
)

PACKAGE_BY_KEY = {item.key: item for item in SYSTEM_PACKAGES}


def catalog_keys() -> list[str]:
    return [item.key for item in SYSTEM_PACKAGES]
