import asyncio
import io
import subprocess
import wave
from pathlib import Path

import pytest

from eurus.media import (
    INLINE_TEXT_MIMES,
    MAX_MEDIA_BYTES,
    MediaError,
    MediaFile,
    MediaLimitError,
    MediaLimits,
    MediaProcessor,
    MediaStore,
    MediaValidationError,
    compose_attachment_block,
    detect_signature,
    run_processing_pipeline,
    sanitize_filename,
    sniff_mime,
)
from eurus.media.processing import (
    extract_document_metadata,
    extract_image_metadata,
    ffprobe_available,
)
from eurus.media.signatures import safe_session_component

PNG_BYTES = (
    b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a\x00\x00\x00\x0d\x49\x48\x44\x52"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90\x77\x53"
    b"\xde\x00\x00\x00\x0c\x49\x44\x41\x54\x78\x9c\x63\xf8\xcf\xc0\x00"
    b"\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00\x49\x45\x4e"
    b"\x44\xae\x42\x60\x82"
)

WIDE_PNG_BYTES = (
    b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a\x00\x00\x00\x0d\x49\x48\x44\x52"
    b"\x00\x00\x00\x64\x00\x00\x00\x02\x08\x02\x00\x00\x00\x21\x2d\xee"
    b"\x44\x00\x00\x00\x15\x49\x44\x41\x54\x78\x9c\x63\x64\xf8\xcf\x30"
    b"\x0a\x88\x04\x4c\x03\xed\x80\xa1\x04\x00\x5c\xd4\x01\x03\x39\x03"
    b"\xd9\x27\x00\x00\x00\x00\x49\x45\x4e\x44\xae\x42\x60\x82"
)


def _wav_bytes(duration_seconds: float = 0.25, sample_rate: int = 8000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        frames = int(duration_seconds * sample_rate)
        wav_file.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


@pytest.fixture()
def store(tmp_path: Path) -> MediaStore:
    return MediaStore(database=tmp_path / "media.db", root=tmp_path / "media-root")


@pytest.fixture()
def processor() -> MediaProcessor:
    return MediaProcessor(limits=MediaLimits(processing_timeout_seconds=20.0))


def run(coro):
    return asyncio.run(coro)


class TestIngestion:
    def test_save_registers_media_with_lifecycle_state(self, store: MediaStore):
        saved = store.save("s1", "photo.png", PNG_BYTES, declared_mime="image/png")
        assert saved.status == "registered"
        assert saved.owner_id == ""
        assert saved.kind == "image"
        assert saved.metadata == {}
        fetched = store.get(saved.id)
        assert fetched is not None
        assert fetched.status == "registered"
        assert Path(fetched.storage_path).is_file()

    def test_save_records_owner(self, store: MediaStore):
        saved = store.save("s1", "note.txt", b"hello", declared_mime="text/plain", owner_id="eurus")
        assert saved.owner_id == "eurus"
        assert store.get_owned(saved.id, "eurus") is not None
        assert store.get_owned(saved.id, "other") is None
        assert store.get_owned(saved.id, "") is None
        unowned = store.save("s1", "open.txt", b"hi", declared_mime="text/plain")
        assert store.get_owned(unowned.id, "anyone") is not None
        assert store.get_owned(unowned.id, "") is not None

    def test_save_rejects_empty_and_oversize(self, store: MediaStore):
        with pytest.raises(MediaValidationError, match="empty"):
            store.save("s1", "empty.png", b"")
        tight = MediaStore(
            database=store.database,
            root=store.root,
            limits=MediaLimits(max_bytes=8),
        )
        with pytest.raises(MediaLimitError, match="exceeds"):
            tight.save("s1", "big.png", PNG_BYTES, declared_mime="image/png")

    def test_save_deduplicates_by_sha256(self, store: MediaStore):
        first = store.save("s1", "photo.png", PNG_BYTES, declared_mime="image/png")
        second = store.save("s1", "photo-again.png", PNG_BYTES, declared_mime="image/png")
        assert second.id == first.id
        assert second.filename == first.filename
        assert len(store.list_for_session("s1")) == 1
        distinct = store.save("s1", "other.png", PNG_BYTES + b"\x00", declared_mime="image/png")
        assert distinct.id != first.id
        assert len(store.list_for_session("s1")) == 2

    def test_dedup_scoped_to_session(self, store: MediaStore):
        first = store.save("s1", "photo.png", PNG_BYTES, declared_mime="image/png")
        other_session = store.save("s2", "photo.png", PNG_BYTES, declared_mime="image/png")
        assert other_session.id != first.id

    def test_dedup_can_be_disabled(self, store: MediaStore):
        first = store.save("s1", "photo.png", PNG_BYTES, declared_mime="image/png")
        second = store.save(
            "s1",
            "photo.png",
            PNG_BYTES,
            declared_mime="image/png",
            deduplicate=False,
        )
        assert second.id != first.id

    def test_find_by_sha256(self, store: MediaStore):
        saved = store.save("s1", "photo.png", PNG_BYTES, declared_mime="image/png")
        found = store.find_by_sha256("s1", saved.sha256)
        assert found is not None and found.id == saved.id
        assert store.find_by_sha256("s1", "z" * 64) is None
        assert store.find_by_sha256("s1", "nothex") is None

    def test_save_blocks_path_traversal_session_ids(self, store: MediaStore):
        for bad in ("../etc", "a/b", "a\\b", "..", "  ", ".hidden"):
            with pytest.raises(MediaValidationError):
                store.save(bad, "note.txt", b"hello", declared_mime="text/plain")
        saved = store.save("s1", "note.txt", b"hello", declared_mime="text/plain")
        assert store.root.resolve() in Path(saved.storage_path).resolve().parents


class TestSignatureValidation:
    @pytest.mark.parametrize(
        ("data", "expected_mime", "expected_kind"),
        [
            (PNG_BYTES, "image/png", "image"),
            (b"\xff\xd8\xff\xe0" + b"\x00" * 32, "image/jpeg", "image"),
            (b"GIF89a" + b"\x00" * 24, "image/gif", "image"),
            (b"%PDF-1.4\n%fake\n" + b"\x00" * 16, "application/pdf", "document"),
            (b"ID3" + b"\x00" * 64, "audio/mpeg", "audio"),
            (b"OggS" + b"\x00" * 24, "audio/ogg", "audio"),
            (b"fLaC" + b"\x00" * 24, "audio/flac", "audio"),
            (b"\x1a\x45\xdf\xa3" + b"\x00" * 24, "video/webm", "video"),
            (b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16, "video/mp4", "video"),
            (b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 16, "video/quicktime", "video"),
        ],
    )
    def test_magic_byte_detection(self, data, expected_mime, expected_kind):
        assert sniff_mime(data, None, "probe.bin") == (expected_mime, expected_kind)
        assert detect_signature(data) is not None

    def test_riff_containers(self):
        wav = _wav_bytes()
        assert sniff_mime(wav, None, "note.wav") == ("audio/wav", "audio")
        webp = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 16
        assert sniff_mime(webp, None, "logo.webp") == ("image/webp", "image")

    def test_extension_fallback_requires_known_extension(self):
        assert sniff_mime(b"plain text", None, "notes.txt") == ("text/plain", "document")
        assert sniff_mime(b"{}", None, "data.json") == ("application/json", "document")
        with pytest.raises(MediaValidationError, match="unsupported file type"):
            sniff_mime(b"mystery", None, "file.xyz")
        assert sniff_mime(b"mystery", "text/plain", "file.xyz") == ("text/plain", "document")

    def test_declared_type_conflict_is_rejected(self):
        with pytest.raises(MediaValidationError, match="conflicts"):
            sniff_mime(b"plain text", "image/png", "notes.txt")

    def test_zip_without_ooxml_profile_is_rejected(self):
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("hello.txt", "not office")
        with pytest.raises(MediaValidationError, match="unsupported ZIP container"):
            sniff_mime(buffer.getvalue(), None, "archive.zip")
        with pytest.raises(MediaValidationError, match="not a valid"):
            sniff_mime(b"PK\x03\x04" + b"\x00" * 32, None, "report.docx")

    @pytest.mark.parametrize(
        ("build", "filename", "expected_mime"),
        [
            (lambda: _docx_bytes(), "report.docx", "wordprocessingml.document"),
            (lambda: _xlsx_bytes(), "book.xlsx", "spreadsheetml.sheet"),
            (lambda: _pptx_bytes(), "deck.pptx", "presentationml.presentation"),
        ],
    )
    def test_ooxml_profiles_detected(self, build, filename, expected_mime):
        data = build()
        mime, kind = sniff_mime(data, None, filename)
        assert expected_mime in mime
        assert kind == "document"

    def test_ooxml_declared_mismatch_rejected(self):
        data = _docx_bytes()
        declared = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        with pytest.raises(MediaValidationError, match="does not match"):
            sniff_mime(data, declared, "x.docx")

    def test_empty_data_rejected(self):
        with pytest.raises(MediaValidationError, match="empty"):
            sniff_mime(b"", None, "file.png")

    def test_safe_session_component_rules(self):
        assert safe_session_component(" session-1 ") == "session-1"
        for bad in ("", "  ", "a/b", "a\\b", "..", ".hidden", "x" * 201, "a\x00b"):
            with pytest.raises(MediaValidationError):
                safe_session_component(bad)


def _docx_bytes() -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph("media infrastructure probe")
    document.core_properties.title = "Probe Report"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.title = "probe"
    workbook.create_sheet("second")
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _pptx_bytes() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


class TestMetadataExtraction:
    def test_image_metadata_via_pillow(self):
        metadata = extract_image_metadata(PNG_BYTES)
        assert metadata is not None
        assert metadata.width == 1 and metadata.height == 1
        assert metadata.image_format == "PNG"

    def test_image_metadata_returns_none_for_corrupt_image(self):
        assert extract_image_metadata(b"\x89PNG\r\n\x1a\nbroken") is None

    @pytest.mark.skipif(not ffprobe_available(), reason="ffprobe not installed")
    def test_audio_metadata_via_ffprobe(self):
        from eurus.media.processing import extract_audio_metadata

        metadata = extract_audio_metadata(_wav_bytes())
        assert metadata is not None
        assert metadata.sample_rate == 8000
        assert metadata.channels == 1
        assert metadata.duration_seconds is not None and metadata.duration_seconds > 0

    @pytest.mark.skipif(not ffprobe_available(), reason="ffprobe not installed")
    def test_video_metadata_via_ffprobe(self):
        import shutil
        import tempfile

        from eurus.media.processing import extract_video_metadata

        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            pytest.skip("ffmpeg not installed")
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "probe.mp4"
            subprocess.run(
                [
                    ffmpeg,
                    "-loglevel",
                    "quiet",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=64x48:d=0.3",
                    "-pix_fmt",
                    "yuv420p",
                    str(target),
                ],
                check=True,
                timeout=60,
            )
            metadata = extract_video_metadata(target.read_bytes())
        assert metadata is not None
        assert metadata.width == 64 and metadata.height == 48
        assert metadata.duration_seconds is not None and metadata.duration_seconds > 0

    def test_document_metadata_pdf(self):
        pdf = (
            b"%PDF-1.4\n1 0 obj\n<< /Type /Page >>\nendobj\n"
            b"2 0 obj\n<< /Type /Page >>\nendobj\ntrailer\n"
        )
        metadata = extract_document_metadata(pdf, "application/pdf")
        assert metadata is not None
        assert metadata.page_count == 2

    def test_document_metadata_office(self):
        docx = extract_document_metadata(
            _docx_bytes(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        assert docx is not None and docx.title == "Probe Report"
        xlsx = extract_document_metadata(
            _xlsx_bytes(),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        assert xlsx is not None and xlsx.sheet_count == 2
        pptx = extract_document_metadata(
            _pptx_bytes(),
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
        assert pptx is not None and pptx.slide_count == 1

    def test_document_metadata_unknown_document_returns_none(self):
        assert extract_document_metadata(b"data", "text/plain") is None


class TestProcessingLifecycle:
    def test_pipeline_marks_ready_with_image_metadata(
        self, store: MediaStore, processor: MediaProcessor
    ):
        saved = store.save("s1", "photo.png", PNG_BYTES, declared_mime="image/png")
        outcome = run(run_processing_pipeline(store, saved, processor, data=PNG_BYTES))
        assert outcome.status == "ready"
        assert outcome.metadata["width"] == 1
        assert outcome.metadata["height"] == 1
        fresh = store.get(saved.id)
        assert fresh is not None
        assert fresh.status == "ready"
        assert fresh.metadata["image_format"] == "PNG"
        assert "metadata" in fresh.view()

    @pytest.mark.skipif(not ffprobe_available(), reason="ffprobe not installed")
    def test_pipeline_marks_ready_with_audio_metadata(
        self, store: MediaStore, processor: MediaProcessor
    ):
        wav = _wav_bytes()
        saved = store.save("s1", "note.wav", wav, declared_mime="audio/wav")
        outcome = run(run_processing_pipeline(store, saved, processor, data=wav))
        assert outcome.status == "ready"
        assert outcome.metadata["sample_rate"] == 8000
        assert outcome.metadata["channels"] == 1

    def test_pipeline_marks_failed_on_limit_violation(self, store: MediaStore):
        strict = MediaProcessor(
            limits=MediaLimits(
                max_dimension=32,
                processing_timeout_seconds=10.0,
            )
        )
        saved = store.save("s1", "wide.png", WIDE_PNG_BYTES, declared_mime="image/png")
        outcome = run(run_processing_pipeline(store, saved, strict, data=WIDE_PNG_BYTES))
        assert outcome.status == "failed"
        assert outcome.error_code == "limit_exceeded"
        fresh = store.get(saved.id)
        assert fresh is not None
        assert fresh.status == "failed"
        assert fresh.error_code == "limit_exceeded"
        assert "error_code" in fresh.view()

    def test_processor_marks_missing_payload_failed(
        self, store: MediaStore, processor: MediaProcessor
    ):
        saved = store.save("s1", "photo.png", PNG_BYTES, declared_mime="image/png")
        Path(saved.storage_path).unlink()
        outcome = run(processor.process(saved))
        assert outcome.status == "failed"
        assert outcome.error_code == "corrupt_media"

    def test_processor_respects_timeout(self, store: MediaStore):
        class SlowProcessor(MediaProcessor):
            def _image(self, media: MediaFile, data: bytes) -> dict:
                import time

                time.sleep(0.2)
                return {"never": "happens"}

        saved = store.save("s1", "photo.png", PNG_BYTES, declared_mime="image/png")
        slowpoke = SlowProcessor(limits=MediaLimits(processing_timeout_seconds=0.001))
        outcome = run(slowpoke.process(saved, data=PNG_BYTES))
        assert outcome.status == "failed"
        assert outcome.error_code == "processing_timeout"

    def test_unknown_kind_yields_empty_metadata(self, store: MediaStore, processor: MediaProcessor):
        media = MediaFile(
            id="f" * 32,
            session_id="s1",
            filename="mystery.bin",
            mime="application/octet-stream",
            kind="other",
            size=8,
            sha256="0" * 64,
            storage_path="/tmp/nowhere.bin",
            created_at="2026-01-01T00:00:00+00:00",
        )
        outcome = run(processor.process(media, data=b"whatever"))
        assert outcome.status == "ready"
        assert outcome.metadata == {}

    def test_status_transitions_are_validated(self, store: MediaStore):
        saved = store.save("s1", "note.txt", b"hello", declared_mime="text/plain")
        with pytest.raises(ValueError, match="status"):
            store.update_status(saved.id, "vapourized")
        processing = store.mark_processing(saved.id)
        assert processing is not None and processing.status == "processing"
        ready = store.mark_ready(saved.id, {"title": "x"})
        assert ready is not None
        assert ready.status == "ready"
        assert ready.metadata["title"] == "x"
        failed = store.mark_failed(saved.id, "decoder_error")
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error_code == "decoder_error"
        assert failed.metadata["title"] == "x"
        assert store.update_status("0" * 32, "ready") is None


class TestResourceGovernance:
    def test_limits_validation_methods(self):
        limits = MediaLimits(
            max_bytes=100, max_pixels=100, max_dimension=10, max_duration_seconds=5
        )
        limits.validate_size(100)
        with pytest.raises(MediaLimitError):
            limits.validate_size(101)
        limits.validate_pixels(10, 10)
        with pytest.raises(MediaLimitError):
            limits.validate_pixels(11, 10)
        limits.validate_dimension(10, 10)
        with pytest.raises(MediaLimitError):
            limits.validate_dimension(11, 2)
        limits.validate_duration(5)
        with pytest.raises(MediaLimitError):
            limits.validate_duration(5.1)

    def test_limits_reject_invalid_configuration(self):
        with pytest.raises(ValueError):
            MediaLimits(max_bytes=0)
        with pytest.raises(ValueError):
            MediaLimits(max_concurrent_processing=0)
        with pytest.raises(ValueError):
            MediaLimits(max_processing_retries=-1)
        with pytest.raises(ValueError):
            MediaLimits(retention_days=-1)

    def test_limits_from_environ(self):
        limits = MediaLimits.from_environ(
            {
                "EURUS_MEDIA_MAX_BYTES": "1024",
                "EURUS_MEDIA_MAX_DURATION_SECONDS": "10.5",
                "EURUS_MEDIA_RETENTION_DAYS": "7",
                "EURUS_MEDIA_MAX_CONCURRENT_PROCESSING": "5",
            }
        )
        assert limits.max_bytes == 1024
        assert limits.max_duration_seconds == 10.5
        assert limits.retention_days == 7
        assert limits.max_concurrent_processing == 5

    def test_limits_from_environ_ignores_invalid_values(self):
        limits = MediaLimits.from_environ(
            {"EURUS_MEDIA_MAX_BYTES": "not-a-number", "EURUS_MEDIA_RETENTION_DAYS": "-3"}
        )
        assert limits.max_bytes == MAX_MEDIA_BYTES
        assert limits.retention_days == 0

    def test_default_limits_preserve_previous_maximum(self):
        assert MAX_MEDIA_BYTES == 20 * 1024 * 1024
        assert MediaLimits().max_bytes == MAX_MEDIA_BYTES

    def test_processing_concurrency_is_bounded(self, tmp_path: Path):
        store = MediaStore(database=tmp_path / "m.db", root=tmp_path / "r")
        saved = store.save("s1", "photo.png", PNG_BYTES, declared_mime="image/png")
        processor = MediaProcessor(limits=MediaLimits(max_concurrent_processing=1))
        active = 0
        peak = 0
        original = processor._image

        def tracking(media: MediaFile, data: bytes) -> dict:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            import time

            time.sleep(0.05)
            try:
                return original(media, data)
            finally:
                active -= 1

        processor._image = tracking

        async def go() -> list:
            tasks = [
                asyncio.create_task(processor.process(saved, data=PNG_BYTES)) for _ in range(4)
            ]
            return [await task for task in tasks]

        outcomes = run(go())
        assert len(outcomes) == 4
        assert all(o.status == "ready" for o in outcomes)
        assert peak == 1


class TestOptionalDependencies:
    def test_media_package_imports_without_optional_deps(self, monkeypatch):
        monkeypatch.setattr("eurus.media.processing.shutil.which", lambda name: None)
        from eurus.media import processing as processing_module

        assert processing_module.ffprobe_available() is False
        assert processing_module.extract_audio_metadata(b"ID3" + b"\x00" * 64) is None
        assert processing_module.extract_video_metadata(b"\x1a\x45\xdf\xa3") is None

    def test_image_extraction_reports_decoder_unavailable(self, store: MediaStore, monkeypatch):
        def unavailable(data: bytes) -> object:
            return None

        monkeypatch.setattr("eurus.media.processing.extract_image_metadata", unavailable)
        processor = MediaProcessor(limits=MediaLimits())
        saved = store.save("s1", "photo.png", PNG_BYTES, declared_mime="image/png")
        outcome = run(processor.process(saved, data=PNG_BYTES))
        assert outcome.status == "ready"
        assert outcome.metadata == {"decoder": "unavailable"}


class TestStorageIntegration:
    def test_legacy_media_schema_is_migrated(self, tmp_path: Path):
        import sqlite3

        from eurus.storage.sqlite import managed as sqlite_managed

        database = tmp_path / "legacy.db"
        root = tmp_path / "root"
        with sqlite_managed(database, rows=True) as connection:
            connection.executescript(
                """CREATE TABLE media_files (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """INSERT INTO media_files
                   (id, session_id, filename, mime, kind, size, sha256, storage_path, created_at)
                   VALUES ('0123456789abcdef0123456789abcdef', 's1', 'old.png', 'image/png',
                           'image', 10, 'aabb', 'x', '2020-01-01T00:00:00+00:00')"""
            )
        store = MediaStore(database=database, root=root)
        with sqlite3.connect(database) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(media_files)")}
        assert {"owner_id", "status", "error_code", "metadata_json", "updated_at"} <= columns
        legacy = store.get("0123456789abcdef0123456789abcdef")
        assert legacy is not None
        assert legacy.status == "ready"
        assert legacy.metadata == {}
        saved = store.save("s1", "new.png", PNG_BYTES, declared_mime="image/png")
        assert store.get(saved.id) is not None

    def test_retention_purge_removes_expired_assets(self, store: MediaStore):
        fresh = store.save("s1", "photo.png", PNG_BYTES, declared_mime="image/png")
        import sqlite3

        stale_sha = "b" * 64
        with sqlite3.connect(store.database) as connection:
            connection.execute(
                """INSERT INTO media_files
                   (id, session_id, filename, mime, kind, size, sha256, storage_path, created_at)
                   VALUES ('deadbeefdeadbeefdeadbeefdeadbeef', 's1', 'old.png', 'image/png',
                           'image', 10, ?, 'x', '2020-01-01T00:00:00+00:00')""",
                (stale_sha,),
            )
            connection.commit()
        purged = store.purge_expired(30)
        assert purged == 1
        assert store.get("deadbeefdeadbeefdeadbeefdeadbeef") is None
        assert store.get(fresh.id) is not None
        assert store.purge_expired(0) == 0

    def test_storage_path_for_validates_containment(self, store: MediaStore):
        import sqlite3

        saved = store.save("s1", "note.txt", b"hi", declared_mime="text/plain")
        assert store.storage_path_for(saved.id) == Path(saved.storage_path)
        with sqlite3.connect(store.database) as connection:
            connection.execute(
                "UPDATE media_files SET storage_path = '/etc/passwd' WHERE id = ?",
                (saved.id,),
            )
            connection.commit()
        with pytest.raises(MediaValidationError, match="escapes"):
            store.storage_path_for(saved.id)

    def test_get_for_session_enforces_session_scope(self, store: MediaStore):
        saved = store.save("s1", "note.txt", b"hi", declared_mime="text/plain")
        assert store.get_for_session(saved.id, "s1") is not None
        assert store.get_for_session(saved.id, "s2") is None

    def test_attachment_bridge_functions_preserved(self, store: MediaStore):
        saved = store.save("s1", "note.txt", b"hello world", declared_mime="text/plain")
        block = compose_attachment_block([saved])
        assert "[attach id=" in block
        assert "--- begin note.txt ---" in block
        assert "hello world" in block
        binary = store.save("s1", "photo.png", PNG_BYTES, declared_mime="image/png")
        marker = compose_attachment_block([binary])
        assert "(stored at" in marker
        assert saved.mime in INLINE_TEXT_MIMES

    def test_sanitize_filename(self):
        assert sanitize_filename("../../etc/passwd") == "passwd"
        assert sanitize_filename("weird name!!!.png") == "weird_name_.png"
        assert sanitize_filename("") == "file"
        assert len(sanitize_filename("a" * 500 + ".txt")) <= 120

    def test_delete_removes_row_and_file(self, store: MediaStore):
        saved = store.save("s1", "note.txt", b"hi", declared_mime="text/plain")
        path = Path(saved.storage_path)
        assert path.is_file()
        assert store.delete(saved.id) is True
        assert store.get(saved.id) is None
        assert not path.exists()
        assert store.delete(saved.id) is False

    def test_invalid_media_ids_rejected(self, store: MediaStore):
        assert store.get("not-an-id") is None
        assert store.get("") is None
        assert store.get("A" * 32) is None

    def test_module_reexports_for_compatibility(self):
        import eurus.media as media_module

        assert media_module.MAX_MEDIA_BYTES == 20 * 1024 * 1024
        assert callable(media_module.sniff_mime)
        assert callable(media_module.compose_attachment_block)
        assert callable(media_module.sanitize_filename)
        assert media_module.MediaError is MediaError
        assert media_module.MediaFile is MediaFile
        assert media_module.MediaStore is MediaStore
