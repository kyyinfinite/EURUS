from __future__ import annotations

from dataclasses import dataclass

RESEARCH_MODES = ("quick", "standard", "deep")
PROVIDER_NAMES = ("ddgs", "brave", "tavily", "serpapi")
JOB_RETENTION_DEFAULT_DAYS = 30


@dataclass(frozen=True, slots=True)
class ResearchSettings:
    enabled: bool = True
    default_mode: str = "standard"
    provider_names: tuple[str, ...] = ("ddgs",)
    provider_timeout_seconds: float = 20.0
    provider_retries: int = 1
    fetch_timeout_seconds: float = 15.0
    max_fetch_bytes: int = 2_000_000
    max_content_chars: int = 12_000
    quick_max_results: int = 10
    standard_max_supporting: int = 2
    deep_max_rounds: int = 3
    deep_max_queries: int = 12
    deep_max_sources: int = 16
    deep_max_fetches: int = 10
    deep_deadline_seconds: float = 240.0
    deep_coverage_target: float = 0.8
    deep_max_contradictions: int = 12
    browser_fallback: bool = True
    browser_fetch_budget: int = 2
    browser_min_text_chars: int = 200
    cache_ttl_seconds: float = 900.0
    cache_max_entries: int = 256
    per_source_content_chars: int = 5_000
    ranking_domain_cap: int = 2
    domain_diversity_min: float = 0.5
    evidence_max_excerpt_chars: int = 600
    citation_relevance_min: float = 0.34
    claim_term_overlap_direct: float = 0.6
    claim_term_overlap_partial: float = 0.3
    max_source_chars: int = 400_000
    job_retention_days: int = JOB_RETENTION_DEFAULT_DAYS
    persist_standard: bool = True
    persist_deep: bool = True

    def __post_init__(self) -> None:
        if self.default_mode not in RESEARCH_MODES:
            raise ValueError(f"unknown research mode: {self.default_mode}")
        unknown = [name for name in self.provider_names if name not in PROVIDER_NAMES]
        if unknown:
            raise ValueError(f"unknown search provider(s): {', '.join(unknown)}")
        if not 3.0 <= self.provider_timeout_seconds <= 120.0:
            raise ValueError("provider_timeout_seconds must be between 3 and 120")
        if not 0 <= self.provider_retries <= 3:
            raise ValueError("provider_retries must be between 0 and 3")
        if not 3.0 <= self.fetch_timeout_seconds <= 120.0:
            raise ValueError("fetch_timeout_seconds must be between 3 and 120")
        if not 100_000 <= self.max_fetch_bytes <= 20_000_000:
            raise ValueError("max_fetch_bytes is out of range")
        if not 1_000 <= self.max_content_chars <= 200_000:
            raise ValueError("max_content_chars is out of range")
        if not 1 <= self.quick_max_results <= 10:
            raise ValueError("quick_max_results must be between 1 and 10")
        if not 0 <= self.standard_max_supporting <= 4:
            raise ValueError("standard_max_supporting must be between 0 and 4")
        if not 1 <= self.deep_max_rounds <= 10:
            raise ValueError("deep_max_rounds must be between 1 and 10")
        if not 1 <= self.deep_max_queries <= 60:
            raise ValueError("deep_max_queries must be between 1 and 60")
        if not 1 <= self.deep_max_sources <= 60:
            raise ValueError("deep_max_sources must be between 1 and 60")
        if not 1 <= self.deep_max_fetches <= 60:
            raise ValueError("deep_max_fetches must be between 1 and 60")
        if not 30.0 <= self.deep_deadline_seconds <= 3600.0:
            raise ValueError("deep_deadline_seconds must be between 30 and 3600")
        if not 0.3 <= self.deep_coverage_target <= 1.0:
            raise ValueError("deep_coverage_target must be between 0.3 and 1")
        if not 0 <= self.browser_fetch_budget <= 10:
            raise ValueError("browser_fetch_budget must be between 0 and 10")
        if not 0 <= self.browser_min_text_chars <= 5000:
            raise ValueError("browser_min_text_chars is out of range")
        if not 30.0 <= self.cache_ttl_seconds <= 86_400.0:
            raise ValueError("cache_ttl_seconds is out of range")
        if not 8 <= self.cache_max_entries <= 4096:
            raise ValueError("cache_max_entries is out of range")
        if not 200 <= self.per_source_content_chars <= 50_000:
            raise ValueError("per_source_content_chars is out of range")
        if not 1 <= self.ranking_domain_cap <= 10:
            raise ValueError("ranking_domain_cap must be between 1 and 10")
        if not 0.0 <= self.domain_diversity_min <= 1.0:
            raise ValueError("domain_diversity_min must be between 0 and 1")
        if not 100 <= self.evidence_max_excerpt_chars <= 4000:
            raise ValueError("evidence_max_excerpt_chars is out of range")
        if not 0.05 <= self.citation_relevance_min <= 0.9:
            raise ValueError("citation_relevance_min is out of range")
        if not 0.05 <= self.claim_term_overlap_direct <= 1.0:
            raise ValueError("claim_term_overlap_direct is out of range")
        if not 0.01 <= self.claim_term_overlap_partial <= 1.0:
            raise ValueError("claim_term_overlap_partial is out of range")
        if not 1000 <= self.max_source_chars <= 5_000_000:
            raise ValueError("max_source_chars is out of range")
        if not 1 <= self.job_retention_days <= 3650:
            raise ValueError("job_retention_days is out of range")
        if not 0.0 < self.claim_term_overlap_partial <= self.claim_term_overlap_direct <= 1.0:
            raise ValueError("claim term overlap thresholds are inconsistent")
