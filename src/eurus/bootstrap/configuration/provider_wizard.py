from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from eurus.bootstrap.configuration.env_manager import EnvFile, global_env_path
from eurus.bootstrap.configuration.secrets import mask_secret, validate_secret_format
from eurus.bootstrap.configuration.toml_manager import TomlManager
from eurus.bootstrap.wizard import BootstrapIO
from eurus.catalog import ProviderSpec, builtin_providers
from eurus.config import Config
from eurus.diagnose import probe_model
from eurus.errors import EurusError
from eurus.models import ModelRef

PROVIDER_MENU: tuple[tuple[str, str], ...] = (
    ("Groq", "groq"),
    ("OpenAI", "openai"),
    ("Anthropic", "anthropic"),
    ("Gemini", "gemini"),
    ("NVIDIA", "nvidia"),
    ("OpenRouter", "openrouter"),
    ("DeepSeek", "deepseek"),
    ("Mistral", "mistral"),
    ("Together", "together"),
    ("xAI", "xai"),
    ("Ollama", "ollama"),
)

SUGGESTED_MODELS: dict[str, str] = {
    "groq": "groq/llama-3.3-70b-versatile",
    "openai": "openai/gpt-4o-mini",
}

PROBE_TIMEOUT_SECONDS = 45.0


@dataclass(slots=True)
class ProviderSetupResult:
    provider: str = ""
    model: str = ""
    fallback: tuple[str, ...] = ()
    key_env: str = ""
    key_saved: bool = False
    base_url_changed: bool = False
    tested: bool = False
    test_ok: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "fallback": list(self.fallback),
            "key_env": self.key_env,
            "key_saved": self.key_saved,
            "tested": self.tested,
            "test_ok": self.test_ok,
            "detail": self.detail,
        }


def _spec_for(provider_name: str, environ: dict[str, str]) -> ProviderSpec:
    providers = builtin_providers(environ)
    spec = providers.get(provider_name)
    if spec is None:
        raise EurusError(f"unknown provider '{provider_name}'")
    return spec


def _existing_model(config: Config | None) -> str:
    return config.agent.model if config is not None else ""


def choose_provider(io: BootstrapIO) -> str:
    labels = [label for label, _ in PROVIDER_MENU]
    index = io.choose("Select provider", labels, 0)
    return PROVIDER_MENU[index][1]


def _ask_model(io: BootstrapIO, provider_name: str, environ: dict[str, str], current: str) -> str:
    if provider_name == "ollama":
        listed = _list_ollama_models(io, environ)
        if listed:
            index = io.choose("Select default model", listed, 0)
            chosen = listed[index]
            return f"ollama/{chosen}"
        suggested = ""
    else:
        suggested = SUGGESTED_MODELS.get(provider_name, "")
    hint = f" (suggestion: {suggested})" if suggested else ""
    while True:
        raw = io.ask(f"Enter model id as provider/model{hint}", suggested)
        model = raw.strip()
        error = _model_error(model, provider_name)
        if not error:
            return model
        io.error(error)


def _model_error(model: str, provider_name: str) -> str:
    if not model:
        return "a model id is required (for example: provider/model-id)"
    if "/" not in model:
        return "model must use the provider/model format"
    prefix, _, rest = model.partition("/")
    if prefix != provider_name:
        return f"model must start with '{provider_name}/'"
    if not rest.strip():
        return "model id is empty after the slash"
    return ""


def _list_ollama_models(io: BootstrapIO, environ: dict[str, str]) -> list[str]:
    base = environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").strip().rstrip("/")
    if not base:
        base = "http://127.0.0.1:11434"
    try:
        response = httpx.get(f"{base}/v1/models", timeout=5.0)
    except httpx.HTTPError:
        io.warn(f"cannot reach Ollama at {base}; enter the model id manually")
        return []
    if response.status_code != 200:
        io.warn(f"Ollama replied with HTTP {response.status_code}; enter the model id manually")
        return []
    try:
        payload = response.json()
    except ValueError:
        return []
    models = [str(item.get("id", "")) for item in payload.get("data", []) if item.get("id")]
    return sorted(models)


def _ask_api_key(io: BootstrapIO, spec: ProviderSpec) -> str:
    while True:
        first = io.hidden(f"Enter API key ({spec.api_key_env})")
        if not first:
            io.error("empty key; the provider will stay unconfigured")
            return ""
        problem = validate_secret_format(spec.api_key_env, first)
        if problem:
            io.error(problem)
            continue
        second = io.hidden("Confirm API key")
        if first == second:
            return first
        io.error("keys do not match; try again")


async def _probe(config: Config, model: str, environ: dict[str, str]) -> tuple[bool, str]:
    try:
        ref = ModelRef.parse(model)
    except EurusError as exc:
        return False, str(exc)
    from eurus.providers.httpx_transport import HttpxTransport

    transport = HttpxTransport(timeout=PROBE_TIMEOUT_SECONDS)
    try:
        result = await probe_model(config, ref, transport=transport, environ=environ)
    finally:
        await transport.aclose()
    return result.ok, result.detail


def test_provider_model(config: Config, model: str, environ: dict[str, str]) -> tuple[bool, str]:
    return asyncio.run(_probe(config, model, environ))


def run_provider_setup(
    io: BootstrapIO,
    config: Config | None,
    environ: dict[str, str],
    *,
    allow_test: bool = True,
) -> ProviderSetupResult:
    io.console.print("[bold]AI PROVIDER CONFIGURATION[/bold]")
    io.echo()
    provider_name = choose_provider(io)
    spec = _spec_for(provider_name, environ)
    result = ProviderSetupResult(provider=provider_name, key_env=spec.api_key_env)

    key_value = ""
    if spec.api_key_env:
        current = environ.get(spec.api_key_env, "").strip()
        if current:
            io.info(f"{spec.api_key_env} is already configured ({mask_secret(current)})")
            if not io.confirm("Replace the stored key?", False):
                key_value = current
            else:
                key_value = _ask_api_key(io, spec)
        else:
            key_value = _ask_api_key(io, spec)
    else:
        base_url = io.ask(
            "Ollama base URL", environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        )
        if base_url and base_url.rstrip("/") != "http://127.0.0.1:11434":
            result.base_url_changed = True
            key_value = ""

    model = _ask_model(io, provider_name, environ, _existing_model(config))
    result.model = model

    fallback: list[str] = []
    if io.confirm("Configure a fallback model?", False):
        fallback_model = io.ask("Fallback model (provider/model)", "")
        if fallback_model.strip() and "/" in fallback_model:
            fallback.append(fallback_model.strip())

    if spec.api_key_env and key_value:
        result.key_saved = _save_env_values(io, environ, {spec.api_key_env: key_value})
    elif provider_name == "ollama":
        env_updates: dict[str, str] = {}
        base_url = environ.get("OLLAMA_BASE_URL", "").strip()
        requested = io.ask(
            "Save Ollama base URL to global .env?", base_url or "http://127.0.0.1:11434"
        )
        if requested.strip():
            env_updates["OLLAMA_BASE_URL"] = requested.strip()
        if env_updates:
            result.base_url_changed = _save_env_values(io, environ, env_updates)

    if fallback and io.confirm("Save model and fallback to config.toml?", True):
        _save_model_config(io, environ, model, tuple(fallback))
    elif io.confirm(f"Save {model} as the default model in config.toml?", True):
        _save_model_config(io, environ, model, ())
        io.info(f"model saved: {model}")

    result.fallback = tuple(fallback)

    if allow_test and io.confirm("Test provider connection now?", True):
        if config is None:
            result.detail = "config unavailable; save and re-run eurus doctor --full"
        else:
            ok, detail = test_provider_model(config, model, environ)
            result.tested = True
            result.test_ok = ok
            result.detail = detail
            if ok:
                io.ok(f"connection test passed ({detail})")
            else:
                io.error(f"connection test failed: {detail}")
    return result


def _save_env_values(io: BootstrapIO, environ: dict[str, str], updates: dict[str, str]) -> bool:
    path = global_env_path(environ)
    env = EnvFile.load(path)
    before = env.snapshot()
    for name, value in updates.items():
        env.set(name, value)
    changed = env.values != before
    env.save()
    for name in updates:
        environ[name] = updates[name]
    if changed:
        io.ok(f"saved to {path} (permissions 0600)")
    return changed


def _save_model_config(
    io: BootstrapIO, environ: dict[str, str], model: str, fallback: tuple[str, ...]
) -> None:
    from eurus import paths

    path = paths.user_config_path(environ)
    manager = TomlManager.load(path)
    updates: dict[str, Any] = {"agent": {"model": model}}
    if fallback:
        updates["agent"]["fallback"] = list(fallback)
    manager.merge(updates)
    try:
        manager.save(environ=environ)
    except Exception as exc:
        io.error(f"cannot update {path}: {exc}")
        return
    io.ok(f"saved to {path}")
