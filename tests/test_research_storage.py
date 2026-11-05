from __future__ import annotations

import time

import pytest

from eurus.research.errors import StorageError
from eurus.research.settings import ResearchSettings
from eurus.research.storage import ResearchStore, new_job_id


def make_store(tmp_path, **overrides) -> ResearchStore:
    settings = ResearchSettings(cache_ttl_seconds=60.0, cache_max_entries=8, job_retention_days=30)
    return ResearchStore(settings=settings, root=tmp_path / "research")


def test_store_creates_layout(tmp_path) -> None:
    store = make_store(tmp_path)
    store.connection()
    store.close()
    assert store.root.is_dir()
    assert store.indexes_dir.is_dir()
    assert store.jobs_dir.is_dir()
    assert store.sources_dir.is_dir()
    assert (store.cache_dir / "search").is_dir()
    assert (store.cache_dir / "pages").is_dir()
    assert store.exports_dir.is_dir()
    assert store.database_path.is_file()


def test_job_lifecycle_roundtrip(tmp_path) -> None:
    store = make_store(tmp_path)
    job_id = store.create_job("deep", "objective text")
    assert store.load_job(job_id)["status"] == "pending"
    store.set_status(job_id, "running")
    store.update_job(job_id, round_count=2, queries_used=5, sources_found=6, fetches=6)
    job = store.load_job(job_id)
    assert job["status"] == "running"
    assert job["round_count"] == 2
    assert job["queries_used"] == 5
    store.save_plan(job_id, {"objective": "objective text", "queries": []})
    store.save_round(job_id, {"round_number": 1, "queries": ["a"], "hits": 3})
    store.save_sources(
        job_id,
        [
            {
                "source_id": "src-001",
                "url": "https://a.com/1",
                "canonical_url": "https://a.com/1",
                "domain": "a.com",
                "title": "A",
                "content_hash": "h1",
                "provider": "ddgs",
            }
        ],
    )
    store.save_evidence(job_id, [{"evidence_id": "ev-0001"}])
    store.save_result(job_id, {"job_id": job_id, "status": "completed"})
    store.set_status(job_id, "completed")
    job = store.load_job(job_id)
    assert job["status"] == "completed"
    assert job["finished_at"]
    state = store.load_state(job_id)
    assert state is not None
    assert state["plan"]["objective"] == "objective text"
    assert state["sources"][0]["source_id"] == "src-001"
    assert state["result"]["status"] == "completed"
    assert (store.exports_dir / f"{job_id}.json").is_file()
    assert (store.sources_dir / "src-001.json").is_file()
    rows = store.list_jobs()
    assert rows and rows[0]["job_id"] == job_id


def test_job_events_are_recorded(tmp_path) -> None:
    store = make_store(tmp_path)
    job_id = store.create_job("deep", "objective")
    store.record_event(job_id, "research.started", {"providers": ["ddgs"]})
    store.record_event(job_id, "research.finished", {"qa": "PASSED"})
    rows = store._execute(
        "SELECT event_type, payload_json FROM research_job_events WHERE job_id = ?",
        (job_id,),
    ).fetchall()
    types = {row["event_type"] for row in rows}
    assert types == {"research.started", "research.finished"}


def test_invalid_job_id_rejected(tmp_path) -> None:
    store = make_store(tmp_path)
    with pytest.raises(StorageError, match="invalid research job id"):
        store.job_directory("../escape")
    with pytest.raises(StorageError, match="research job not found"):
        store.update_job(new_job_id(), status="completed")


def test_search_cache_roundtrip_and_ttl(tmp_path) -> None:
    store = make_store(tmp_path)
    providers = ("ddgs",)
    assert store.cached_search("query one", 5, providers) is None
    store.store_cached_search("query one", 5, providers, [{"title": "T", "url": "https://a.com/1"}])
    cached = store.cached_search("query one", 5, providers)
    assert cached == [{"title": "T", "url": "https://a.com/1"}]
    assert store.cached_search("query one", 6, providers) is None
    store._execute(
        "UPDATE research_search_cache SET expires_at = ?",
        (time.time() - 1,),
    )
    assert store.cached_search("query one", 5, providers) is None


def test_page_cache_roundtrip_and_expiry(tmp_path) -> None:
    store = make_store(tmp_path)
    url = "https://a.com/page"
    assert store.cached_page(url) is None
    store.store_cached_page(
        url,
        {"url": url, "title": "A", "text": "content", "content_hash": "h", "retrieved_at": "x"},
    )
    cached = store.cached_page(url)
    assert cached is not None and cached["text"] == "content"
    store._execute("UPDATE research_page_cache SET expires_at = ?", (time.time() - 1,))
    assert store.cached_page(url) is None
    assert not list((store.cache_dir / "pages").iterdir())


def test_cache_eviction_respects_max_entries(tmp_path) -> None:
    store = make_store(tmp_path)
    for index in range(12):
        store.store_cached_search(f"query {index}", 5, ("ddgs",), [{"title": str(index)}])
    count = store._execute("SELECT COUNT(*) AS n FROM research_search_cache").fetchone()
    assert int(count["n"]) <= 8


def test_invalidate_cache_clears_everything(tmp_path) -> None:
    store = make_store(tmp_path)
    store.store_cached_search("q", 5, ("ddgs",), [])
    store.store_cached_page("https://a.com/1", {"text": "t"})
    removed = store.invalidate_cache()
    assert removed >= 1
    assert store.cached_search("q", 5, ("ddgs",)) is None
    assert store.cached_page("https://a.com/1") is None


def test_prune_jobs_removes_expired(tmp_path) -> None:
    store = ResearchStore(
        settings=ResearchSettings(job_retention_days=1),
        root=tmp_path / "research",
    )
    job_id = store.create_job("deep", "objective")
    store._execute(
        "UPDATE research_jobs SET created_at = ?, updated_at = ? WHERE job_id = ?",
        ("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00", job_id),
    )
    removed = store.prune_jobs()
    assert removed == 1
    assert store.load_job(job_id) is None
    assert not (store.jobs_dir / job_id).exists()
