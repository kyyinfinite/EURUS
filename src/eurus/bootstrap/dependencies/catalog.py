from __future__ import annotations

from dataclasses import dataclass

from eurus.bootstrap.models import MISSING, OPTIONAL, READY, FeatureStatus, PackageStatus

CORE_REQUIREMENTS: tuple[str, ...] = (
    "httpx>=0.27,<1",
    "httpcore>=1.0,<2",
    "click>=8.1",
    "rich>=13.7",
    "pygments>=2.17",
    "prompt_toolkit>=3.0.43",
    "tzdata>=2025.2",
    "mcp>=1.27,<2",
)

CORE_DIST_NAMES: tuple[str, ...] = (
    "httpx",
    "httpcore",
    "click",
    "rich",
    "pygments",
    "prompt_toolkit",
    "tzdata",
    "mcp",
)


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    name: str
    label: str
    extra: str
    requirements: tuple[str, ...]
    dist_names: tuple[str, ...]
    description: str
    system_hint: str = ""


FEATURES: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        name="web",
        label="Web research",
        extra="web",
        requirements=("ddgs>=9.14.0", "trafilatura>=2.2.0"),
        dist_names=("ddgs", "trafilatura"),
        description="web_search, web_fetch and deep_search tools",
    ),
    FeatureSpec(
        name="groq",
        label="Groq SDK",
        extra="groq",
        requirements=("groq>=0.13",),
        dist_names=("groq",),
        description="Official Groq SDK protocol (groq-sdk provider)",
    ),
    FeatureSpec(
        name="genai",
        label="Google GenAI SDK",
        extra="genai",
        requirements=("google-genai>=1.0",),
        dist_names=("google-genai",),
        description="Official Google GenAI SDK protocol (gemini-sdk provider)",
    ),
    FeatureSpec(
        name="mcp",
        label="MCP support",
        extra="mcp",
        requirements=("mcp>=1.27,<2",),
        dist_names=("mcp",),
        description="Model Context Protocol client and server (installed with Eurus core)",
    ),
    FeatureSpec(
        name="google",
        label="Google secure storage",
        extra="google",
        requirements=("keyring>=25", "cryptography>=42"),
        dist_names=("keyring", "cryptography"),
        description="OS keyring and encrypted token storage for Google OAuth",
    ),
    FeatureSpec(
        name="telegram",
        label="Telegram gateway",
        extra="telegram",
        requirements=("aiogram>=3.29,<4",),
        dist_names=("aiogram",),
        description="EURUS gateway over a Telegram bot",
    ),
    FeatureSpec(
        name="browser",
        label="Browser automation (Playwright)",
        extra="browser",
        requirements=("playwright>=1.50",),
        dist_names=("playwright",),
        description="Playwright browser backend",
        system_hint="after install run: python -m playwright install chromium",
    ),
    FeatureSpec(
        name="browser-cdp",
        label="Browser CDP backend",
        extra="browser-cdp",
        requirements=("websockets>=12",),
        dist_names=("websockets",),
        description="Chromium DevTools Protocol backend (recommended on Termux)",
    ),
    FeatureSpec(
        name="documents",
        label="Office documents",
        extra="documents",
        requirements=("python-docx>=1.1", "openpyxl>=3.1", "python-pptx>=1.0"),
        dist_names=("python-docx", "openpyxl", "python-pptx"),
        description="DOCX, XLSX and PPTX creation and editing tools",
    ),
)

IMPORT_NAME_OVERRIDES = {
    "python-docx": "docx",
    "python-pptx": "pptx",
    "google-genai": "google.genai",
    "prompt_toolkit": "prompt_toolkit",
    "tzdata": "tzdata",
}

feature_by_name = {item.name: item for item in FEATURES}


def feature_for_extra(extra: str) -> FeatureSpec | None:
    return next((item for item in FEATURES if item.extra == extra), None)


def requirements_for(feature_names: tuple[str, ...]) -> list[str]:
    targets: list[str] = []
    for name in feature_names:
        spec = feature_by_name.get(name)
        if spec is not None:
            targets.extend(spec.requirements)
    unique: list[str] = []
    for item in targets:
        base = item.split(">")[0].split("<")[0].split("=")[0].strip()
        if not any(
            existing.split(">")[0].split("<")[0].split("=")[0].strip() == base
            for existing in unique
        ):
            unique.append(item)
    return unique


def _base_name(spec: str) -> str:
    for separator in (">", "<", "=", "!", ";", "["):
        index = spec.find(separator)
        if index != -1:
            spec = spec[:index]
    return spec.strip()


def feature_status(
    spec: FeatureSpec,
    packages: dict[str, PackageStatus],
) -> FeatureStatus:
    missing: list[str] = []
    installed: list[str] = []
    for item in spec.requirements:
        base = _base_name(item)
        entry = packages.get(base)
        if entry is not None and entry.ok:
            installed.append(item)
        else:
            missing.append(item)
    if not missing:
        status = READY
    elif installed:
        status = MISSING
    else:
        status = OPTIONAL
    return FeatureStatus(
        feature=spec.name,
        label=spec.label,
        status=status,
        missing=tuple(missing),
        installed=tuple(installed),
        description=spec.description,
    )


FEATURE_LABELS = {item.name: item.label for item in FEATURES}
