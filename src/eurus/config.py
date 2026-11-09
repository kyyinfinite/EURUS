from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from eurus import paths
from eurus.audio.config import AudioConfig
from eurus.catalog import PROTOCOLS, ProviderSpec, builtin_providers
from eurus.errors import ConfigError, ModelRefError
from eurus.generation.config import GenerationConfig
from eurus.models import ModelRef
from eurus.office.config import OfficeConfig
from eurus.research.config import ResearchConfig
from eurus.termux import is_termux

PERMISSION_MODES: tuple[str, ...] = ("ask", "auto-edit", "yolo")
SANDBOX_BACKENDS: tuple[str, ...] = ("container", "host")
LEARNING_MODES: tuple[str, ...] = ("off", "ask", "auto")
MAX_EVALUATION_RETENTION_DAYS = 3650
LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")
TOP_LEVEL_KEYS = frozenset(
    {
        "agent",
        "agentic_engineering",
        "audio",
        "auxiliary",
        "generation",
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
        "network_security",
        "office",
        "research",
        "autonomy",
    }
)
PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
SANDBOX_USER_PATTERN = re.compile(r"^(auto|[a-z_][a-z0-9_-]{0,31}|\d{1,5}(:\d{1,5})?)$")
APPARMOR_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
NETWORK_PROFILE_VALUES = ("offline", "web_read", "api_access", "package_install", "restricted")
DEFAULT_PACKAGE_HOSTS_TUPLE = (
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
)
HOST_PATTERN = re.compile(r"^\*?\.[A-Za-z0-9*][A-Za-z0-9.*-]*$|^[A-Za-z0-9*][A-Za-z0-9.*-]*$")
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})

ENV_OVERRIDES: tuple[tuple[str, str, str, type[Any]], ...] = (
    ("EURUS_MODEL", "agent", "model", str),
    ("EURUS_SKILLS", "agent", "skills", list),
    ("EURUS_FALLBACK", "agent", "fallback", list),
    ("EURUS_MAX_STEPS", "agent", "max_steps", int),
    ("EURUS_TEMPERATURE", "agent", "temperature", float),
    ("EURUS_STT_MODEL", "audio", "stt_model", str),
    ("EURUS_STT_LANGUAGE", "audio", "stt_language", str),
    ("EURUS_TTS_MODEL", "audio", "tts_model", str),
    ("EURUS_TTS_VOICE", "audio", "tts_voice", str),
    ("EURUS_TTS_FORMAT", "audio", "tts_format", str),
    ("EURUS_REALTIME_MODEL", "audio", "realtime_model", str),
    ("EURUS_REALTIME_VOICE", "audio", "realtime_voice", str),
    ("EURUS_REALTIME_SESSION_SECONDS", "audio", "realtime_session_seconds", float),
    ("EURUS_REALTIME_TIMEOUT_SECONDS", "audio", "realtime_timeout_seconds", float),
    ("EURUS_IMAGE_MODEL", "generation", "image_model", str),
    ("EURUS_IMAGE_SIZE", "generation", "image_size", str),
    ("EURUS_IMAGE_TIMEOUT_SECONDS", "generation", "image_timeout_seconds", float),
    ("EURUS_VIDEO_MODEL", "generation", "video_model", str),
    ("EURUS_VIDEO_ASPECT_RATIO", "generation", "video_aspect_ratio", str),
    ("EURUS_VIDEO_TIMEOUT_SECONDS", "generation", "video_timeout_seconds", float),
    ("EURUS_VIDEO_POLL_INTERVAL_SECONDS", "generation", "video_poll_interval_seconds", float),
    ("EURUS_VIDEO_JOB_WAIT_SECONDS", "generation", "video_job_wait_seconds", float),
    ("EURUS_GENERATION_MAX_OUTPUTS", "generation", "max_outputs_per_request", int),
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
    ("EURUS_AUTONOMY_ENABLED", "autonomy", "enabled", bool),
    ("EURUS_AUTONOMY_WORKERS", "autonomy", "workers", int),
    ("EURUS_AUTONOMY_POLL_SECONDS", "autonomy", "poll_seconds", float),
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
skills = false
mission_learning = true
evaluation_retention_days = 180

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
network_profile = "offline"
memory_mb = 1024
cpu_limit = 1.0
pids_limit = 128
user = "auto"
image_allowlist = []
digest_pin_required = false
seccomp_profile = ""
apparmor_profile = ""

[network_security]
enabled = true
proxy_bind = "172.17.0.1"
proxy_port = 0
lease_ttl_seconds = 3600.0
max_redirects = 6
max_response_mb = 2
max_tunnel_mb = 64
requests_per_minute = 60
egress_ports = [80, 443]
allow_private_addresses = false
web_read_hosts = []
api_hosts = []
package_hosts = ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"]
restricted_hosts = []

[autonomy]
enabled = true
workers = 2
poll_seconds = 15.0
goals = true
workflows = true
events = true
specialists = true
learning = true
daily_job_limit = 50
pre_script_timeout_seconds = 30.0
delegate_max_batch = 4
delegate_max_iterations = 20
delegate_deadline_seconds = 600.0

[agentic_engineering]
enabled = true
hierarchical_dispatch = true
strict_tool_scope = true
max_mission_tasks = 12
max_review_cycles = 3
autonomous_progression = true
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
    model_policies: tuple[tuple[str, str], ...] = ()

    def model_for_policy(self, policy: str) -> str | None:
        for name, model in self.model_policies:
            if name == policy:
                return model
        return None


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
    network_profile: str = ""
    memory_mb: int = 1024
    cpu_limit: float = 1.0
    pids_limit: int = 128
    user: str = "auto"
    image_allowlist: tuple[str, ...] = ()
    digest_pin_required: bool = False
    seccomp_profile: str = ""
    apparmor_profile: str = ""


@dataclass(frozen=True, slots=True)
class NetworkSecurityConfig:
    enabled: bool = False
    proxy_bind: str = "172.17.0.1"
    proxy_port: int = 0
    lease_ttl_seconds: float = 3600.0
    max_redirects: int = 6
    max_response_mb: int = 2
    max_tunnel_mb: int = 64
    requests_per_minute: int = 60
    egress_ports: tuple[int, ...] = (80, 443)
    allow_private_addresses: bool = False
    web_read_hosts: tuple[str, ...] = ()
    api_hosts: tuple[str, ...] = ()
    package_hosts: tuple[str, ...] = (
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
    )
    restricted_hosts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LearningConfig:
    mode: str = "ask"
    lessons_in_prompt: int = 5
    max_lessons: int = 500
    skills: bool = False
    mission_learning: bool = True
    evaluation_retention_days: int = 180


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    provider: str = "none"
    endpoint: str = ""
    model: str = ""
    api_key_env: str = ""
    dimension: int = 0


@dataclass(frozen=True, slots=True)
class MemoryConfig:
    enabled: bool = True
    max_memories: int = 2000
    retention_days: int = 0
    max_content_chars: int = 500
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)


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
class AutonomyConfig:
    enabled: bool = True
    workers: int = 2
    poll_seconds: float = 15.0
    goals: bool = True
    workflows: bool = True
    events: bool = True
    specialists: bool = True
    learning: bool = True
    daily_job_limit: int = 50
    pre_script_timeout_seconds: float = 30.0
    delegate_max_batch: int = 4
    delegate_max_iterations: int = 20
    delegate_deadline_seconds: float = 600.0


@dataclass(frozen=True, slots=True)
class AgenticEngineeringConfig:
    enabled: bool = True
    hierarchical_dispatch: bool = True
    strict_tool_scope: bool = True
    max_mission_tasks: int = 12
    max_review_cycles: int = 3
    autonomous_progression: bool = True


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
    allow_private_addresses: bool = False


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
    autonomy: AutonomyConfig = field(default_factory=AutonomyConfig)
    agentic_engineering: AgenticEngineeringConfig = field(default_factory=AgenticEngineeringConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    network_security: NetworkSecurityConfig = field(default_factory=NetworkSecurityConfig)
    office: OfficeConfig = field(default_factory=OfficeConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    generation: GenerationConfig = field(default_factory=GenerationConfig)
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


def _build_capabilities(entry: Mapping[str, Any], where: str) -> tuple[tuple[str, bool], ...]:
    from eurus.providers.model_capabilities import CAPABILITY_KEYS, VIEW_KEY_MAP

    raw_caps = entry.get("capabilities")
    if raw_caps is None:
        return ()
    if not isinstance(raw_caps, Mapping):
        raise ConfigError(f"{where}.capabilities must be a table")
    parsed: list[tuple[str, bool]] = []
    for key, value in raw_caps.items():
        normalized = VIEW_KEY_MAP.get(str(key), str(key))
        if normalized not in CAPABILITY_KEYS:
            raise ConfigError(
                f"{where}.capabilities has an unknown capability: {key!r}; "
                f"expected one of {', '.join(CAPABILITY_KEYS)}"
            )
        if not isinstance(value, bool):
            raise ConfigError(f"{where}.capabilities.{key} must be a boolean")
        parsed.append((normalized, value))
    return tuple(parsed)


def _build_providers(raw: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, ProviderSpec]:
    providers = builtin_providers(environ)
    for name, entry in _section(raw, "providers").items():
        where = f"providers.{name}"
        if not PROVIDER_NAME.match(name):
            raise ConfigError(f"invalid provider name: {name!r}")
        if not isinstance(entry, Mapping):
            raise ConfigError(f"[{where}] must be a table")
        _check_keys(
            entry,
            {"protocol", "base_url", "api_key_env", "user_agent", "headers", "capabilities"},
            where,
        )
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
            capabilities=_build_capabilities(entry, where),
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
            "model_policies",
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
    model_policies_raw = section.get("model_policies", {})
    if not isinstance(model_policies_raw, dict):
        raise ConfigError("agent.model_policies must be a table of policy = model")
    policy_entries: list[tuple[str, str]] = []
    for policy_name, model_value in model_policies_raw.items():
        if not isinstance(model_value, str):
            raise ConfigError(f"agent.model_policies.{policy_name} must be a model string")
        _check_model(model_value, providers, f"agent.model_policies.{policy_name}")
        policy_entries.append((str(policy_name), model_value))
    if len({name for name, _ in policy_entries}) != len(policy_entries):
        raise ConfigError("agent.model_policies contains duplicate policy names")
    return AgentConfig(
        model=model,
        fallback=fallback,
        max_steps=max_steps,
        iteration_budget=iteration_budget,
        temperature=temperature,
        system_prompt=system_prompt,
        context_tokens=context_tokens,
        skills=skills,
        model_policies=tuple(policy_entries),
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
    _check_keys(
        section,
        {
            "mode",
            "lessons_in_prompt",
            "max_lessons",
            "skills",
            "mission_learning",
            "evaluation_retention_days",
        },
        "learning",
    )
    mode = _get(section, "mode", str, "ask", "learning").strip().lower()
    in_prompt = _get(section, "lessons_in_prompt", int, 5, "learning")
    maximum = _get(section, "max_lessons", int, 500, "learning")
    skills = _get(section, "skills", bool, False, "learning")
    mission_learning = _get(section, "mission_learning", bool, True, "learning")
    retention = _get(section, "evaluation_retention_days", int, 180, "learning")
    if mode not in LEARNING_MODES:
        raise ConfigError(f"learning.mode must be one of: {', '.join(LEARNING_MODES)}")
    if not 0 <= in_prompt <= 20:
        raise ConfigError("learning.lessons_in_prompt must be between 0 and 20")
    if not 10 <= maximum <= 10000:
        raise ConfigError("learning.max_lessons must be between 10 and 10000")
    if not 0 <= retention <= MAX_EVALUATION_RETENTION_DAYS:
        raise ConfigError(
            "learning.evaluation_retention_days must be between 0 and "
            f"{MAX_EVALUATION_RETENTION_DAYS} (0 disables pruning)"
        )
    return LearningConfig(
        mode=mode,
        lessons_in_prompt=in_prompt,
        max_lessons=maximum,
        skills=skills,
        mission_learning=mission_learning,
        evaluation_retention_days=retention,
    )


def _build_embedding(section: Mapping[str, Any]) -> EmbeddingConfig:
    _check_keys(
        section, {"provider", "endpoint", "model", "api_key_env", "dimension"}, "memory.embedding"
    )
    provider = _get(section, "provider", str, "none", "memory.embedding").strip().lower()
    if provider not in {"none", "http"}:
        raise ConfigError("memory.embedding.provider must be 'none' or 'http'")
    endpoint = _get(section, "endpoint", str, "", "memory.embedding").strip()
    model = _get(section, "model", str, "", "memory.embedding").strip()
    api_key_env = _get(section, "api_key_env", str, "", "memory.embedding").strip()
    dimension = _get(section, "dimension", int, 0, "memory.embedding")
    if provider == "http":
        if not endpoint.startswith(("http://", "https://")):
            raise ConfigError("memory.embedding.endpoint must be an http(s) URL")
        if not model:
            raise ConfigError("memory.embedding.model is required for provider 'http'")
        if not api_key_env:
            raise ConfigError("memory.embedding.api_key_env is required for provider 'http'")
    if not 0 <= dimension <= 100_000:
        raise ConfigError("memory.embedding.dimension must be between 0 and 100000")
    return EmbeddingConfig(
        provider=provider,
        endpoint=endpoint,
        model=model,
        api_key_env=api_key_env,
        dimension=dimension,
    )


def _build_memory(raw: Mapping[str, Any]) -> MemoryConfig:
    section = _section(raw, "memory")
    _check_keys(
        section,
        {"enabled", "max_memories", "retention_days", "max_content_chars", "embedding"},
        "memory",
    )
    enabled = _get(section, "enabled", bool, True, "memory")
    maximum = _get(section, "max_memories", int, 2000, "memory")
    retention = _get(section, "retention_days", int, 0, "memory")
    max_chars = _get(section, "max_content_chars", int, 500, "memory")
    embedding_raw = section.get("embedding", {})
    if not isinstance(embedding_raw, Mapping):
        raise ConfigError("memory.embedding must be a table")
    embedding = _build_embedding(embedding_raw)
    if not 10 <= maximum <= 100000:
        raise ConfigError("memory.max_memories must be between 10 and 100000")
    if not 0 <= retention <= 36500:
        raise ConfigError("memory.retention_days must be between 0 and 36500")
    if not 100 <= max_chars <= 5000:
        raise ConfigError("memory.max_content_chars must be between 100 and 5000")
    return MemoryConfig(enabled, maximum, retention, max_chars, embedding)


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
            "allow_private_addresses",
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
    allow_private_addresses = _get(section, "allow_private_addresses", bool, False, "browser")
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
        allow_private_addresses=allow_private_addresses,
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
            "network_profile",
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
    network_profile = _get(section, "network_profile", str, "", "sandbox").strip().lower()
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
    if network_profile and network_profile not in NETWORK_PROFILE_VALUES:
        allowed_profiles = ", ".join(NETWORK_PROFILE_VALUES)
        raise ConfigError(f"sandbox.network_profile must be one of: {allowed_profiles}")
    if backend == "host" and network_profile and network_profile != "offline":
        raise ConfigError(
            "sandbox.backend 'host' cannot enforce a network profile; "
            "use the container backend or keep network_profile = 'offline'"
        )
    if not network and network_profile and network_profile != "offline":
        raise ConfigError(
            "sandbox.network is false; set sandbox.network = true to use a "
            "non-offline network_profile"
        )
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
        network_profile,
        memory_mb,
        cpu_limit,
        pids_limit,
        user,
        tuple(image_allowlist),
        digest_pin_required,
        seccomp_profile,
        apparmor_profile,
    )


def _build_network_security(raw: Mapping[str, Any]) -> NetworkSecurityConfig:
    section = _section(raw, "network_security")
    _check_keys(
        section,
        {
            "enabled",
            "proxy_bind",
            "proxy_port",
            "lease_ttl_seconds",
            "max_redirects",
            "max_response_mb",
            "max_tunnel_mb",
            "requests_per_minute",
            "egress_ports",
            "allow_private_addresses",
            "web_read_hosts",
            "api_hosts",
            "package_hosts",
            "restricted_hosts",
        },
        "network_security",
    )
    enabled = _get(section, "enabled", bool, True, "network_security")
    proxy_bind = _get(section, "proxy_bind", str, "172.17.0.1", "network_security").strip()
    proxy_port = _get(section, "proxy_port", int, 0, "network_security")
    lease_ttl = _get(section, "lease_ttl_seconds", float, 3600.0, "network_security")
    max_redirects = _get(section, "max_redirects", int, 6, "network_security")
    max_response_mb = _get(section, "max_response_mb", int, 2, "network_security")
    max_tunnel_mb = _get(section, "max_tunnel_mb", int, 64, "network_security")
    requests_per_minute = _get(section, "requests_per_minute", int, 60, "network_security")
    allow_private = _get(section, "allow_private_addresses", bool, False, "network_security")
    raw_ports = section.get("egress_ports", [80, 443])
    if not isinstance(raw_ports, list) or not all(isinstance(item, int) for item in raw_ports):
        raise ConfigError("network_security.egress_ports must be a list of integers")
    if any(isinstance(item, bool) or not 1 <= item <= 65535 for item in raw_ports):
        raise ConfigError("network_security.egress_ports entries must be between 1 and 65535")
    if not raw_ports:
        raise ConfigError("network_security.egress_ports must not be empty")
    try:
        bind_address = ipaddress.ip_address(proxy_bind)
    except ValueError as exc:
        raise ConfigError("network_security.proxy_bind must be an IP address") from exc
    if bind_address.is_unspecified:
        raise ConfigError(
            "network_security.proxy_bind must not be an unspecified address (0.0.0.0 or ::)"
        )
    if not (bind_address.is_private or bind_address.is_loopback):
        raise ConfigError("network_security.proxy_bind must be a private or loopback address")
    if not 0 <= proxy_port <= 65535:
        raise ConfigError("network_security.proxy_port must be between 0 and 65535")
    if not 60 <= lease_ttl <= 86400:
        raise ConfigError("network_security.lease_ttl_seconds must be between 60 and 86400")
    if not 0 <= max_redirects <= 20:
        raise ConfigError("network_security.max_redirects must be between 0 and 20")
    if not 1 <= max_response_mb <= 100:
        raise ConfigError("network_security.max_response_mb must be between 1 and 100")
    if not 1 <= max_tunnel_mb <= 1024:
        raise ConfigError("network_security.max_tunnel_mb must be between 1 and 1024")
    if not 0 <= requests_per_minute <= 10000:
        raise ConfigError("network_security.requests_per_minute must be between 0 and 10000")
    web_read_hosts = _host_list(section, "web_read_hosts")
    api_hosts = _host_list(section, "api_hosts")
    package_hosts = _host_list(section, "package_hosts", default=DEFAULT_PACKAGE_HOSTS_TUPLE)
    restricted_hosts = _host_list(section, "restricted_hosts")
    return NetworkSecurityConfig(
        enabled=enabled,
        proxy_bind=proxy_bind,
        proxy_port=proxy_port,
        lease_ttl_seconds=lease_ttl,
        max_redirects=max_redirects,
        max_response_mb=max_response_mb,
        max_tunnel_mb=max_tunnel_mb,
        requests_per_minute=requests_per_minute,
        egress_ports=tuple(int(item) for item in raw_ports),
        allow_private_addresses=allow_private,
        web_read_hosts=web_read_hosts,
        api_hosts=api_hosts,
        package_hosts=package_hosts,
        restricted_hosts=restricted_hosts,
    )


def _host_list(
    section: Mapping[str, Any], key: str, *, default: tuple[str, ...] = ()
) -> tuple[str, ...]:
    value = section.get(key)
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"network_security.{key} must be a list of strings")
    entries: list[str] = []
    for item in value:
        cleaned = item.strip().lower().lstrip(".")
        if not cleaned:
            continue
        if not HOST_PATTERN.match(cleaned):
            raise ConfigError(f"network_security.{key} contains an invalid host: {item!r}")
        entries.append(cleaned)
    return tuple(entries)


def _build_office(raw: Mapping[str, Any]) -> OfficeConfig:
    section = _section(raw, "office")
    _check_keys(
        section,
        {
            "enabled",
            "max_revision_cycles",
            "max_model_calls",
            "max_workers",
            "deadline_seconds",
            "tool_timeout_seconds",
            "content_score_min",
            "visual_score_min",
            "structural_score_min",
            "allow_research_delegation",
        },
        "office",
    )
    enabled = _get(section, "enabled", bool, True, "office")
    revisions = _get(section, "max_revision_cycles", int, 3, "office")
    model_calls = _get(section, "max_model_calls", int, 60, "office")
    workers = _get(section, "max_workers", int, 3, "office")
    deadline = _get(section, "deadline_seconds", float, 1800.0, "office")
    tool_timeout = _get(section, "tool_timeout_seconds", float, 1800.0, "office")
    content_min = _get(section, "content_score_min", float, 85.0, "office")
    visual_min = _get(section, "visual_score_min", float, 85.0, "office")
    structural_min = _get(section, "structural_score_min", float, 95.0, "office")
    delegation = _get(section, "allow_research_delegation", bool, True, "office")
    if not 0 <= revisions <= 10:
        raise ConfigError("office.max_revision_cycles must be between 0 and 10")
    if not 1 <= model_calls <= 400:
        raise ConfigError("office.max_model_calls must be between 1 and 400")
    if not 1 <= workers <= 12:
        raise ConfigError("office.max_workers must be between 1 and 12")
    if not 30.0 <= deadline <= 7200.0:
        raise ConfigError("office.deadline_seconds must be between 30 and 7200")
    if not 30.0 <= tool_timeout <= 7200.0:
        raise ConfigError("office.tool_timeout_seconds must be between 30 and 7200")
    for name, value in (
        ("content_score_min", content_min),
        ("visual_score_min", visual_min),
        ("structural_score_min", structural_min),
    ):
        if not 0.0 <= value <= 100.0:
            raise ConfigError(f"office.{name} must be between 0 and 100")
    return OfficeConfig(
        enabled=enabled,
        max_revision_cycles=revisions,
        max_model_calls=model_calls,
        max_workers=workers,
        deadline_seconds=deadline,
        tool_timeout_seconds=tool_timeout,
        content_score_min=content_min,
        visual_score_min=visual_min,
        structural_score_min=structural_min,
        allow_research_delegation=delegation,
    )


def _build_research(raw: Mapping[str, Any]) -> ResearchConfig:
    section = _section(raw, "research")
    _check_keys(
        section,
        {
            "enabled",
            "default_mode",
            "providers",
            "provider_timeout_seconds",
            "provider_retries",
            "fetch_timeout_seconds",
            "max_fetch_mb",
            "max_content_chars",
            "deep_max_rounds",
            "deep_max_queries",
            "deep_max_sources",
            "deep_max_fetches",
            "deep_deadline_seconds",
            "deep_coverage_target",
            "browser_fallback",
            "browser_fetch_budget",
            "cache_ttl_seconds",
            "cache_max_entries",
            "job_retention_days",
        },
        "research",
    )
    enabled = _get(section, "enabled", bool, True, "research")
    default_mode = _get(section, "default_mode", str, "standard", "research").strip().lower()
    providers_raw = _get(section, "providers", list, ["ddgs"], "research")
    providers: list[str] = []
    for entry in providers_raw:
        name = str(entry).strip().lower()
        if name:
            providers.append(name)
    try:
        return ResearchConfig(
            enabled=enabled,
            default_mode=default_mode,
            providers=tuple(providers),
            provider_timeout_seconds=_get(
                section, "provider_timeout_seconds", float, 20.0, "research"
            ),
            provider_retries=_get(section, "provider_retries", int, 1, "research"),
            fetch_timeout_seconds=_get(section, "fetch_timeout_seconds", float, 15.0, "research"),
            max_fetch_mb=_get(section, "max_fetch_mb", int, 2, "research"),
            max_content_chars=_get(section, "max_content_chars", int, 12_000, "research"),
            deep_max_rounds=_get(section, "deep_max_rounds", int, 3, "research"),
            deep_max_queries=_get(section, "deep_max_queries", int, 12, "research"),
            deep_max_sources=_get(section, "deep_max_sources", int, 16, "research"),
            deep_max_fetches=_get(section, "deep_max_fetches", int, 10, "research"),
            deep_deadline_seconds=_get(section, "deep_deadline_seconds", float, 240.0, "research"),
            deep_coverage_target=_get(section, "deep_coverage_target", float, 0.8, "research"),
            browser_fallback=_get(section, "browser_fallback", bool, True, "research"),
            browser_fetch_budget=_get(section, "browser_fetch_budget", int, 2, "research"),
            cache_ttl_seconds=_get(section, "cache_ttl_seconds", float, 900.0, "research"),
            cache_max_entries=_get(section, "cache_max_entries", int, 256, "research"),
            job_retention_days=_get(section, "job_retention_days", int, 30, "research"),
        )
    except ValueError as exc:
        raise ConfigError(f"[research]: {exc}") from exc


def _build_autonomy(raw: Mapping[str, Any]) -> AutonomyConfig:
    section = _section(raw, "autonomy")
    _check_keys(
        section,
        {
            "enabled",
            "workers",
            "poll_seconds",
            "goals",
            "workflows",
            "events",
            "specialists",
            "learning",
            "daily_job_limit",
            "pre_script_timeout_seconds",
            "delegate_max_batch",
            "delegate_max_iterations",
            "delegate_deadline_seconds",
        },
        "autonomy",
    )
    workers = _get(section, "workers", int, 2, "autonomy")
    poll_seconds = _get(section, "poll_seconds", float, 15.0, "autonomy")
    daily_limit = _get(section, "daily_job_limit", int, 50, "autonomy")
    pre_script_timeout = _get(section, "pre_script_timeout_seconds", float, 30.0, "autonomy")
    delegate_batch = _get(section, "delegate_max_batch", int, 4, "autonomy")
    delegate_iterations = _get(section, "delegate_max_iterations", int, 20, "autonomy")
    delegate_deadline = _get(section, "delegate_deadline_seconds", float, 600.0, "autonomy")
    if not 1 <= workers <= 16:
        raise ConfigError("autonomy.workers must be between 1 and 16")
    if not 0.05 <= poll_seconds <= 300.0:
        raise ConfigError("autonomy.poll_seconds must be between 0.05 and 300")
    if not 1 <= daily_limit <= 10_000:
        raise ConfigError("autonomy.daily_job_limit must be between 1 and 10000")
    if not 1.0 <= pre_script_timeout <= 3600.0:
        raise ConfigError("autonomy.pre_script_timeout_seconds must be between 1 and 3600")
    if not 1 <= delegate_batch <= 16:
        raise ConfigError("autonomy.delegate_max_batch must be between 1 and 16")
    if not 1 <= delegate_iterations <= 50:
        raise ConfigError("autonomy.delegate_max_iterations must be between 1 and 50")
    if not 5.0 <= delegate_deadline <= 86_400.0:
        raise ConfigError("autonomy.delegate_deadline_seconds must be between 5 and 86400")
    return AutonomyConfig(
        enabled=_get(section, "enabled", bool, True, "autonomy"),
        workers=workers,
        poll_seconds=poll_seconds,
        goals=_get(section, "goals", bool, True, "autonomy"),
        workflows=_get(section, "workflows", bool, True, "autonomy"),
        events=_get(section, "events", bool, True, "autonomy"),
        specialists=_get(section, "specialists", bool, True, "autonomy"),
        learning=_get(section, "learning", bool, True, "autonomy"),
        daily_job_limit=daily_limit,
        pre_script_timeout_seconds=pre_script_timeout,
        delegate_max_batch=delegate_batch,
        delegate_max_iterations=delegate_iterations,
        delegate_deadline_seconds=delegate_deadline,
    )


def _build_agentic_engineering(raw: Mapping[str, Any]) -> AgenticEngineeringConfig:
    section = _section(raw, "agentic_engineering")
    _check_keys(
        section,
        {
            "enabled",
            "hierarchical_dispatch",
            "strict_tool_scope",
            "max_mission_tasks",
            "max_review_cycles",
            "autonomous_progression",
        },
        "agentic_engineering",
    )
    max_tasks = _get(section, "max_mission_tasks", int, 12, "agentic_engineering")
    max_cycles = _get(section, "max_review_cycles", int, 3, "agentic_engineering")
    if not 1 <= max_tasks <= 64:
        raise ConfigError("agentic_engineering.max_mission_tasks must be between 1 and 64")
    if not 1 <= max_cycles <= 20:
        raise ConfigError("agentic_engineering.max_review_cycles must be between 1 and 20")
    if not _get(section, "strict_tool_scope", bool, True, "agentic_engineering"):
        raise ConfigError(
            "agentic_engineering.strict_tool_scope cannot be disabled: "
            "deny-by-default tool scoping is mandatory (FR-006)"
        )
    return AgenticEngineeringConfig(
        enabled=_get(section, "enabled", bool, True, "agentic_engineering"),
        hierarchical_dispatch=_get(
            section, "hierarchical_dispatch", bool, True, "agentic_engineering"
        ),
        strict_tool_scope=True,
        max_mission_tasks=max_tasks,
        max_review_cycles=max_cycles,
        autonomous_progression=_get(
            section, "autonomous_progression", bool, True, "agentic_engineering"
        ),
    )


def _build_audio(raw: Mapping[str, Any]) -> AudioConfig:
    section = _section(raw, "audio")
    _check_keys(
        section,
        {
            "stt_model",
            "stt_language",
            "stt_timeout_seconds",
            "tts_model",
            "tts_voice",
            "tts_format",
            "tts_timeout_seconds",
            "realtime_model",
            "realtime_voice",
            "realtime_session_seconds",
            "realtime_timeout_seconds",
        },
        "audio",
    )
    stt_model = str(_get(section, "stt_model", str, "", "audio")).strip()
    stt_language = str(_get(section, "stt_language", str, "", "audio")).strip()
    tts_model = str(_get(section, "tts_model", str, "", "audio")).strip()
    tts_voice = str(_get(section, "tts_voice", str, "alloy", "audio")).strip()
    tts_format = str(_get(section, "tts_format", str, "mp3", "audio")).strip()
    realtime_model = str(_get(section, "realtime_model", str, "", "audio")).strip()
    realtime_voice = str(_get(section, "realtime_voice", str, "alloy", "audio")).strip()
    try:
        return AudioConfig(
            stt_model=stt_model,
            stt_language=stt_language,
            stt_timeout_seconds=_get(section, "stt_timeout_seconds", float, 180.0, "audio"),
            tts_model=tts_model,
            tts_voice=tts_voice,
            tts_format=tts_format,
            tts_timeout_seconds=_get(section, "tts_timeout_seconds", float, 120.0, "audio"),
            realtime_model=realtime_model,
            realtime_voice=realtime_voice,
            realtime_session_seconds=_get(
                section, "realtime_session_seconds", float, 300.0, "audio"
            ),
            realtime_timeout_seconds=_get(
                section, "realtime_timeout_seconds", float, 30.0, "audio"
            ),
        )
    except ValueError as exc:
        raise ConfigError(f"audio: {exc}") from exc


def _build_generation(raw: Mapping[str, Any]) -> GenerationConfig:
    section = _section(raw, "generation")
    _check_keys(
        section,
        {
            "image_model",
            "image_size",
            "image_timeout_seconds",
            "video_model",
            "video_aspect_ratio",
            "video_timeout_seconds",
            "video_poll_interval_seconds",
            "video_job_wait_seconds",
            "max_outputs_per_request",
        },
        "generation",
    )
    try:
        return GenerationConfig(
            image_model=str(_get(section, "image_model", str, "", "generation")).strip(),
            image_size=str(_get(section, "image_size", str, "", "generation")).strip(),
            image_timeout_seconds=_get(
                section, "image_timeout_seconds", float, 180.0, "generation"
            ),
            video_model=str(_get(section, "video_model", str, "", "generation")).strip(),
            video_aspect_ratio=str(
                _get(section, "video_aspect_ratio", str, "16:9", "generation")
            ).strip(),
            video_timeout_seconds=_get(
                section, "video_timeout_seconds", float, 900.0, "generation"
            ),
            video_poll_interval_seconds=_get(
                section, "video_poll_interval_seconds", float, 10.0, "generation"
            ),
            video_job_wait_seconds=_get(
                section, "video_job_wait_seconds", float, 120.0, "generation"
            ),
            max_outputs_per_request=_get(section, "max_outputs_per_request", int, 4, "generation"),
        )
    except ValueError as exc:
        raise ConfigError(f"generation: {exc}") from exc


def build_config(raw: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> Config:
    env = os.environ if environ is None else environ
    unknown = sorted(set(raw) - TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join(unknown)}")
    providers = _build_providers(raw, env)
    sandbox = _build_sandbox(raw, env)
    network_security = _build_network_security(raw)
    if sandbox.network_profile == "restricted" and not network_security.restricted_hosts:
        raise ConfigError(
            "sandbox.network_profile 'restricted' requires non-empty "
            "network_security.restricted_hosts"
        )
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
        sandbox=sandbox,
        network_security=network_security,
        office=_build_office(raw),
        research=_build_research(raw),
        audio=_build_audio(raw),
        generation=_build_generation(raw),
        autonomy=_build_autonomy(raw),
        agentic_engineering=_build_agentic_engineering(raw),
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
