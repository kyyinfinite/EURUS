from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from eurus import paths
from eurus.catalog import PROTOCOLS, ProviderSpec, builtin_providers
from eurus.errors import ConfigError, ModelRefError
from eurus.models import ModelRef
from eurus.termux import is_termux

PERMISSION_MODES: tuple[str, ...] = ("ask", "auto-edit", "yolo")
SANDBOX_BACKENDS: tuple[str, ...] = ("container", "host")
LEARNING_MODES: tuple[str, ...] = ("off", "ask", "auto")
LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")
TOP_LEVEL_KEYS = frozenset(
    {
        "agent",
        "auxiliary",
        "permissions",
        "logging",
        "network",
        "tools",
        "providers",
        "sandbox",
        "learning",
        "memory",
        "verification",
        "planning",
        "pricing",
        "browser",
    }
)
PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
SANDBOX_USER_PATTERN = re.compile(r"^(auto|[a-z_][a-z0-9_-]{0,31}|\d{1,5}(:\d{1,5})?)$")
APPARMOR_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})

ENV_OVERRIDES: tuple[tuple[str, str, str, type[Any]], ...] = (
    ("EURUS_MODEL", "agent", "model", str),
    ("EURUS_SKILLS", "agent", "skills", list),
    ("EURUS_FALLBACK", "agent", "fallback", list),
    ("EURUS_MAX_STEPS", "agent", "max_steps", int),
    ("EURUS_TEMPERATURE", "agent", "temperature", float),
    ("EURUS_PERMISSION_MODE", "permissions", "mode", str),
    ("EURUS_WORKSPACE", "permissions", "workspace", str),
    ("EURUS_LOG_LEVEL", "logging", "level", str),
    ("EURUS_LOG_FILE", "logging", "file", str),
    ("EURUS_LOG_JSON", "logging", "json", bool),
    ("EURUS_TIMEOUT", "network", "timeout_seconds", float),
    ("EURUS_MAX_RETRIES", "network", "max_retries", int),
    ("EURUS_LOCAL_ONLY", "network", "local_only", bool),
    ("EURUS_COOLDOWN", "network", "cooldown_seconds", float),
    ("EURUS_MAX_COOLDOWN", "network", "max_cooldown_seconds", float),
    ("EURUS_RETRY_JITTER", "network", "retry_jitter", float),
    ("EURUS_FIRST_TOKEN", "network", "first_token_seconds", float),
)

SAMPLE_CONFIG = """[agent]
model = "groq/llama-3.3-70b-versatile"
fallback = ["openai/gpt-4o-mini"]
iteration_budget = 500
temperature = 0.2
system_prompt = ""
context_tokens = 32000
skills = []

[auxiliary]
model = ""
fallback = []

[learning]
mode = "ask"
lessons_in_prompt = 5
max_lessons = 500
# Draft reusable skills after completed tasks (LLM call after each answer).
skills = false

[permissions]
mode = "ask"
workspace = "."

[logging]
level = "INFO"
file = ""
json = false

[network]
timeout_seconds = 120.0
max_retries = 3
local_only = false
cooldown_seconds = 30.0
max_cooldown_seconds = 300.0
retry_jitter = 0.0
first_token_seconds = 0.0

[tools]
max_output_chars = 20000
timeout_seconds = 120.0
max_parallel_tools = 4

[sandbox]
backend = "container"
runtime = "docker"
image = "python:3.12-alpine"
required = true
network = false
memory_mb = 1024
cpu_limit = 1.0
pids_limit = 128
user = "auto"
image_allowlist = []
digest_pin_required = false
seccomp_profile = ""
apparmor_profile = ""
"""


TERMUX_SANDBOX = """[sandbox]
backend = "host"
required = false
"""


def sample_config(environ: Mapping[str, str] | None = None) -> str:
    if not is_termux(environ):
        return SAMPLE_CONFIG
    head, _, _ = SAMPLE_CONFIG.partition("[sandbox]")
    return head + TERMUX_SANDBOX


@dataclass(frozen=True, slots=True)
class AgentConfig:
    model: str = ""
    fallback: tuple[str, ...] = ()
    max_steps: int = 40
    iteration_budget: int = 500
    temperature: float = 0.2
    system_prompt: str = ""
    context_tokens: int = 32000
    skills: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AuxiliaryConfig:
    model: str = ""
    fallback: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PermissionConfig:
    mode: str = "ask"
    workspace: str = "."


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str = "INFO"
    file: str = ""
    json: bool = False


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    timeout_seconds: float = 120.0
    max_retries: int = 3
    local_only: bool = False
    cooldown_seconds: float = 30.0
    max_cooldown_seconds: float = 300.0
    retry_jitter: float = 0.0
    first_token_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ToolsConfig:
    max_output_chars: int = 20000
    timeout_seconds: float = 120.0
    max_parallel_tools: int = 4


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    backend: str = "container"
    runtime: str = "docker"
    image: str = "python:3.12-alpine"
    required: bool = True
    network: bool = False
    memory_mb: int = 1024
    cpu_limit: float = 1.0
    pids_limit: int = 128
    user: str = "auto"
    image_allowlist: tuple[str, ...] = ()
    digest_pin_required: bool = False
    seccomp_profile: str = ""
    apparmor_profile: str = ""


@dataclass(frozen=True, slots=True)
class LearningConfig:
    mode: str = "ask"
    lessons_in_prompt: int = 5
    max_lessons: int = 500
    skills: bool = False


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    enabled: bool = True
    max_memories: int = 2000
    retention_days: int = 0
    max_content_chars: int = 500


@dataclass(frozen=True, slots=True)
class VerificationConfig:
    enabled: bool = True
    model_judge: bool = False


@dataclass(frozen=True, slots=True)
class PlanningConfig:
    model_planning: bool = True
    max_steps: int = 8


@dataclass(frozen=True, slots=True)
class PricingConfig:
    updated_at: str = ""
    models: dict[str, dict[str, float | str]] = field(default_factory=dict)


BROWSER_BACKENDS = ("auto", "cdp", "playwright", "remote", "attach")


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    enabled: bool = False
    max_page_bytes: int = 2_000_000
    navigation_timeout_seconds: float = 30.0
    max_screenshot_width: int = 1280
    max_screenshot_height: int = 800
    backend: str = "auto"
    cdp_endpoint: str = ""
    chromium_executable: str = ""
    profile_dir: str = ""
    headless: bool = True
    interaction_timeout_seconds: float = 10.0
    startup_timeout_seconds: float = 25.0
    max_snapshot_tokens: int = 3000
    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()
    download_dir: str = ""
    max_download_mb: int = 50
    dns_cache_ttl_seconds: float = 60.0
    max_sessions: int = 3
    session_idle_ttl_seconds: float = 900.0


@dataclass(frozen=True, slots=True)
class Config:
    agent: AgentConfig = field(default_factory=AgentConfig)
    auxiliary: AuxiliaryConfig = field(default_factory=AuxiliaryConfig)
    permissions: PermissionConfig = field(default_factory=PermissionConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    planning: PlanningConfig = field(default_factory=PlanningConfig)
    pricing: PricingConfig = field(default_factory=PricingConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    providers: dict[str, ProviderSpec] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def deep_merge(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in extra.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = deep_merge(current, value)
        elif isinstance(value, Mapping):
            result[key] = deep_merge({}, value)
        else:
            result[key] = value
    return result


def read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc


def _coerce_env(name: str, text: str, kind: type[Any]) -> Any:
    cleaned = text.strip()
    if kind is list:
        return [part.strip() for part in cleaned.split(",") if part.strip()]
    if kind is bool:
        lowered = cleaned.lower()
        if lowered in TRUE_VALUES:
            return True
        if lowered in FALSE_VALUES:
            return False
        raise ConfigError(f"{name} has invalid value: {text!r}")
    try:
        return kind(cleaned)
    except ValueError as exc:
        raise ConfigError(f"{name} has invalid value: {text!r}") from exc


def env_overrides(environ: Mapping[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, section, key, kind in ENV_OVERRIDES:
        text = environ.get(name, "")
        if not text.strip():
            continue
        result.setdefault(section, {})[key] = _coerce_env(name, text, kind)
    return result


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, Mapping):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _check_keys(section: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(section) - allowed)
    if unknown:
        raise ConfigError(f"unknown key(s) in [{where}]: {', '.join(unknown)}")


def _get(section: Mapping[str, Any], key: str, kind: type[Any], default: Any, where: str) -> Any:
    if key not in section:
        return default
    value = section[key]
    is_bool = isinstance(value, bool)
    if kind is float and isinstance(value, int) and not is_bool:
        return float(value)
    if kind is not bool and is_bool:
        raise ConfigError(f"{where}.{key} must be {kind.__name__}")
    if not isinstance(value, kind):
        raise ConfigError(f"{where}.{key} must be {kind.__name__}")
    return value


def _str_list(section: Mapping[str, Any], key: str, where: str) -> list[str]:
    value = section.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where}.{key} must be a list of strings")
    return [item.strip() for item in value if item.strip()]


def _check_model(text: str, providers: Mapping[str, ProviderSpec], where: str) -> None:
    try:
        ref = ModelRef.parse(text)
    except ModelRefError as exc:
        raise ConfigError(f"{where}: {exc}") from exc
    if ref.provider not in providers:
        raise ConfigError(f"{where}: unknown provider '{ref.provider}'")


def _header_value(value: str, where: str) -> str:
    if "\r" in value or "\n" in value:
        raise ConfigError(f"{where} must not contain line breaks")
    return value


def _build_headers(
    entry: Mapping[str, Any], existing: ProviderSpec | None, where: str
) -> tuple[tuple[str, str], ...]:
    if "headers" not in entry:
        return existing.extra_headers if existing else ()
    table = entry["headers"]
    if not isinstance(table, Mapping):
        raise ConfigError(f"{where}.headers must be a table")
    result: list[tuple[str, str]] = []
    for key, value in table.items():
        if not HEADER_NAME.match(str(key)) or not isinstance(value, str):
            raise ConfigError(f"{where}.headers has an invalid entry: {key!r}")
        result.append((str(key), _header_value(value.strip(), f"{where}.headers.{key}")))
    return tuple(result)


def _build_providers(raw: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, ProviderSpec]:
    providers = builtin_providers(environ)
    for name, entry in _section(raw, "providers").items():
        where = f"providers.{name}"
        if not PROVIDER_NAME.match(name):
            raise ConfigError(f"invalid provider name: {name!r}")
        if not isinstance(entry, Mapping):
            raise ConfigError(f"[{where}] must be a table")
        _check_keys(entry, {"protocol", "base_url", "api_key_env", "user_agent", "headers"}, where)
        existing = providers.get(name)
        protocol = _get(entry, "protocol", str, existing.protocol if existing else "", where)
        base_url = _get(entry, "base_url", str, existing.base_url if existing else "", where)
        api_key_env = _get(
            entry, "api_key_env", str, existing.api_key_env if existing else "", where
        )
        if protocol not in PROTOCOLS:
            raise ConfigError(f"{where}.protocol must be one of: {', '.join(PROTOCOLS)}")
        if not base_url.startswith(("http://", "https://")):
            raise ConfigError(f"{where}.base_url must start with http:// or https://")
        user_agent = _get(entry, "user_agent", str, existing.user_agent if existing else "", where)
        providers[name] = ProviderSpec(
            name=name,
            protocol=protocol,
            base_url=base_url.rstrip("/"),
            api_key_env=api_key_env.strip(),
            user_agent=_header_value(user_agent.strip(), f"{where}.user_agent"),
            extra_headers=_build_headers(entry, existing, where),
        )
    return providers


def _build_agent(raw: Mapping[str, Any], providers: Mapping[str, ProviderSpec]) -> AgentConfig:
    section = _section(raw, "agent")
    _check_keys(
        section,
        {
            "model",
            "fallback",
            "max_steps",
            "iteration_budget",
            "temperature",
            "system_prompt",
            "context_tokens",
            "skills",
        },
        "agent",
    )
    model = _get(section, "model", str, "", "agent").strip()
    fallback = tuple(_str_list(section, "fallback", "agent"))
    max_steps = _get(section, "max_steps", int, 40, "agent")
    default_budget = max_steps if "max_steps" in section else 500
    iteration_budget = _get(section, "iteration_budget", int, default_budget, "agent")
    temperature = _get(section, "temperature", float, 0.2, "agent")
    system_prompt = _get(section, "system_prompt", str, "", "agent").strip()
    context_tokens = _get(section, "context_tokens", int, 32000, "agent")
    skills = tuple(_str_list(section, "skills", "agent")) if "skills" in section else ()
    for skill_name in skills:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", skill_name):
            raise ConfigError(f"agent.skills contains invalid skill name: {skill_name!r}")
    if context_tokens != 0 and not 2000 <= context_tokens <= 4_000_000:
        raise ConfigError("agent.context_tokens must be 0 or between 2000 and 4000000")
    if not 1 <= max_steps <= 1000:
        raise ConfigError("agent.max_steps must be between 1 and 1000")
    if not 1 <= iteration_budget <= 1000:
        raise ConfigError("agent.iteration_budget must be between 1 and 1000")
    if not 0.0 <= temperature <= 2.0:
        raise ConfigError("agent.temperature must be between 0 and 2")
    if model:
        _check_model(model, providers, "agent.model")
    for item in fallback:
        _check_model(item, providers, "agent.fallback")
    return AgentConfig(
        model=model,
        fallback=fallback,
        max_steps=max_steps,
        iteration_budget=iteration_budget,
        temperature=temperature,
        system_prompt=system_prompt,
        context_tokens=context_tokens,
        skills=skills,
    )


def _build_auxiliary(
    raw: Mapping[str, Any], providers: Mapping[str, ProviderSpec]
) -> AuxiliaryConfig:
    section = _section(raw, "auxiliary")
    _check_keys(section, {"model", "fallback"}, "auxiliary")
    model = _get(section, "model", str, "", "auxiliary").strip()
    fallback = tuple(_str_list(section, "fallback", "auxiliary"))
    if model:
        _check_model(model, providers, "auxiliary.model")
    for item in fallback:
        _check_model(item, providers, "auxiliary.fallback")
    return AuxiliaryConfig(model=model, fallback=fallback)


def _build_permissions(raw: Mapping[str, Any]) -> PermissionConfig:
    section = _section(raw, "permissions")
    _check_keys(section, {"mode", "workspace"}, "permissions")
    mode = _get(section, "mode", str, "ask", "permissions").strip()
    workspace = _get(section, "workspace", str, ".", "permissions").strip() or "."
    if mode not in PERMISSION_MODES:
        raise ConfigError(f"permissions.mode must be one of: {', '.join(PERMISSION_MODES)}")
    return PermissionConfig(mode=mode, workspace=workspace)


def _build_logging(raw: Mapping[str, Any]) -> LoggingConfig:
    section = _section(raw, "logging")
    _check_keys(section, {"level", "file", "json"}, "logging")
    level = _get(section, "level", str, "INFO", "logging").strip().upper()
    file = _get(section, "file", str, "", "logging").strip()
    as_json = _get(section, "json", bool, False, "logging")
    if level not in LOG_LEVELS:
        raise ConfigError(f"logging.level must be one of: {', '.join(LOG_LEVELS)}")
    return LoggingConfig(level=level, file=file, json=as_json)


def _build_network(raw: Mapping[str, Any]) -> NetworkConfig:
    section = _section(raw, "network")
    _check_keys(
        section,
        {
            "timeout_seconds",
            "max_retries",
            "local_only",
            "cooldown_seconds",
            "max_cooldown_seconds",
            "retry_jitter",
            "first_token_seconds",
        },
        "network",
    )
    timeout = _get(section, "timeout_seconds", float, 120.0, "network")
    retries = _get(section, "max_retries", int, 3, "network")
    local_only = _get(section, "local_only", bool, False, "network")
    cooldown = _get(section, "cooldown_seconds", float, 30.0, "network")
    max_cooldown = _get(section, "max_cooldown_seconds", float, 300.0, "network")
    jitter = _get(section, "retry_jitter", float, 0.0, "network")
    first_token: float | None = None
    if "first_token_seconds" in section:
        raw_first_token = _get(section, "first_token_seconds", float, 0.0, "network")
        if raw_first_token == 0.0:
            first_token = None
        elif 1.0 <= raw_first_token <= 600.0:
            first_token = raw_first_token
        else:
            raise ConfigError("network.first_token_seconds must be 0 or between 1 and 600")
    if not 1.0 <= timeout <= 600.0:
        raise ConfigError("network.timeout_seconds must be between 1 and 600")
    if not 0 <= retries <= 10:
        raise ConfigError("network.max_retries must be between 0 and 10")
    if not 0.0 <= cooldown <= 3600.0:
        raise ConfigError("network.cooldown_seconds must be between 0 and 3600")
    if not 0.0 <= max_cooldown <= 86400.0:
        raise ConfigError("network.max_cooldown_seconds must be between 0 and 86400")
    if cooldown > max_cooldown:
        raise ConfigError("network.cooldown_seconds cannot exceed network.max_cooldown_seconds")
    if not 0.0 <= jitter <= 1.0:
        raise ConfigError("network.retry_jitter must be between 0 and 1")
    return NetworkConfig(
        timeout_seconds=timeout,
        max_retries=retries,
        local_only=local_only,
        cooldown_seconds=cooldown,
        max_cooldown_seconds=max_cooldown,
        retry_jitter=jitter,
        first_token_seconds=first_token,
    )


def _build_tools(raw: Mapping[str, Any]) -> ToolsConfig:
    section = _section(raw, "tools")
    _check_keys(section, {"max_output_chars", "timeout_seconds", "max_parallel_tools"}, "tools")
    limit = _get(section, "max_output_chars", int, 20000, "tools")
    timeout = _get(section, "timeout_seconds", float, 120.0, "tools")
    parallel = _get(section, "max_parallel_tools", int, 4, "tools")
    if not 500 <= limit <= 500000:
        raise ConfigError("tools.max_output_chars must be between 500 and 500000")
    if not 1.0 <= timeout <= 3600.0:
        raise ConfigError("tools.timeout_seconds must be between 1 and 3600")
    if not 1 <= parallel <= 32:
        raise ConfigError("tools.max_parallel_tools must be between 1 and 32")
    return ToolsConfig(max_output_chars=limit, timeout_seconds=timeout, max_parallel_tools=parallel)


def _build_learning(raw: Mapping[str, Any]) -> LearningConfig:
    section = _section(raw, "learning")
    _check_keys(section, {"mode", "lessons_in_prompt", "max_lessons", "skills"}, "learning")
    mode = _get(section, "mode", str, "ask", "learning").strip().lower()
    in_prompt = _get(section, "lessons_in_prompt", int, 5, "learning")
    maximum = _get(section, "max_lessons", int, 500, "learning")
    skills = _get(section, "skills", bool, False, "learning")
    if mode not in LEARNING_MODES:
        raise ConfigError(f"learning.mode must be one of: {', '.join(LEARNING_MODES)}")
    if not 0 <= in_prompt <= 20:
        raise ConfigError("learning.lessons_in_prompt must be between 0 and 20")
    if not 10 <= maximum <= 10000:
        raise ConfigError("learning.max_lessons must be between 10 and 10000")
    return LearningConfig(
        mode=mode, lessons_in_prompt=in_prompt, max_lessons=maximum, skills=skills
    )


def _build_memory(raw: Mapping[str, Any]) -> MemoryConfig:
    section = _section(raw, "memory")
    _check_keys(
        section, {"enabled", "max_memories", "retention_days", "max_content_chars"}, "memory"
    )
    enabled = _get(section, "enabled", bool, True, "memory")
    maximum = _get(section, "max_memories", int, 2000, "memory")
    retention = _get(section, "retention_days", int, 0, "memory")
    max_chars = _get(section, "max_content_chars", int, 500, "memory")
    if not 10 <= maximum <= 100000:
        raise ConfigError("memory.max_memories must be between 10 and 100000")
    if not 0 <= retention <= 36500:
        raise ConfigError("memory.retention_days must be between 0 and 36500")
    if not 100 <= max_chars <= 5000:
        raise ConfigError("memory.max_content_chars must be between 100 and 5000")
    return MemoryConfig(enabled, maximum, retention, max_chars)


def _build_verification(raw: Mapping[str, Any]) -> VerificationConfig:
    section = _section(raw, "verification")
    _check_keys(section, {"enabled", "model_judge"}, "verification")
    enabled = _get(section, "enabled", bool, True, "verification")
    model_judge = _get(section, "model_judge", bool, False, "verification")
    return VerificationConfig(enabled=enabled, model_judge=model_judge)


def _build_planning(raw: Mapping[str, Any]) -> PlanningConfig:
    section = _section(raw, "planning")
    _check_keys(section, {"model_planning", "max_steps"}, "planning")
    model_planning = _get(section, "model_planning", bool, True, "planning")
    max_steps = _get(section, "max_steps", int, 8, "planning")
    if not 1 <= max_steps <= 8:
        raise ConfigError("planning.max_steps must be between 1 and 8")
    return PlanningConfig(model_planning=model_planning, max_steps=max_steps)


def _build_pricing(raw: Mapping[str, Any]) -> PricingConfig:
    section = _section(raw, "pricing")
    _check_keys(section, {"updated_at", "models"}, "pricing")
    updated_at = _get(section, "updated_at", str, "", "pricing").strip()
    models: dict[str, dict[str, float | str]] = {}
    entries = section.get("models", {})
    if not isinstance(entries, Mapping):
        raise ConfigError("pricing.models must be a table")
    for name, entry in entries.items():
        where = f"pricing.models.{name}"
        if not isinstance(entry, Mapping):
            raise ConfigError(f"[{where}] must be a table")
        allowed = {
            "input_per_mtok",
            "output_per_mtok",
            "cached_input_per_mtok",
            "currency",
            "updated_at",
        }
        unknown = sorted(set(entry) - allowed)
        if unknown:
            raise ConfigError(f"{where} has unknown key(s): {', '.join(unknown)}")
        if "input_per_mtok" not in entry or "output_per_mtok" not in entry:
            raise ConfigError(f"{where} requires input_per_mtok and output_per_mtok")
        cleaned: dict[str, float | str] = {}
        for key in ("input_per_mtok", "output_per_mtok"):
            value = entry[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ConfigError(f"{where}.{key} must be a non-negative number")
            cleaned[key] = float(value)
        if "cached_input_per_mtok" in entry:
            value = entry["cached_input_per_mtok"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ConfigError(f"{where}.cached_input_per_mtok must be a non-negative number")
            cleaned["cached_input_per_mtok"] = float(value)
        if "currency" in entry:
            currency = entry["currency"]
            if not isinstance(currency, str) or not currency.strip():
                raise ConfigError(f"{where}.currency must be a non-empty string")
            cleaned["currency"] = currency.strip()
        if "updated_at" in entry:
            stamp = entry["updated_at"]
            if not isinstance(stamp, str) or not stamp.strip():
                raise ConfigError(f"{where}.updated_at must be a non-empty string")
            cleaned["updated_at"] = stamp.strip()
        models[str(name)] = cleaned
    return PricingConfig(updated_at=updated_at, models=models)


def _build_browser(raw: Mapping[str, Any]) -> BrowserConfig:
    section = _section(raw, "browser")
    _check_keys(
        section,
        {
            "enabled",
            "max_page_bytes",
            "navigation_timeout_seconds",
            "max_screenshot_width",
            "max_screenshot_height",
            "backend",
            "cdp_endpoint",
            "chromium_executable",
            "profile_dir",
            "headless",
            "interaction_timeout_seconds",
            "startup_timeout_seconds",
            "max_snapshot_tokens",
            "allowed_domains",
            "blocked_domains",
            "download_dir",
            "max_download_mb",
            "dns_cache_ttl_seconds",
            "max_sessions",
            "session_idle_ttl_seconds",
        },
        "browser",
    )
    enabled = _get(section, "enabled", bool, False, "browser")
    max_page_bytes = _get(section, "max_page_bytes", int, 2_000_000, "browser")
    navigation_timeout = _get(section, "navigation_timeout_seconds", float, 30.0, "browser")
    screenshot_width = _get(section, "max_screenshot_width", int, 1280, "browser")
    screenshot_height = _get(section, "max_screenshot_height", int, 800, "browser")
    backend = _get(section, "backend", str, "auto", "browser").strip().lower()
    cdp_endpoint = _get(section, "cdp_endpoint", str, "", "browser").strip()
    chromium_executable = _get(section, "chromium_executable", str, "", "browser").strip()
    profile_dir = _get(section, "profile_dir", str, "", "browser").strip()
    headless = _get(section, "headless", bool, True, "browser")
    interaction_timeout = _get(section, "interaction_timeout_seconds", float, 10.0, "browser")
    startup_timeout = _get(section, "startup_timeout_seconds", float, 25.0, "browser")
    max_snapshot_tokens = _get(section, "max_snapshot_tokens", int, 3000, "browser")
    allowed_domains = _get(section, "allowed_domains", list, [], "browser")
    blocked_domains = _get(section, "blocked_domains", list, [], "browser")
    download_dir = _get(section, "download_dir", str, "", "browser").strip()
    max_download_mb = _get(section, "max_download_mb", int, 50, "browser")
    dns_cache_ttl = _get(section, "dns_cache_ttl_seconds", float, 60.0, "browser")
    max_sessions = _get(section, "max_sessions", int, 3, "browser")
    session_idle_ttl = _get(section, "session_idle_ttl_seconds", float, 900.0, "browser")
    if not 100_000 <= max_page_bytes <= 20_000_000:
        raise ConfigError("browser.max_page_bytes must be between 100000 and 20000000")
    if not 5.0 <= navigation_timeout <= 300.0:
        raise ConfigError("browser.navigation_timeout_seconds must be between 5 and 300")
    if not 320 <= screenshot_width <= 3840 or not 240 <= screenshot_height <= 2160:
        raise ConfigError("browser screenshot bounds are outside the allowed range")
    if backend not in BROWSER_BACKENDS:
        allowed = ", ".join(repr(item) for item in BROWSER_BACKENDS)
        raise ConfigError(f"browser.backend currently supports only {allowed}")
    if backend in {"remote", "attach"} and not cdp_endpoint:
        raise ConfigError(f"browser.backend {backend!r} requires browser.cdp_endpoint")
    if not 1.0 <= interaction_timeout <= 120.0:
        raise ConfigError("browser.interaction_timeout_seconds must be between 1 and 120")
    if not 5.0 <= startup_timeout <= 120.0:
        raise ConfigError("browser.startup_timeout_seconds must be between 5 and 120")
    if not 200 <= max_snapshot_tokens <= 20_000:
        raise ConfigError("browser.max_snapshot_tokens must be between 200 and 20000")
    if not 1 <= max_download_mb <= 500:
        raise ConfigError("browser.max_download_mb must be between 1 and 500")
    if not 1.0 <= dns_cache_ttl <= 3600.0:
        raise ConfigError("browser.dns_cache_ttl_seconds must be between 1 and 3600")
    if not 1 <= max_sessions <= 8:
        raise ConfigError("browser.max_sessions must be between 1 and 8")
    if not 30.0 <= session_idle_ttl <= 86400.0:
        raise ConfigError("browser.session_idle_ttl_seconds must be between 30 and 86400")
    domain_groups = (("allowed_domains", allowed_domains), ("blocked_domains", blocked_domains))
    for name, domains in domain_groups:
        cleaned: list[str] = []
        for entry in domains:
            if not isinstance(entry, str) or not entry.strip():
                raise ConfigError(f"browser.{name} entries must be non-empty strings")
            cleaned.append(entry.strip().lower().lstrip("."))
        if name == "allowed_domains":
            allowed_clean: tuple[str, ...] = tuple(cleaned)
        else:
            blocked_clean: tuple[str, ...] = tuple(cleaned)
    return BrowserConfig(
        enabled=enabled,
        max_page_bytes=max_page_bytes,
        navigation_timeout_seconds=navigation_timeout,
        max_screenshot_width=screenshot_width,
        max_screenshot_height=screenshot_height,
        backend=backend,
        cdp_endpoint=cdp_endpoint,
        chromium_executable=chromium_executable,
        profile_dir=profile_dir,
        headless=headless,
        interaction_timeout_seconds=interaction_timeout,
        startup_timeout_seconds=startup_timeout,
        max_snapshot_tokens=max_snapshot_tokens,
        allowed_domains=allowed_clean,
        blocked_domains=blocked_clean,
        download_dir=download_dir,
        max_download_mb=max_download_mb,
        dns_cache_ttl_seconds=dns_cache_ttl,
        max_sessions=max_sessions,
        session_idle_ttl_seconds=session_idle_ttl,
    )


def _build_sandbox(raw: Mapping[str, Any], env: Mapping[str, str]) -> SandboxConfig:
    section = _section(raw, "sandbox")
    _check_keys(
        section,
        {
            "backend",
            "runtime",
            "image",
            "required",
            "network",
            "memory_mb",
            "cpu_limit",
            "pids_limit",
            "user",
            "image_allowlist",
            "digest_pin_required",
            "seccomp_profile",
            "apparmor_profile",
        },
        "sandbox",
    )
    termux = is_termux(env)
    backend = (
        _get(section, "backend", str, "host" if termux else "container", "sandbox").strip().lower()
    )
    runtime = _get(section, "runtime", str, "docker", "sandbox").strip()
    image = _get(section, "image", str, "python:3.12-alpine", "sandbox").strip()
    required = _get(section, "required", bool, not (termux and backend == "host"), "sandbox")
    network = _get(section, "network", bool, False, "sandbox")
    memory_mb = _get(section, "memory_mb", int, 1024, "sandbox")
    cpu_limit = _get(section, "cpu_limit", float, 1.0, "sandbox")
    pids_limit = _get(section, "pids_limit", int, 128, "sandbox")
    user = _get(section, "user", str, "auto", "sandbox").strip()
    image_allowlist_raw = _get(section, "image_allowlist", list, [], "sandbox")
    digest_pin_required = _get(section, "digest_pin_required", bool, False, "sandbox")
    seccomp_profile = _get(section, "seccomp_profile", str, "", "sandbox").strip()
    apparmor_profile = _get(section, "apparmor_profile", str, "", "sandbox").strip()
    if backend not in SANDBOX_BACKENDS:
        allowed = ", ".join(repr(item) for item in SANDBOX_BACKENDS)
        raise ConfigError(f"sandbox.backend currently supports only {allowed}")
    if backend == "host" and required:
        raise ConfigError("sandbox.backend 'host' is not isolated; set sandbox.required = false")
    if not runtime or not image:
        raise ConfigError("sandbox.runtime and sandbox.image must not be empty")
    if not 128 <= memory_mb <= 65536:
        raise ConfigError("sandbox.memory_mb must be between 128 and 65536")
    if not 0.1 <= cpu_limit <= 64:
        raise ConfigError("sandbox.cpu_limit must be between 0.1 and 64")
    if not 16 <= pids_limit <= 4096:
        raise ConfigError("sandbox.pids_limit must be between 16 and 4096")
    if not SANDBOX_USER_PATTERN.match(user):
        raise ConfigError("sandbox.user must be 'auto', a user name, a numeric uid, or uid:gid")
    image_allowlist: list[str] = []
    for entry in image_allowlist_raw:
        text = str(entry).strip()
        if not text or any(character.isspace() for character in text) or text.startswith("-"):
            raise ConfigError(f"sandbox.image_allowlist contains an invalid entry: {entry!r}")
        image_allowlist.append(text)
    if apparmor_profile and not APPARMOR_PATTERN.match(apparmor_profile):
        raise ConfigError("sandbox.apparmor_profile must be a profile name")
    return SandboxConfig(
        backend,
        runtime,
        image,
        required,
        network,
        memory_mb,
        cpu_limit,
        pids_limit,
        user,
        tuple(image_allowlist),
        digest_pin_required,
        seccomp_profile,
        apparmor_profile,
    )


def build_config(raw: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> Config:
    env = os.environ if environ is None else environ
    unknown = sorted(set(raw) - TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(unknown)}")
    providers = _build_providers(raw, env)
    return Config(
        agent=_build_agent(raw, providers),
        auxiliary=_build_auxiliary(raw, providers),
        permissions=_build_permissions(raw),
        logging=_build_logging(raw),
        network=_build_network(raw),
        tools=_build_tools(raw),
        learning=_build_learning(raw),
        memory=_build_memory(raw),
        verification=_build_verification(raw),
        planning=_build_planning(raw),
        pricing=_build_pricing(raw),
        browser=_build_browser(raw),
        sandbox=_build_sandbox(raw, env),
        providers=providers,
    )


def load_config(
    *,
    config_path: Path | None = None,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Config:
    env = os.environ if environ is None else environ
    raw: dict[str, Any] = {}
    if config_path is not None:
        if not config_path.is_file():
            raise ConfigError(f"config file not found: {config_path}")
        raw = deep_merge(raw, read_toml(config_path))
    else:
        user_file = paths.user_config_path(env)
        if user_file.is_file():
            raw = deep_merge(raw, read_toml(user_file))
        project_file = paths.project_config_path(cwd or Path.cwd())
        if project_file is not None:
            raw = deep_merge(raw, read_toml(project_file))
    raw = deep_merge(raw, env_overrides(env))
    if overrides:
        raw = deep_merge(raw, overrides)
    return build_config(raw, env)
