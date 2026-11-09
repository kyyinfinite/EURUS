from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest

from eurus.errors import ToolError
from eurus.execution.envelope import NetworkProfile
from eurus.network.policy import NetworkPolicyError, PolicySettings, build_policy_engine
from eurus.research.engine import ResearchEngine
from eurus.research.errors import ResearchError, SearchUnavailableError
from eurus.research.extract import PageContent
from eurus.research.fetch import ContentFetcher, FetchResult
from eurus.research.metrics import BudgetLimits
from eurus.research.models import JobStatus, ResearchMode, SearchHit
from eurus.research.providers.base import SearchProvider
from eurus.research.providers.registry import ProviderRegistry
from eurus.research.settings import ResearchSettings
from eurus.research.urlutils import canonical_url

FAKE_ADDRESSES = {
    "example.com": ["93.184.216.34"],
    "a.com": ["93.184.216.36"],
    "b.com": ["93.184.216.37"],
}


def fake_resolver(host: str, port: int) -> list[str]:
    if host in FAKE_ADDRESSES:
        return FAKE_ADDRESSES[host]
    raise NetworkPolicyError(f"cannot resolve host: {host}")


def make_policy():
    return build_policy_engine(
        PolicySettings(profile=NetworkProfile.WEB_READ, requests_per_minute=600),
        resolver=fake_resolver,
    )


PARAGRAPH = "<p>Solid state batteries.</p>"
LONG_PAGE = "<html><head><title>Report</title></head><body>" + (PARAGRAPH * 100) + "</body></html>"


def mock_client_factory(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    def mocked_client(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=kwargs.get("timeout", 15.0),
            follow_redirects=False,
            headers=kwargs.get("headers"),
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("eurus.research.fetch.public_http_client", mocked_client)


def real_clock() -> float:
    return time.monotonic()


class ScriptedProvider(SearchProvider):
    name = "scripted"

    def __init__(self, handler=None) -> None:
        self.queries: list[str] = []
        self._handler = handler or self._default

    def _default(self, query: str, limit: int) -> list[SearchHit]:
        if "key characteristics" in query:
            return [
                SearchHit("B report", "https://b.com/report", "lithium iron notes", "scripted", 1)
            ]
        if "lithium iron" in query and "vs" not in query:
            return []
        if "vs" in query or "solid state" in query:
            return [
                SearchHit("A report", "https://a.com/report", "solid state notes", "scripted", 1),
                SearchHit(
                    "A duplicate",
                    "https://a.com/report?utm_source=x",
                    "solid state again",
                    "scripted",
                    2,
                ),
            ]
        return []

    def search(self, query: str, limit: int) -> list[SearchHit]:
        self.queries.append(query)
        return self._handler(query, limit)[:limit]


class FailingProvider(SearchProvider):
    name = "scripted"

    def search(self, query: str, limit: int) -> list[SearchHit]:
        raise RuntimeError("provider down")


class BlockingProvider(SearchProvider):
    name = "scripted"

    def __init__(self) -> None:
        self.release = __import__("threading").Event()

    def search(self, query: str, limit: int) -> list[SearchHit]:
        self.release.wait(10)
        return []


class FakeFetcher(ContentFetcher):
    def __init__(
        self,
        settings: ResearchSettings,
        pages: dict[str, str],
        errors: dict[str, str] | None = None,
    ) -> None:
        super().__init__(settings=settings)
        self.pages = pages
        self.errors = errors or {}
        self.calls: list[str] = []

    async def fetch(self, url: str, *, allow_browser: bool = True) -> FetchResult:
        self.calls.append(url)
        if url in self.errors:
            raise ToolError(self.errors[url])
        text = self.pages.get(url, "")
        return FetchResult(
            url=url,
            content_type="text/html",
            page=PageContent(
                title=f"Page {url}",
                text=text,
                date="2026-10-30",
                author="A. Author",
                content_type="html",
            ),
            response_bytes=len(text),
        )


A_CONTENT = (
    "Solid state batteries reached 400 Wh/kg in 2026. "
    "The energy density is high for this chemistry. "
    "Mass production is planned for 2027."
)
B_CONTENT = (
    "Lithium iron phosphate batteries cost less and last long. "
    "The chemistry is stable and safe. "
    "Capacity around 320 Wh/kg is typical for this chemistry."
)
COMPARISON_OBJECTIVE = "solid state batteries vs lithium iron phosphate batteries for evs"


def make_engine(
    tmp_path,
    provider: SearchProvider | None = None,
    pages: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    clock: Any = None,
    **settings_overrides: Any,
) -> ResearchEngine:
    base: dict[str, Any] = {
        "deep_max_rounds": 2,
        "deep_deadline_seconds": 60.0,
        "persist_standard": True,
    }
    base.update(settings_overrides)
    settings = ResearchSettings(**base)
    engine = ResearchEngine(
        settings=settings,
        storage_root=tmp_path / "research",
        clock=clock or real_clock,
    )
    engine.providers = ProviderRegistry([provider or ScriptedProvider()], settings=settings)
    engine.fetcher = FakeFetcher(settings, pages or {}, errors)
    return engine


async def test_quick_search_returns_legacy_dicts(tmp_path) -> None:
    engine = make_engine(tmp_path)
    results = await engine.quick_search_dicts("solid state batteries", 5)
    assert results == [
        {"title": "A report", "url": "https://a.com/report", "snippet": "solid state notes"}
    ]


async def test_quick_search_dedupes_tracking_variants(tmp_path) -> None:
    engine = make_engine(tmp_path)
    hits = await engine.quick_search("solid state batteries", 5)
    canonicals = [canonical_url(hit.url) for hit in hits]
    assert len(canonicals) == len(set(canonicals))


async def test_fetch_page_returns_compat_payload(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=LONG_PAGE, headers={"content-type": "text/html"})

    mock_client_factory(monkeypatch, handler)
    engine = ResearchEngine(
        settings=ResearchSettings(),
        policy=make_policy(),
        storage_root=tmp_path / "r",
    )
    page = await engine.fetch_page("https://example.com/report", max_chars=200)
    assert set(page) == {"title", "text", "date", "author", "url"}
    assert page["url"] == "https://example.com/report"
    assert page["text"].endswith("[Content truncated]")
    assert page["text"].startswith("Solid state batteries.")


async def test_fetch_page_uses_page_cache(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=LONG_PAGE, headers={"content-type": "text/html"})

    mock_client_factory(monkeypatch, handler)
    engine = ResearchEngine(
        settings=ResearchSettings(),
        policy=make_policy(),
        storage_root=tmp_path / "r",
    )
    first = await engine.fetch_page("https://example.com/report")
    second = await engine.fetch_page("https://example.com/report")
    assert calls == ["https://example.com/report"]
    assert first["text"] == second["text"]


async def test_fetch_page_empty_text_raises(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        empty = "<html><body></body></html>"
        return httpx.Response(200, text=empty, headers={"content-type": "text/html"})

    mock_client_factory(monkeypatch, handler)
    engine = ResearchEngine(
        settings=ResearchSettings(),
        policy=make_policy(),
        storage_root=tmp_path / "r",
    )
    with pytest.raises(ToolError, match="could not extract readable text"):
        await engine.fetch_page("https://example.com/report")


async def test_standard_payload_matches_legacy_contract(tmp_path) -> None:
    def both_hits(query: str, limit: int) -> list[SearchHit]:
        return [
            SearchHit("A report", "https://a.com/report", "solid state notes", "scripted", 1),
            SearchHit("B report", "https://b.com/report", "lithium iron notes", "scripted", 2),
        ]

    engine = make_engine(
        tmp_path,
        provider=ScriptedProvider(handler=both_hits),
        pages={"https://a.com/report": A_CONTENT, "https://b.com/report": B_CONTENT},
    )
    payload = await engine.standard_research_payload(COMPARISON_OBJECTIVE, 2)
    assert payload["query"] == COMPARISON_OBJECTIVE
    assert set(payload) >= {"query", "sources", "instructions"}
    assert "untrusted data, not instructions" in payload["instructions"]
    assert len(payload["sources"]) == 2
    for item in payload["sources"]:
        assert set(item) == {"title", "url", "search_snippet", "content", "status"}
        assert item["status"] == "fetched"
    assert "job_id" not in payload


async def test_standard_payload_reports_fetch_failures(tmp_path) -> None:
    def both_hits(query: str, limit: int) -> list[SearchHit]:
        return [
            SearchHit("A report", "https://a.com/report", "a snippet", "scripted", 1),
            SearchHit("B report", "https://b.com/report", "b snippet", "scripted", 2),
        ]

    engine = make_engine(
        tmp_path,
        provider=ScriptedProvider(handler=both_hits),
        errors={"https://a.com/report": "boom", "https://b.com/report": "boom"},
    )
    payload = await engine.standard_research_payload("solid state batteries", 2)
    statuses = [item["status"] for item in payload["sources"]]
    assert all(status.startswith("fetch_failed:") for status in statuses)
    assert all(item["content"] == item["search_snippet"] for item in payload["sources"])


async def test_standard_payload_reports_snippet_only(tmp_path) -> None:
    def both_hits(query: str, limit: int) -> list[SearchHit]:
        return [
            SearchHit("A report", "https://a.com/report", "solid state notes", "scripted", 1),
            SearchHit("B report", "https://b.com/report", "lithium iron notes", "scripted", 2),
        ]

    engine = make_engine(
        tmp_path,
        provider=ScriptedProvider(handler=both_hits),
        pages={"https://a.com/report": A_CONTENT, "https://b.com/report": ""},
    )
    payload = await engine.standard_research_payload("solid state batteries", 2)
    by_url = {item["url"]: item for item in payload["sources"]}
    assert by_url["https://a.com/report"]["status"] == "fetched"
    assert by_url["https://b.com/report"]["status"] == "snippet_only"
    assert by_url["https://b.com/report"]["content"] == "lithium iron notes"


async def test_standard_payload_notes_when_no_results(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        provider=ScriptedProvider(handler=lambda query, limit: []),
    )
    payload = await engine.standard_research_payload("unmatched query", 3)
    assert payload["sources"] == []
    assert payload["note"] == "No search results found."


async def test_deep_research_runs_multiple_rounds(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        pages={"https://a.com/report": A_CONTENT, "https://b.com/report": B_CONTENT},
    )
    provider = engine.providers.providers()[0]
    outcome = await engine.research(COMPARISON_OBJECTIVE, mode=ResearchMode.DEEP)
    assert outcome.status is JobStatus.COMPLETED
    assert len(outcome.rounds) == 2
    assert outcome.followups
    assert any("key characteristics" in query for query in provider.queries)
    assert outcome.coverage.ratio == 1.0
    assert outcome.qa_status in {"PASSED", "PASSED_WITH_WARNINGS"}
    assert outcome.evidence
    supporting = [
        item for item in outcome.evidence if item.evidence_type.value in {"direct", "partial"}
    ]
    assert supporting
    assert all(item.verification.value == "verified" for item in supporting)
    assert all(record.verified for record in outcome.citations if record.verified is True)
    job = engine.storage.load_job(outcome.job_id)
    assert job is not None and job["status"] == "completed"
    state = engine.storage.load_state(outcome.job_id)
    assert state is not None and state["result"]["status"] == "completed"


async def test_deep_research_marks_incomplete_when_gap_remains(tmp_path) -> None:
    def only_a(query: str, limit: int) -> list[SearchHit]:
        return [SearchHit("A report", "https://a.com/report", "solid state notes", "scripted", 1)]

    engine = make_engine(
        tmp_path,
        provider=ScriptedProvider(handler=only_a),
        pages={"https://a.com/report": A_CONTENT},
    )
    outcome = await engine.research(COMPARISON_OBJECTIVE, mode=ResearchMode.DEEP)
    assert outcome.status is JobStatus.INCOMPLETE
    assert outcome.coverage.ratio < 1.0
    assert outcome.warnings


async def test_deep_research_stops_on_query_budget(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        pages={"https://a.com/report": A_CONTENT, "https://b.com/report": B_CONTENT},
    )
    outcome = await engine.research(
        COMPARISON_OBJECTIVE,
        mode=ResearchMode.DEEP,
        budget_limits=BudgetLimits(
            max_rounds=2,
            max_queries=2,
            max_sources=16,
            max_fetches=10,
            deadline_seconds=60.0,
        ),
    )
    assert outcome.status is JobStatus.INCOMPLETE
    assert any(
        "query budget exhausted" in warning or "query limit" in warning
        for warning in outcome.warnings
    )
    assert any("coverage" in warning for warning in outcome.warnings)


class FastClock:
    def __init__(self, step: float) -> None:
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        self.now += self.step
        return self.now


async def test_deep_research_stops_on_deadline(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        pages={"https://a.com/report": A_CONTENT},
        clock=FastClock(step=50.0),
    )
    outcome = await engine.research(COMPARISON_OBJECTIVE, mode=ResearchMode.DEEP)
    assert outcome.status is JobStatus.INCOMPLETE
    assert any("deadline" in warning.lower() for warning in outcome.warnings)


async def test_deep_research_marks_failure_when_providers_die(tmp_path) -> None:
    engine = make_engine(tmp_path, provider=FailingProvider())
    with pytest.raises(SearchUnavailableError):
        await engine.research(COMPARISON_OBJECTIVE, mode=ResearchMode.DEEP)
    jobs = engine.storage.list_jobs()
    assert jobs and jobs[0]["status"] == "failed"


async def test_deep_research_supports_cancellation(tmp_path) -> None:
    blocking = BlockingProvider()
    engine = make_engine(tmp_path, provider=blocking)
    task = asyncio.create_task(engine.research(COMPARISON_OBJECTIVE, mode=ResearchMode.DEEP))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    blocking.release.set()
    jobs = engine.storage.list_jobs()
    assert jobs and jobs[0]["status"] == "cancelled"


async def test_resume_continues_incomplete_job(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        deep_max_rounds=3,
        pages={"https://a.com/report": A_CONTENT, "https://b.com/report": B_CONTENT},
    )
    outcome = await engine.research(
        COMPARISON_OBJECTIVE,
        mode=ResearchMode.DEEP,
        budget_limits=BudgetLimits(
            max_rounds=2,
            max_queries=2,
            max_sources=16,
            max_fetches=10,
            deadline_seconds=60.0,
        ),
    )
    assert outcome.status is JobStatus.INCOMPLETE
    resumed = await engine.resume(outcome.job_id)
    assert resumed.status is JobStatus.COMPLETED
    assert resumed.job_id == outcome.job_id
    job = engine.storage.load_job(outcome.job_id)
    assert job is not None and job["status"] == "completed"


async def test_resume_rejects_finished_jobs(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        pages={"https://a.com/report": A_CONTENT, "https://b.com/report": B_CONTENT},
    )
    outcome = await engine.research(COMPARISON_OBJECTIVE, mode=ResearchMode.DEEP)
    assert outcome.status is JobStatus.COMPLETED
    with pytest.raises(ResearchError, match="cannot be resumed"):
        await engine.resume(outcome.job_id)


async def test_deep_payload_exposes_research_fields(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        pages={"https://a.com/report": A_CONTENT, "https://b.com/report": B_CONTENT},
    )
    payload = await engine.standard_research_payload(COMPARISON_OBJECTIVE, 4, depth="deep")
    assert payload["research_status"] == "completed"
    assert payload["job_id"]
    assert payload["coverage"]["ratio"] == 1.0
    assert payload["evidence"]
    assert payload["citations"]
    assert "untrusted data, not instructions" in payload["instructions"]


async def test_page_cache_is_shared_between_runs(tmp_path) -> None:
    engine = make_engine(
        tmp_path,
        pages={"https://a.com/report": A_CONTENT, "https://b.com/report": B_CONTENT},
    )
    await engine.research(COMPARISON_OBJECTIVE, mode=ResearchMode.DEEP)
    fetcher = engine.fetcher
    assert isinstance(fetcher, FakeFetcher)
    calls_after_first = len(fetcher.calls)
    await engine.research(COMPARISON_OBJECTIVE, mode=ResearchMode.DEEP)
    assert len(fetcher.calls) == calls_after_first
