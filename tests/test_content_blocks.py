import json

import pytest

from eurus.content import (
    ASSET_ID_PATTERN,
    BLOCK_TYPES,
    ArtifactBlock,
    AudioBlock,
    ContentBlockError,
    DocumentBlock,
    ImageBlock,
    TextBlock,
    VideoBlock,
    VideoFrameBlock,
    block_from_dict,
    blocks_from_dicts,
    blocks_to_dicts,
    referenced_asset_ids,
    text_projection,
)
from eurus.media import MediaFile, attachment_content_blocks
from eurus.models import Message

ASSET_A = "a" * 32
ASSET_B = "b" * 32
ASSET_C = "c3f1d2e4a5b6c7d8e9f0a1b2c3d4e5f6"


def _media_file(
    kind: str, mime: str, file_id: str = ASSET_A, filename: str = "clip.bin"
) -> MediaFile:
    return MediaFile(
        id=file_id,
        session_id="session-1",
        filename=filename,
        mime=mime,
        kind=kind,
        size=128,
        sha256="0" * 64,
        storage_path=f"/tmp/{file_id}.bin",
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_block_types_cover_required_modalities():
    assert BLOCK_TYPES == (
        "text",
        "image",
        "audio",
        "video",
        "video_frame",
        "document",
        "artifact",
    )


@pytest.mark.parametrize(
    "block",
    [
        TextBlock(text="analyze this"),
        ImageBlock(
            asset_id=ASSET_A, mime_type="image/jpeg", alt_text="a chart", width=640, height=480
        ),
        AudioBlock(asset_id=ASSET_A, mime_type="audio/mpeg", duration_seconds=12.5),
        AudioBlock(asset_id=ASSET_A, mime_type="audio/wav", transcript="hello world"),
        VideoBlock(asset_id=ASSET_A, mime_type="video/mp4", duration_seconds=60.0),
        VideoBlock(asset_id=ASSET_A, mime_type="video/webm", thumbnail_asset_id=ASSET_B),
        VideoFrameBlock(
            asset_id=ASSET_B,
            source_asset_id=ASSET_A,
            mime_type="image/jpeg",
            timestamp_seconds=4.25,
        ),
        DocumentBlock(
            asset_id=ASSET_A, mime_type="application/pdf", filename="report.pdf", page_count=12
        ),
        ArtifactBlock(
            artifact_id=ASSET_A, artifact_type="document", media_type="text/markdown", name="out.md"
        ),
    ],
)
def test_block_roundtrip(block):
    restored = block_from_dict(block.to_dict())
    assert restored == block


def test_block_type_tags():
    assert TextBlock("hi").type == "text"
    assert ImageBlock(ASSET_A, "image/png").type == "image"
    assert AudioBlock(ASSET_A, "audio/mpeg").type == "audio"
    assert VideoBlock(ASSET_A, "video/mp4").type == "video"
    assert VideoFrameBlock(ASSET_B, ASSET_A, "image/jpeg", 1.0).type == "video_frame"
    assert DocumentBlock(ASSET_A, "application/pdf").type == "document"
    assert ArtifactBlock(ASSET_A).type == "artifact"


def test_optional_fields_omitted_from_serialization():
    data = ImageBlock(asset_id=ASSET_A, mime_type="image/png").to_dict()
    assert data == {"type": "image", "asset_id": ASSET_A, "mime_type": "image/png"}
    data = DocumentBlock(asset_id=ASSET_A, mime_type="application/pdf").to_dict()
    assert data == {"type": "document", "asset_id": ASSET_A, "mime_type": "application/pdf"}


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "text", "text": "hi", "data": "base64rawbytes"},
        {"type": "image", "asset_id": ASSET_A, "mime_type": "image/png", "bytes": "AAA="},
        {"type": "image", "asset_id": ASSET_A, "mime_type": "image/png", "base64": "AAA="},
        {"type": "audio", "asset_id": ASSET_A, "mime_type": "audio/mpeg", "inline_data": "AAA="},
    ],
)
def test_binary_payload_fields_are_rejected(payload):
    with pytest.raises(ContentBlockError, match="unexpected block fields"):
        block_from_dict(payload)


def test_unknown_block_type_rejected():
    with pytest.raises(ContentBlockError, match="unknown block type"):
        block_from_dict({"type": "hologram", "asset_id": ASSET_A})


def test_non_object_block_rejected():
    with pytest.raises(ContentBlockError):
        block_from_dict("text")
    with pytest.raises(ContentBlockError):
        blocks_from_dicts("not-a-list")


@pytest.mark.parametrize(
    "asset_id",
    ["", "xyz", "A" * 32, "a" * 31, "a" * 33, "g" * 32, 12345, None],
)
def test_invalid_asset_references_rejected(asset_id):
    with pytest.raises(ContentBlockError):
        ImageBlock(asset_id=asset_id, mime_type="image/png")


def test_asset_id_pattern_is_strict():
    assert ASSET_ID_PATTERN.match(ASSET_C)
    assert not ASSET_ID_PATTERN.match(ASSET_C.upper())


@pytest.mark.parametrize(
    "mime_type",
    ["", "notamime", "image/", "/png", "image png", "x" * 200 + "/png", 42, None],
)
def test_invalid_mime_types_rejected(mime_type):
    with pytest.raises(ContentBlockError):
        ImageBlock(asset_id=ASSET_A, mime_type=mime_type)


def test_long_office_mimes_accepted():
    block = DocumentBlock(
        asset_id=ASSET_A,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert block.mime_type.endswith("wordprocessingml.document")


@pytest.mark.parametrize("text", [123, None, ["list"]])
def test_non_string_text_rejected(text):
    with pytest.raises(ContentBlockError):
        TextBlock(text=text)


def test_text_block_length_cap_enforced():
    with pytest.raises(ContentBlockError, match="exceeds"):
        TextBlock(text="x" * 100_001)


def test_metadata_length_caps_enforced():
    with pytest.raises(ContentBlockError):
        ImageBlock(asset_id=ASSET_A, mime_type="image/png", alt_text="x" * 501)
    with pytest.raises(ContentBlockError):
        DocumentBlock(asset_id=ASSET_A, mime_type="application/pdf", filename="f" * 121)
    with pytest.raises(ContentBlockError):
        ArtifactBlock(artifact_id=ASSET_A, name="n" * 501)


@pytest.mark.parametrize(
    ("field", "value"),
    [("width", 0), ("height", -5), ("width", 100_001)],
)
def test_dimension_bounds_enforced(field, value):
    with pytest.raises(ContentBlockError):
        ImageBlock(asset_id=ASSET_A, mime_type="image/png", **{field: value})


@pytest.mark.parametrize("value", [0, -1, 100_001])
def test_page_count_bounds_enforced(value):
    with pytest.raises(ContentBlockError):
        DocumentBlock(asset_id=ASSET_A, mime_type="application/pdf", page_count=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("duration_seconds", -1.0), ("duration_seconds", 3_000_000.0)],
)
def test_duration_bounds_enforced(field, value):
    with pytest.raises(ContentBlockError):
        AudioBlock(asset_id=ASSET_A, mime_type="audio/mpeg", **{field: value})


def test_video_frame_timestamp_bounds_enforced():
    with pytest.raises(ContentBlockError):
        VideoFrameBlock(
            asset_id=ASSET_B,
            source_asset_id=ASSET_A,
            mime_type="image/jpeg",
            timestamp_seconds=-0.5,
        )
    with pytest.raises(ContentBlockError):
        VideoFrameBlock(
            asset_id=ASSET_B,
            source_asset_id=ASSET_A,
            mime_type="image/jpeg",
            timestamp_seconds=3_000_000.0,
        )


def test_missing_required_fields_rejected():
    with pytest.raises(ContentBlockError, match="missing block field"):
        block_from_dict({"type": "image", "mime_type": "image/png"})
    with pytest.raises(ContentBlockError, match="missing block field"):
        block_from_dict({"type": "text"})
    with pytest.raises(ContentBlockError, match="missing block field"):
        block_from_dict({"type": "video_frame", "asset_id": ASSET_A, "source_asset_id": ASSET_B})


def test_block_count_cap_enforced():
    items = [{"type": "text", "text": "x"}] * 65
    with pytest.raises(ContentBlockError, match="at most"):
        blocks_from_dicts(items)


def test_blocks_from_dicts_preserves_order():
    blocks = blocks_from_dicts(
        [
            {"type": "text", "text": "first"},
            {"type": "image", "asset_id": ASSET_A, "mime_type": "image/png"},
            {"type": "text", "text": "second"},
            {"type": "audio", "asset_id": ASSET_B, "mime_type": "audio/mpeg"},
        ]
    )
    assert [block.type for block in blocks] == ["text", "image", "text", "audio"]
    assert blocks_to_dicts(blocks) == [
        {"type": "text", "text": "first"},
        {"type": "image", "asset_id": ASSET_A, "mime_type": "image/png"},
        {"type": "text", "text": "second"},
        {"type": "audio", "asset_id": ASSET_B, "mime_type": "audio/mpeg"},
    ]


def test_text_projection_joins_text_blocks_only():
    blocks = (
        TextBlock("analyze this image"),
        ImageBlock(asset_id=ASSET_A, mime_type="image/jpeg"),
        TextBlock("and this audio"),
        AudioBlock(asset_id=ASSET_B, mime_type="audio/mpeg"),
    )
    assert text_projection(blocks) == "analyze this image\nand this audio"
    assert text_projection(()) == ""
    assert text_projection((ImageBlock(ASSET_A, "image/png"),)) == ""


def test_referenced_asset_ids_collects_ordered_unique_references():
    blocks = (
        TextBlock("hi"),
        ImageBlock(asset_id=ASSET_A, mime_type="image/png"),
        VideoBlock(asset_id=ASSET_B, mime_type="video/mp4", thumbnail_asset_id=ASSET_C),
        VideoFrameBlock(
            asset_id=ASSET_C, source_asset_id=ASSET_B, mime_type="image/jpeg", timestamp_seconds=2.0
        ),
        DocumentBlock(asset_id=ASSET_A, mime_type="application/pdf"),
        ArtifactBlock(artifact_id="d" * 32),
    )
    assert referenced_asset_ids(blocks) == (ASSET_A, ASSET_B, ASSET_C, "d" * 32)


def test_media_file_blocks_mapping():
    files = [
        _media_file("image", "image/jpeg", ASSET_A, "photo.jpg"),
        _media_file("audio", "audio/mpeg", ASSET_B, "note.mp3"),
        _media_file("video", "video/mp4", ASSET_C, "clip.mp4"),
        _media_file("document", "application/pdf", "d" * 32, "doc.pdf"),
    ]
    blocks = attachment_content_blocks(files)
    assert isinstance(blocks, tuple)
    assert [block.type for block in blocks] == ["image", "audio", "video", "document"]
    assert blocks[0] == ImageBlock(asset_id=ASSET_A, mime_type="image/jpeg", alt_text="photo.jpg")
    assert blocks[1] == AudioBlock(asset_id=ASSET_B, mime_type="audio/mpeg")
    assert blocks[2] == VideoBlock(asset_id=ASSET_C, mime_type="video/mp4")
    assert blocks[3] == DocumentBlock(
        asset_id="d" * 32, mime_type="application/pdf", filename="doc.pdf"
    )


def test_media_file_blocks_reject_foreign_ids():
    with pytest.raises(ContentBlockError):
        attachment_content_blocks([_media_file("image", "image/jpeg", "not-hex")])


def test_message_with_mixed_content_roundtrips():
    message = Message.user(
        "analyze this image",
        (
            TextBlock("analyze this image"),
            ImageBlock(asset_id=ASSET_A, mime_type="image/jpeg"),
        ),
    )
    payload = message.to_dict()
    assert payload["role"] == "user"
    assert payload["content"] == "analyze this image"
    assert payload["blocks"] == [
        {"type": "text", "text": "analyze this image"},
        {"type": "image", "asset_id": ASSET_A, "mime_type": "image/jpeg"},
    ]
    assert Message.from_dict(payload) == message


def test_message_content_defaults_to_text_projection():
    message = Message.user(
        "",
        (TextBlock("look at this"), AudioBlock(asset_id=ASSET_A, mime_type="audio/mpeg")),
    )
    assert message.content == "look at this"


def test_message_explicit_content_wins_over_projection():
    message = Message.user(
        "custom text",
        (TextBlock("block text"), ImageBlock(ASSET_A, "image/png")),
    )
    assert message.content == "custom text"


def test_message_without_blocks_omits_key():
    payload = Message.user("plain").to_dict()
    assert "blocks" not in payload
    assert payload == {"role": "user", "content": "plain"}


def test_message_loads_legacy_payload_without_blocks():
    legacy = {"role": "user", "content": "old style"}
    message = Message.from_dict(legacy)
    assert message.blocks == ()
    assert message.content == "old style"


def test_message_rejects_blocks_on_tool_and_system_roles():
    blocks = (TextBlock("hi"),)
    with pytest.raises(ValueError, match="content blocks require"):
        Message(role="tool", content="x", tool_call_id="1", blocks=blocks)
    with pytest.raises(ValueError, match="content blocks require"):
        Message(role="system", content="x", blocks=blocks)


def test_assistant_message_may_carry_blocks_and_tool_calls():
    from eurus.models import ToolCall

    message = Message.assistant(
        "answer",
        (ToolCall("1", "bash", {"cmd": "ls"}),),
        (TextBlock("answer"),),
    )
    assert message.blocks[0].type == "text"
    assert Message.from_dict(message.to_dict()) == message


def test_message_rejects_invalid_block_payload():
    with pytest.raises(ContentBlockError):
        Message.from_dict(
            {
                "role": "user",
                "content": "x",
                "blocks": [{"type": "image", "asset_id": "bad", "mime_type": "image/png"}],
            }
        )
    with pytest.raises(ContentBlockError):
        Message.from_dict({"role": "user", "content": "x", "blocks": [{"type": "mystery"}]})


def test_message_payload_is_json_safe_and_binary_free():
    message = Message.user(
        "mixed",
        (
            TextBlock("mixed"),
            ImageBlock(ASSET_A, "image/jpeg"),
            DocumentBlock(ASSET_B, "application/pdf", filename="r.pdf", page_count=3),
        ),
    )
    encoded = json.dumps(message.to_dict(), ensure_ascii=False)
    assert "base64" not in encoded
    assert "data" not in encoded
    assert Message.from_dict(json.loads(encoded)) == message


def test_dataclasses_replace_preserves_blocks():
    from dataclasses import replace

    message = Message.user("keep", (ImageBlock(ASSET_A, "image/png"),))
    trimmed = replace(message, content="trimmed")
    assert trimmed.blocks == message.blocks
    assert trimmed.content == "trimmed"


def test_text_only_messages_still_equal_legacy_shape():
    legacy = {
        "role": "assistant",
        "content": "hi",
        "tool_calls": [],
        "tool_call_id": None,
        "name": None,
    }
    message = Message.from_dict(legacy)
    assert message == Message.assistant("hi")
