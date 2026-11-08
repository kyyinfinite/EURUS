from __future__ import annotations

import io
import zipfile

from eurus.media.errors import MediaError, MediaErrorCode, MediaValidationError

OOXML_CONTENT_TYPES = "[Content_Types].xml"

ALLOWED_MEDIA: tuple[tuple[str, str, str], ...] = (
    ("image/png", "image", "png"),
    ("image/jpeg", "image", "jpg"),
    ("image/webp", "image", "webp"),
    ("image/gif", "image", "gif"),
    ("audio/mpeg", "audio", "mp3"),
    ("audio/wav", "audio", "wav"),
    ("audio/x-wav", "audio", "wav"),
    ("audio/webm", "audio", "webm"),
    ("audio/ogg", "audio", "ogg"),
    ("audio/mp4", "audio", "m4a"),
    ("audio/flac", "audio", "flac"),
    ("audio/aac", "audio", "aac"),
    ("video/mp4", "video", "mp4"),
    ("video/webm", "video", "webm"),
    ("video/quicktime", "video", "mov"),
    ("application/pdf", "document", "pdf"),
    ("text/plain", "document", "txt"),
    ("text/markdown", "document", "md"),
    ("text/csv", "document", "csv"),
    ("application/json", "document", "json"),
    ("text/xml", "document", "xml"),
    ("application/xml", "document", "xml"),
    (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "document",
        "docx",
    ),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "document", "xlsx"),
    (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "document",
        "pptx",
    ),
    ("text/x-python", "document", "py"),
    ("text/javascript", "document", "js"),
    ("application/javascript", "document", "js"),
    ("text/typescript", "document", "ts"),
    ("text/x-typescript", "document", "ts"),
    ("text/jsx", "document", "jsx"),
    ("text/tsx", "document", "tsx"),
    ("text/html", "document", "html"),
    ("text/css", "document", "css"),
    ("application/sql", "document", "sql"),
    ("text/x-shellscript", "document", "sh"),
    ("text/yaml", "document", "yaml"),
    ("text/x-yaml", "document", "yaml"),
    ("text/x-toml", "document", "toml"),
    ("text/x-rst", "document", "rst"),
    ("text/x-c", "document", "c"),
    ("text/x-c++src", "document", "cpp"),
    ("text/x-java", "document", "java"),
    ("text/x-go", "document", "go"),
    ("text/x-rust", "document", "rs"),
)

MIME_BY_EXT: dict[str, tuple[str, str]] = {}
for _mime, _kind, _ext in ALLOWED_MEDIA:
    MIME_BY_EXT.setdefault(_ext, (_mime, _kind))

KIND_BY_MIME: dict[str, str] = {_mime: _kind for _mime, _kind, _ in ALLOWED_MEDIA}
EXT_BY_MIME: dict[str, str] = {_mime: _ext for _mime, _, _ext in ALLOWED_MEDIA}

INLINE_TEXT_MIMES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "text/xml",
        "application/xml",
        "text/x-python",
        "text/javascript",
        "application/javascript",
        "text/typescript",
        "text/x-typescript",
        "text/jsx",
        "text/tsx",
        "text/html",
        "text/css",
        "application/sql",
        "text/x-shellscript",
        "text/yaml",
        "text/x-yaml",
        "text/x-toml",
        "text/x-rst",
        "text/x-c",
        "text/x-c++src",
        "text/x-java",
        "text/x-go",
        "text/x-rust",
    }
)
INLINE_TEXT_MAX_CHARS = 12_000

_RIFF_CONTAINERS: dict[bytes, tuple[str, str, str]] = {
    b"WAVE": ("audio/wav", "audio", "wav"),
    b"WEBP": ("image/webp", "image", "webp"),
}

_AUDIO_FTYP_BRANDS: dict[bytes, tuple[str, str]] = {
    b"M4A ": ("audio/mp4", "audio"),
    b"M4B ": ("audio/mp4", "audio"),
    b"MP47": ("audio/mp4", "audio"),
    b"fmp4": ("audio/mp4", "audio"),
}

_AUDIO_CONTAINER_MIMES = frozenset({"audio/mp4", "audio/m4a", "audio/webm"})

_OOXML_SIGNATURES: dict[str, tuple[str, str, str]] = {
    "word/": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "document",
        "docx",
    ),
    "xl/": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "document",
        "xlsx",
    ),
    "ppt/": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "document",
        "pptx",
    ),
}


def _is_mp3(data: bytes) -> bool:
    if data[:3] == b"ID3":
        return True
    if len(data) < 4:
        return False
    if data[0] != 0xFF or (data[1] & 0xE0) != 0xE0:
        return False
    if (data[2] & 0xF0) == 0xF0 or (data[2] & 0x0C) == 0x0C:
        return False
    return (data[1] & 0x06) != 0x00 or (data[2] & 0xF0) in {0x10, 0x20, 0x30, 0x40, 0x50}


def _ooxml_profile(data: bytes) -> tuple[str, str, str] | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return None
    if OOXML_CONTENT_TYPES not in names:
        return None
    for marker, profile in _OOXML_SIGNATURES.items():
        if any(name.startswith(marker) for name in names):
            return profile
    return None


def sniff_mime(data: bytes, declared: str | None, filename: str) -> tuple[str, str]:
    if not data:
        raise MediaValidationError("file is empty", code=MediaErrorCode.EMPTY_FILE.value)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    declared_kind = KIND_BY_MIME.get(declared or "", "")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "image"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "image"
    if data[:4] == b"RIFF" and data[8:12] in _RIFF_CONTAINERS:
        mime, kind, _ = _RIFF_CONTAINERS[data[8:12]]
        if mime not in KIND_BY_MIME:
            raise MediaValidationError(
                f"unsupported RIFF container in {filename or 'unnamed'}",
                code=MediaErrorCode.UNSUPPORTED_TYPE.value,
            )
        return mime, kind
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "image"
    if data[:5] == b"%PDF-":
        return "application/pdf", "document"
    if data[:4] == b"PK\x03\x04":
        profile = _ooxml_profile(data)
        if profile is not None:
            mime, _, _ = profile
            if not declared_kind or declared == mime:
                return mime, KIND_BY_MIME[mime]
            raise MediaValidationError(
                f"declared type {declared!r} does not match the OOXML container of "
                f"{filename or 'unnamed'}",
                code=MediaErrorCode.SIGNATURE_MISMATCH.value,
            )
        if ext in {"docx", "xlsx", "pptx"}:
            raise MediaValidationError(
                f"file {filename or 'unnamed'} claims an Office format but is not a valid "
                "OOXML package",
                code=MediaErrorCode.INVALID_SIGNATURE.value,
            )
        raise MediaValidationError(
            f"unsupported ZIP container: {filename or 'unnamed'}",
            code=MediaErrorCode.UNSUPPORTED_TYPE.value,
        )
    if _is_mp3(data):
        return "audio/mpeg", "audio"
    if data[:4] == b"OggS":
        return "audio/ogg", "audio"
    if data[:4] == b"fLaC":
        return "audio/flac", "audio"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        if declared in _AUDIO_CONTAINER_MIMES:
            return "audio/webm", "audio"
        return "video/webm", "video"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in _AUDIO_FTYP_BRANDS:
            mime, kind = _AUDIO_FTYP_BRANDS[brand]
            return mime, kind
        if declared in {"audio/mp4", "audio/m4a"}:
            return "audio/mp4", "audio"
        if brand.startswith(b"qt") or brand == b"moov ":
            return "video/quicktime", "video"
        return "video/mp4", "video"
    if ext in MIME_BY_EXT:
        mime, kind = MIME_BY_EXT[ext]
        if declared and declared_kind and declared != mime:
            raise MediaValidationError(
                f"declared type {declared!r} conflicts with detected type {mime!r}",
                code=MediaErrorCode.SIGNATURE_MISMATCH.value,
            )
        return mime, kind
    if declared and declared_kind:
        return declared, declared_kind
    raise MediaValidationError(
        f"unsupported file type: {filename or 'unnamed'}",
        code=MediaErrorCode.UNSUPPORTED_TYPE.value,
    )


def detect_signature(data: bytes) -> str | None:
    if not data:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if _is_mp3(data):
        return "audio/mpeg"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:4] == b"fLaC":
        return "audio/flac"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    if data[4:8] == b"ftyp":
        return "video/mp4"
    if data[:4] == b"PK\x03\x04" and _ooxml_profile(data) is not None:
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    return None


def safe_session_component(session_id: str) -> str:
    cleaned = (session_id or "").strip()
    if not cleaned or len(cleaned) > 200:
        raise MediaValidationError(
            "session id is required and must be at most 200 characters",
            code=MediaErrorCode.INVALID_SESSION.value,
        )
    if "/" in cleaned or "\\" in cleaned or ".." in cleaned or "\x00" in cleaned:
        raise MediaValidationError(
            "session id must be a single safe path component",
            code=MediaErrorCode.TRAVERSAL_BLOCKED.value,
        )
    if cleaned.startswith("."):
        raise MediaValidationError(
            "session id must not start with a dot",
            code=MediaErrorCode.TRAVERSAL_BLOCKED.value,
        )
    return cleaned


def raise_unsupported_media(mime: str) -> MediaError:
    return MediaError(
        f"unsupported media type: {mime}",
        code=MediaErrorCode.UNSUPPORTED_TYPE.value,
    )
