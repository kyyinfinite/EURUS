from pathlib import Path
from typing import Any

import pytest

from eurus.media.store import MediaStore
from eurus.memory.media_index import (
    MEDIA_MEMORY_CATEGORY,
    RECALL_MEDIA_TOOL_NAME,
    MediaMemoryIndexer,
    derive_media_memory_text,
    format_media_memory_row,
    media_marker_for_asset,
    media_memory_provenance,
    media_source,
    recall_media_tool,
)
from eurus.sessions import SessionStore
from eurus.tools.base import ToolContext

PNG_BYTES = (
    b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a\x00\x00\x00\x0d\x49\x48\x44\x52"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90\x77\x53"
    b"\xde\x00\x00\x00\x0c\x49\x44\x41\x54\x78\x9c\x63\xf8\xcf\xc0\x00"
    b"\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00\x49\x45\x4e"
    b"\x44\xae\x42\x60\x82"
)
MP4_BYTES = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomvideo-data"


def make_store(tmp_path: Path) -> MediaStore:
    return MediaStore(database=tmp_path / "db.sqlite", root=tmp_path / "media")


def make_indexer(
    tmp_path: Path, media: MediaStore | None = None, owner: str = "owner-1"
) -> MediaMemoryIndexer:
    return MediaMemoryIndexer(
        tmp_path / "db.sqlite",
        owner_id=owner,
        media_store=media,
    )


def make_context(**overrides: Any) -> ToolContext:
    values: dict[str, Any] = {"workspace": Path("/tmp"), "owner_id": "owner-1"}
    values.update(overrides)
    return ToolContext(**values)


def test_media_source_format() -> None:
    asset_id = "a" * 32
    assert media_source(asset_id) == f"media:{asset_id}"


def test_derive_audio_memory_text(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    media = store.save("s1", "plan.mp3", b"ID3" + b"\x00" * 64, owner_id="owner-1")
    store.mark_ready(
        media.id, {"duration_seconds": 91.0, "transcript": "kita akan rilis EURUS besok"}
    )
    refreshed = store.get(media.id)
    assert refreshed is not None
    text = derive_media_memory_text(refreshed)
    assert "audio plan.mp3 (audio/mpeg)" in text
    assert "Duration 1:31." in text
    assert "Transcript: kita akan rilis EURUS besok" in text


def test_derive_video_memory_text_includes_timestamps(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    media = store.save("s1", "tutorial.mp4", MP4_BYTES, owner_id="owner-1")
    store.mark_ready(
        media.id,
        {
            "duration_seconds": 125.0,
            "width": 320,
            "height": 240,
            "transcript": "python basics",
            "frames": [
                {"asset_id": "b" * 32, "timestamp_seconds": 30.0},
                {"asset_id": "c" * 32, "timestamp_seconds": 90.0},
            ],
        },
    )
    refreshed = store.get(media.id)
    assert refreshed is not None
    text = derive_media_memory_text(refreshed)
    assert "video tutorial.mp4 (video/mp4)" in text
    assert "Duration 2:05." in text
    assert "Keyframes at 0:30, 1:30." in text
    assert "Transcript: python basics" in text


def test_derive_document_memory_text_reads_inline_documents(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    notes = b"arsitektur agent EURUS menggunakan event driven design"
    media = store.save(
        "s1", "architecture.txt", notes, declared_mime="text/plain", owner_id="owner-1"
    )
    store.mark_ready(media.id, {})
    refreshed = store.get(media.id)
    assert refreshed is not None
    text = derive_media_memory_text(refreshed)
    assert "document architecture.txt (text/plain)" in text
    assert "arsitektur agent EURUS" in text


def test_derive_image_memory_text_uses_metadata_only(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    media = store.save("s1", "dashboard.png", PNG_BYTES, owner_id="owner-1")
    store.mark_ready(media.id, {"width": 640, "height": 480})
    refreshed = store.get(media.id)
    assert refreshed is not None
    text = derive_media_memory_text(refreshed)
    assert "image dashboard.png (image/png)" in text
    assert "Resolution 640x480." in text


def test_media_memory_provenance_shape(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    media = store.save("s1", "photo.png", PNG_BYTES, owner_id="owner-1")
    provenance = media_memory_provenance(media)
    assert provenance["asset_id"] == media.id
    assert provenance["kind"] == "image"
    assert provenance["sha256"] == media.sha256
    assert provenance["session_id"] == "s1"


def test_index_media_creates_searchable_memory(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    indexer = make_indexer(tmp_path, media=store)
    media = store.save("s1", "plan.mp3", b"ID3" + b"\x00" * 64, owner_id="owner-1")
    store.mark_ready(media.id, {"transcript": "rencana rilis produk minggu depan"})
    refreshed = store.get(media.id)
    assert refreshed is not None
    memory_id = indexer.index_media(refreshed)
    assert memory_id is not None
    rows = indexer.media_memories(media.id)
    assert len(rows) == 1
    assert rows[0]["category"] == MEDIA_MEMORY_CATEGORY
    assert rows[0]["source"] == media_source(media.id)
    assert rows[0]["owner_id"] == "owner-1"
    assert "rencana rilis produk" in rows[0]["content"]
    provenance = rows[0]["provenance"]
    assert "asset_id" in provenance
    assert media.id in provenance


def test_index_media_reindexes_without_duplicates(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    indexer = make_indexer(tmp_path, media=store)
    media = store.save("s1", "plan.mp3", b"ID3" + b"\x00" * 64, owner_id="owner-1")
    store.mark_ready(media.id, {"transcript": "first version"})
    first = store.get(media.id)
    assert first is not None
    indexer.index_media(first)
    store.mark_ready(media.id, {"transcript": "second version with more detail"})
    second = store.get(media.id)
    assert second is not None
    indexer.index_media(second)
    rows = indexer.media_memories(media.id)
    assert len(rows) == 1
    assert "second version" in rows[0]["content"]


def test_index_media_masks_secrets_before_storing(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    indexer = make_indexer(tmp_path, media=store)
    media = store.save("s1", "secret.mp3", b"ID3" + b"\x00" * 64, owner_id="owner-1")
    store.mark_ready(media.id, {})
    refreshed = store.get(media.id)
    assert refreshed is not None
    token = "sk-1234567890abcdef1234567890abcdef"
    refreshed.metadata["transcript"] = f"the key is {token} and the plan continues"
    memory_id = indexer.index_media(refreshed)
    assert memory_id is not None
    rows = indexer.media_memories(media.id)
    assert token not in rows[0]["content"]
    assert "REDACTED" in rows[0]["content"]
    assert "the plan continues" in rows[0]["content"]


def test_index_media_rejects_email_content(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    indexer = make_indexer(tmp_path, media=store)
    media = store.save("s1", "contact.mp3", b"ID3" + b"\x00" * 64, owner_id="owner-1")
    store.mark_ready(media.id, {})
    refreshed = store.get(media.id)
    assert refreshed is not None
    refreshed.metadata["transcript"] = "email me at user@example.com for details"
    memory_id = indexer.index_media(refreshed)
    assert memory_id is None
    assert indexer.media_memories(media.id) == []


def test_index_media_caption(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    indexer = make_indexer(tmp_path, media=store)
    media = store.save("s1", "dashboard.png", PNG_BYTES, owner_id="owner-1")
    memory_id = indexer.index_media_caption(media, "desain dashboard dengan tema copper")
    assert memory_id is not None
    rows = indexer.media_memories(media.id)
    assert "Caption: desain dashboard dengan tema copper" in rows[0]["content"]
    assert "vision_analysis" in rows[0]["provenance"]


def test_search_media_memories_by_transcript(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    indexer = make_indexer(tmp_path, media=store)
    audio = store.save("s1", "plan.mp3", b"ID3" + b"\x00" * 64, owner_id="owner-1")
    store.mark_ready(audio.id, {"transcript": "kita akan merilis EURUS versi multimodal"})
    video = store.save("s1", "demo.mp4", MP4_BYTES, owner_id="owner-1")
    store.mark_ready(video.id, {"transcript": "tutorial python dasar"})
    for media_id in (audio.id, video.id):
        refreshed = store.get(media_id)
        assert refreshed is not None
        indexer.index_media(refreshed)
    results = indexer.search_media_memories("EURUS")
    assert len(results) == 1
    assert results[0]["asset_id"] == audio.id
    assert results[0]["asset_available"] is True
    assert results[0]["asset_kind"] == "audio"
    python_results = indexer.search_media_memories("python")
    assert len(python_results) == 1
    assert python_results[0]["asset_id"] == video.id


def test_search_media_memories_isolates_owners(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    owner_one = MediaMemoryIndexer(tmp_path / "db.sqlite", owner_id="owner-one")
    owner_two = MediaMemoryIndexer(tmp_path / "db.sqlite", owner_id="owner-two")
    media = store.save("s1", "plan.mp3", b"ID3" + b"\x00" * 64, owner_id="owner-one")
    store.mark_ready(media.id, {"transcript": "rahasia owner satu"})
    refreshed = store.get(media.id)
    assert refreshed is not None
    owner_one.index_media(refreshed)
    results = owner_two.search_media_memories("rahasia")
    assert results == []
    own = owner_one.search_media_memories("rahasia")
    assert len(own) == 1


def test_forget_media_removes_memories(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    indexer = make_indexer(tmp_path, media=store)
    media = store.save("s1", "plan.mp3", b"ID3" + b"\x00" * 64, owner_id="owner-1")
    store.mark_ready(media.id, {"transcript": "hapus aku nanti"})
    refreshed = store.get(media.id)
    assert refreshed is not None
    indexer.index_media(refreshed)
    assert indexer.media_memories(media.id)
    removed = indexer.forget_media(media.id)
    assert removed == 1
    assert indexer.media_memories(media.id) == []
    assert indexer.search_media_memories("hapus") == []


def test_search_marks_deleted_assets_unavailable(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    indexer = make_indexer(tmp_path, media=store)
    media = store.save("s1", "plan.mp3", b"ID3" + b"\x00" * 64, owner_id="owner-1")
    store.mark_ready(media.id, {"transcript": "akan dihapus"})
    refreshed = store.get(media.id)
    assert refreshed is not None
    indexer.index_media(refreshed)
    store.delete(media.id)
    results = indexer.search_media_memories("akan")
    assert len(results) == 1
    assert results[0]["asset_available"] is False
    formatted = format_media_memory_row(results[0])
    assert "asset no longer stored" in formatted
    assert "[image:" not in formatted


def test_format_media_memory_row_includes_delivery_marker(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    indexer = make_indexer(tmp_path, media=store)
    media = store.save("s1", "photo.png", PNG_BYTES, owner_id="owner-1")
    indexer.index_media_caption(media, "desain dashboard")
    results = indexer.search_media_memories("desain")
    assert results
    formatted = format_media_memory_row(results[0])
    assert formatted.startswith(f"[image:{media.id}]")
    assert "photo.png" in formatted


def test_media_marker_for_asset_by_kind() -> None:
    asset_id = "a" * 32
    assert media_marker_for_asset(asset_id, "image") == f"[image:{asset_id}]"
    assert media_marker_for_asset(asset_id, "video") == f"[video:{asset_id}]"
    assert media_marker_for_asset(asset_id, "audio") == f"[voice:{asset_id}]"
    assert media_marker_for_asset(asset_id, "document").startswith("[attach id=")


async def test_recall_media_tool_returns_markers(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    indexer = make_indexer(tmp_path, media=store)
    media = store.save("s1", "dashboard.png", PNG_BYTES, owner_id="owner-1")
    indexer.index_media_caption(media, "desain dashboard copper")
    tool = recall_media_tool(indexer, database=tmp_path / "db.sqlite", media_store=store)
    assert tool.name == RECALL_MEDIA_TOOL_NAME
    result = await tool.handler({"query": "desain dashboard"}, make_context())
    assert f"[image:{media.id}]" in result
    assert "desain dashboard copper" in result


async def test_recall_media_tool_requires_query(tmp_path: Path) -> None:
    indexer = make_indexer(tmp_path)
    tool = recall_media_tool(indexer, database=tmp_path / "db.sqlite")
    result = await tool.handler({"query": "  "}, make_context())
    assert result == "media search failed: query is required"


async def test_recall_media_tool_reports_no_matches(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    indexer = make_indexer(tmp_path, media=store)
    tool = recall_media_tool(indexer, database=tmp_path / "db.sqlite", media_store=store)
    result = await tool.handler({"query": "nothing matches this"}, make_context())
    assert result == "No matching media memories."


async def test_recall_media_tool_honors_owner_scope(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    owner_one = MediaMemoryIndexer(tmp_path / "db.sqlite", owner_id="owner-one")
    owner_two = MediaMemoryIndexer(tmp_path / "db.sqlite", owner_id="owner-two")
    media = store.save("s1", "secret.mp3", b"ID3" + b"\x00" * 64, owner_id="owner-one")
    store.mark_ready(media.id, {"transcript": "catatan pribadi owner satu"})
    refreshed = store.get(media.id)
    assert refreshed is not None
    owner_one.index_media(refreshed)
    tool = recall_media_tool(owner_two, database=tmp_path / "db.sqlite", media_store=store)
    result = await tool.handler({"query": "catatan pribadi"}, make_context(owner_id="owner-two"))
    assert result == "No matching media memories."


def test_session_store_find_and_delete_by_source(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "db.sqlite")
    memory_id = store.add_memory(
        "media memory content",
        "media,image",
        category=MEDIA_MEMORY_CATEGORY,
        memory_type="episode",
        source="media:asset-1",
        owner_id="owner-1",
    )
    rows = store.find_memories_by_source("media:asset-1")
    assert len(rows) == 1
    assert rows[0]["id"] == memory_id
    assert store.delete_memories_by_source("media:asset-1") == 1
    assert store.find_memories_by_source("media:asset-1") == []
    assert store.delete_memories_by_source("") == 0


def test_media_memory_survives_memory_manager_recall(tmp_path: Path) -> None:
    from eurus.memory.manager import MemoryManager

    store = make_store(tmp_path)
    indexer = make_indexer(tmp_path, media=store, owner="local")
    media = store.save("s1", "plan.mp3", b"ID3" + b"\x00" * 64, owner_id="local")
    store.mark_ready(media.id, {"transcript": "ingat rencana konferensi bulan Juni"})
    refreshed = store.get(media.id)
    assert refreshed is not None
    indexer.index_media(refreshed)
    manager = MemoryManager(tmp_path / "db.sqlite", owner_id="local")
    results = manager.recall("konferensi")
    assert results
    assert any("konferensi" in str(row.get("content", "")) for row in results)


def test_media_memory_excluded_from_manager_recall_for_other_owner(tmp_path: Path) -> None:
    from eurus.memory.manager import MemoryManager

    store = make_store(tmp_path)
    indexer = make_indexer(tmp_path, media=store, owner="owner-one")
    media = store.save("s1", "plan.mp3", b"ID3" + b"\x00" * 64, owner_id="owner-one")
    store.mark_ready(media.id, {"transcript": "rahasia eksklusif pemilik"})
    refreshed = store.get(media.id)
    assert refreshed is not None
    indexer.index_media(refreshed)
    other = MemoryManager(tmp_path / "db.sqlite", owner_id="owner-two")
    assert other.recall("rahasia eksklusif") == []


@pytest.mark.parametrize(
    ("query", "expected_count"),
    [
        ("EURUS", 1),
        ("multimodal", 1),
        ("nothing", 0),
    ],
)
def test_search_media_memories_bounds(tmp_path: Path, query: str, expected_count: int) -> None:
    store = make_store(tmp_path)
    indexer = make_indexer(tmp_path, media=store)
    audio = store.save("s1", "plan.mp3", b"ID3" + b"\x00" * 64, owner_id="owner-1")
    store.mark_ready(audio.id, {"transcript": "EURUS multimodal"})
    refreshed = store.get(audio.id)
    assert refreshed is not None
    indexer.index_media(refreshed)
    assert len(indexer.search_media_memories(query)) == expected_count
