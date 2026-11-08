from __future__ import annotations

from dataclasses import dataclass

from eurus.research.settings import JOB_RETENTION_DEFAULT_DAYS, PROVIDER_NAMES, RESEARCH_MODES


@dataclass(frozen=True, slots=True)
class ResearchConfig:
    enabled: bool = True
    default_mode: str = "standard"
    providers: tuple[str, ...] = ("ddgs",)
    provider_timeout_seconds: float = 20.0
    provider_retries: int = 1
    fetch_timeout_seconds: float = 15.0
    max_fetch_mb: int = 2
    max_content_chars: int = 12_000
    deep_max_rounds: int = 3
    deep_max_queries: int = 12
    deep_max_sources: int = 16
    deep_max_fetches: int = 10
    deep_deadline_seconds: float = 240.0
    deep_coverage_target: float = 0.8
    browser_fallback: bool = True
    browser_fetch_budget: int = 2
    cache_ttl_seconds: float = 900.0
    cache_max_entries: int = 256
    job_retention_days: int = JOB_RETENTION_DEFAULT_DAYS

    def __post_init__(self) -> None:
        if self.default_mode not in RESEARCH_MODES:
            raise ValueError(f"unknown research mode: {self.default_mode}")
        unknown = [name for name in self.providers if name not in PROVIDER_NAMES]
        if unknown:
            raise ValueError(f"unknown search provider(s): {', '.join(unknown)}")
        if not self.providers:
            raise ValueError("research.providers must not be empty")
        if not 3.0 <= self.provider_timeout_seconds <= 120.0:
            raise ValueError("research.provider_timeout_seconds must be between 3 and 120")
        if not 0 <= self.provider_retries <= 3:
            raise ValueError("research.provider_retries must be between 0 and 3")
        if not 3.0 <= self.fetch_timeout_seconds <= 120.0:
            raise ValueError("research.fetch_timeout_seconds must be between 3 and 120")
        if not 1 <= self.max_fetch_mb <= 20:
            raise ValueError("research.max_fetch_mb must be between 1 and 20")
        if not 1_000 <= self.max_content_chars <= 200_000:
            raise ValueError("research.max_content_chars is out of range")
        if not 1 <= self.deep_max_rounds <= 10:
            raise ValueError("research.deep_max_rounds must be between 1 and 10")
        if not 1 <= self.deep_max_queries <= 60:
            raise ValueError("research.deep_max_queries must be between 1 and 60")
        if not 1 <= self.deep_max_sources <= 60:
            raise ValueError("research.deep_max_sources must be between 1 and 60")
        if not 1 <= self.deep_max_fetches <= 60:
            raise ValueError("research.deep_max_fetches must be between 1 and 60")
        if not 30.0 <= self.deep_deadline_seconds <= 3600.0:
            raise ValueError("research.deep_deadline_seconds must be between 30 and 3600")
        if not 0.3 <= self.deep_coverage_target <= 1.0:
            raise ValueError("research.deep_coverage_target must be between 0.3 and 1")
        if not 0 <= self.browser_fetch_budget <= 10:
            raise ValueError("research.browser_fetch_budget must be between 0 and 10")
        if not 30.0 <= self.cache_ttl_seconds <= 86_400.0:
            raise ValueError("research.cache_ttl_seconds is out of range")
        if not 8 <= self.cache_max_entries <= 4096:
            raise ValueError("research.cache_max_entries is out of range")
        if not 1 <= self.job_retention_days <= 3650:
            raise ValueError("research.job_retention_days is out of range")
