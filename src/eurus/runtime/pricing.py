from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eurus.models import Usage

BUILTIN_PRICES_UPDATED_AT = "2026-10-29"

_BUILTIN_TABLE: dict[str, dict[str, float]] = {
    "openai/gpt-4o": {"input_per_mtok": 2.50, "output_per_mtok": 10.00},
    "openai/gpt-4o-mini": {"input_per_mtok": 0.15, "output_per_mtok": 0.60},
    "anthropic/claude-sonnet-4-5": {"input_per_mtok": 3.00, "output_per_mtok": 15.00},
    "anthropic/claude-3-5-haiku-latest": {"input_per_mtok": 0.80, "output_per_mtok": 4.00},
    "groq/llama-3.3-70b-versatile": {"input_per_mtok": 0.59, "output_per_mtok": 0.79},
    "groq/llama-3.1-8b-instant": {"input_per_mtok": 0.05, "output_per_mtok": 0.08},
    "gemini/gemini-2.0-flash": {"input_per_mtok": 0.10, "output_per_mtok": 0.40},
    "nvidia/llama-3.1-nemotron-70b-instruct": {"input_per_mtok": 0.60, "output_per_mtok": 0.60},
    "nvidia/llama-3.3-nemotron-super-49b-v1": {"input_per_mtok": 0.40, "output_per_mtok": 0.40},
    "nvidia/nemotron-4-340b-instruct": {"input_per_mtok": 1.20, "output_per_mtok": 1.20},
}

MAX_PRICE_ENTRIES = 500
PRICES_FILE = "prices.toml"
UNKNOWN_COST = "unknown"


@dataclass(frozen=True, slots=True)
class ModelPrice:
    model: str
    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float | None = None
    currency: str = "USD"
    updated_at: str = ""

    def cost_for_usage(self, usage: Usage) -> float:
        cost = (
            usage.input_tokens * self.input_per_mtok + usage.output_tokens * self.output_per_mtok
        ) / 1_000_000
        if self.cached_input_per_mtok is not None:
            cost += usage.cache_read_tokens * self.cached_input_per_mtok / 1_000_000
        return cost


@dataclass(frozen=True, slots=True)
class PricingCatalog:
    prices: dict[str, ModelPrice] = field(default_factory=dict)
    source: str = "builtin"
    loaded_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "loaded_at", self.loaded_at or datetime.now(UTC).isoformat())

    def price_for(self, model_ref: str) -> ModelPrice | None:
        normalized = model_ref.strip()
        if not normalized:
            return None
        return self.prices.get(normalized) or self.prices.get(normalized.lower())

    def cost_for_usage(self, model_ref: str, usage: Usage) -> float | None:
        price = self.price_for(model_ref)
        if price is None:
            return None
        return price.cost_for_usage(usage)

    def cost_label(self, model_ref: str, usage: Usage) -> str:
        """Human-readable cost: a number for priced models, "unknown" otherwise.

        Unknown models are never reported as 0.0 — that would understate spend.
        """
        cost = self.cost_for_usage(model_ref, usage)
        if cost is None:
            return UNKNOWN_COST
        return f"{cost:.4f}"

    def describe(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "loaded_at": self.loaded_at,
            "models": sorted(self.prices),
        }


def _validate_price_entry(model: str, entry: Mapping[str, Any], where: str) -> ModelPrice:
    if not isinstance(entry, Mapping):
        raise ValueError(f"{where} must be a table")
    allowed = {
        "input_per_mtok",
        "output_per_mtok",
        "cached_input_per_mtok",
        "currency",
        "updated_at",
    }
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ValueError(f"{where} has unknown key(s): {', '.join(unknown)}")
    try:
        input_price = float(entry["input_per_mtok"])
        output_price = float(entry["output_per_mtok"])
    except KeyError as exc:
        raise ValueError(f"{where} is missing {exc.args[0]}") from exc
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{where} prices must be numbers") from exc
    if input_price < 0 or output_price < 0:
        raise ValueError(f"{where} prices cannot be negative")
    cached = entry.get("cached_input_per_mtok")
    if cached is not None:
        cached = float(cached)
        if cached < 0:
            raise ValueError(f"{where}.cached_input_per_mtok cannot be negative")
    currency = str(entry.get("currency", "USD"))
    updated_at = str(entry.get("updated_at", ""))
    return ModelPrice(
        model=model,
        input_per_mtok=input_price,
        output_per_mtok=output_price,
        cached_input_per_mtok=cached,
        currency=currency,
        updated_at=updated_at,
    )


def _load_prices_file(home: Path | None) -> dict[str, Any]:
    if home is None:
        return {}
    path = home / PRICES_FILE
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a table")
    return data


def build_pricing_catalog(
    configured: Mapping[str, Any] | None = None,
    *,
    home: Path | None = None,
) -> PricingCatalog:
    prices: dict[str, ModelPrice] = {}
    for model, entry in _BUILTIN_TABLE.items():
        prices[model] = ModelPrice(
            model=model,
            input_per_mtok=entry["input_per_mtok"],
            output_per_mtok=entry["output_per_mtok"],
            updated_at=BUILTIN_PRICES_UPDATED_AT,
        )
    source = "builtin"
    updated_at = BUILTIN_PRICES_UPDATED_AT
    file_data = _load_prices_file(home)
    file_models = file_data.get("models") if file_data else None
    if file_models is not None:
        if not isinstance(file_models, Mapping):
            raise ValueError(f"{PRICES_FILE}: models must be a table")
        if len(file_models) > MAX_PRICE_ENTRIES:
            raise ValueError(f"{PRICES_FILE}: models cannot exceed {MAX_PRICE_ENTRIES} entries")
        for model, entry in file_models.items():
            where = f"{PRICES_FILE}:models.{model}"
            price = _validate_price_entry(str(model), entry, where)
            prices[price.model] = price
            source = "file"
        file_updated = str(file_data.get("updated_at", "") or "")
        if file_updated:
            updated_at = file_updated
    if configured:
        models = configured.get("models")
        if models is not None:
            if not isinstance(models, Mapping):
                raise ValueError("pricing.models must be a table")
            if len(models) > MAX_PRICE_ENTRIES:
                raise ValueError(f"pricing.models cannot exceed {MAX_PRICE_ENTRIES} entries")
            for model, entry in models.items():
                where = f"pricing.models.{model}"
                price = _validate_price_entry(str(model), entry, where)
                prices[price.model] = price
                source = "config"
        override_updated = str(configured.get("updated_at", "") or "")
        if override_updated:
            updated_at = override_updated
    return PricingCatalog(prices=prices, source=source, loaded_at=updated_at)
