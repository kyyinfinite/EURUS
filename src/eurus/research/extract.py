from __future__ import annotations

import json
import re
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from eurus.research.errors import ExtractionError

HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
PDF_CONTENT_TYPES = frozenset({"application/pdf"})
FEED_CONTENT_TYPES = frozenset({"application/rss+xml", "application/atom+xml"})
XML_CONTENT_TYPES = frozenset({"application/xml", "text/xml"})
JSON_CONTENT_TYPES = frozenset({"application/json", "application/problem+json"})
TEXT_CONTENT_TYPES = frozenset({"text/plain", "text/csv", "text/markdown"})

MAX_FEED_ITEMS = 100
MAX_PDF_STREAMS = 200
MAX_PDF_DECOMPRESSED_BYTES = 8_000_000
MAX_PDF_TEXT_CHARS = 400_000

PDF_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
PDF_FLATE_RE = re.compile(rb"/FlateDecode")
PDF_TJ_RE = re.compile(rb"\[((?:[^\[\]\\]|\\.)*)\]\s*TJ", re.DOTALL)
PDF_TJ_STR_RE = re.compile(rb"\(((?:[^()\\]|\\.)*)\)")
PDF_TJ_HEX_RE = re.compile(rb"<([0-9A-Fa-f\s]+)>")
PDF_TJ_RE2 = re.compile(rb"\(((?:[^()\\]|\\.)*)\)\s*(?:Tj|')", re.DOTALL)
PDF_HEX_RE = re.compile(rb"<([0-9A-Fa-f\s]+)>\s*(?:Tj|')")
ESCAPES = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}


@dataclass(frozen=True, slots=True)
class PageContent:
    title: str
    text: str
    date: str
    author: str
    content_type: str

    def to_page_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "text": self.text,
            "date": self.date,
            "author": self.author,
        }


def content_type_family(content_type: str) -> str:
    lowered = (content_type or "").split(";", 1)[0].strip().lower()
    if not lowered:
        return "html"
    if lowered in HTML_CONTENT_TYPES:
        return "html"
    if lowered in PDF_CONTENT_TYPES:
        return "pdf"
    if lowered in FEED_CONTENT_TYPES:
        return "feed"
    if lowered in JSON_CONTENT_TYPES:
        return "json"
    if lowered in XML_CONTENT_TYPES:
        return "xml"
    if lowered in TEXT_CONTENT_TYPES:
        return "text"
    return "unsupported"


def extract_html(html: str, url: str) -> PageContent:
    try:
        import trafilatura
    except ImportError:
        return PageContent(
            title=url, text=_html_fallback_text(html), date="", author="", content_type="html"
        )
    try:
        raw = trafilatura.extract(
            html,
            url=url,
            output_format="json",
            with_metadata=True,
            include_comments=False,
            include_tables=True,
        )
    except (ValueError, TypeError):
        raw = None
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return PageContent(
                title=str(data.get("title") or url),
                text=str(data.get("text") or ""),
                date=str(data.get("date") or ""),
                author=str(data.get("author") or ""),
                content_type="html",
            )
    return PageContent(
        title=url, text=_html_fallback_text(html), date="", author="", content_type="html"
    )


def _html_fallback_text(html: str) -> str:
    from html.parser import HTMLParser

    class TextParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []
            self.skip = 0

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag in {"script", "style", "noscript", "svg"}:
                self.skip += 1

        def handle_endtag(self, tag: str) -> None:
            if tag in {"script", "style", "noscript", "svg"} and self.skip:
                self.skip -= 1

        def handle_data(self, data: str) -> None:
            if not self.skip and data.strip():
                self.parts.append(data.strip())

    parser = TextParser()
    parser.feed(html)
    return "\n".join(parser.parts)


def _decode_pdf_string(raw: bytes) -> bytes:
    out = bytearray()
    index = 0
    while index < len(raw):
        byte = raw[index : index + 1]
        if byte == b"\\" and index + 1 < len(raw):
            marker = raw[index + 1 : index + 2]
            if marker in ESCAPES:
                out += ESCAPES[marker]
                index += 2
                continue
            if marker.isdigit():
                octal = raw[index + 1 : index + 4]
                digits = re.match(rb"([0-7]{1,3})", octal)
                if digits:
                    out.append(int(digits.group(1), 8) & 0xFF)
                    index += 1 + len(digits.group(1))
                    continue
            if marker in {b"(", b")", b"\\"}:
                out += marker
                index += 2
                continue
            if marker == b"\n":
                index += 2
                continue
            out += marker
            index += 2
            continue
        out += byte
        index += 1
    return bytes(out)


def _decode_pdf_hex(raw: bytes) -> bytes:
    cleaned = re.sub(rb"\s", b"", raw)
    if len(cleaned) % 2:
        cleaned += b"0"
    try:
        return bytes.fromhex(cleaned.decode("ascii"))
    except ValueError:
        return b""


def _iter_pdf_text_operators(stream: bytes) -> Iterator[bytes]:
    for match in PDF_TJ_RE.finditer(stream):
        body = match.group(1)
        for piece in PDF_TJ_STR_RE.finditer(body):
            yield _decode_pdf_string(piece.group(1))
        for piece in PDF_TJ_HEX_RE.finditer(body):
            decoded = _decode_pdf_hex(piece.group(1))
            if decoded:
                yield decoded
    for match in PDF_TJ_RE2.finditer(stream):
        yield _decode_pdf_string(match.group(1))
    for match in PDF_HEX_RE.finditer(stream):
        decoded = _decode_pdf_hex(match.group(1))
        if decoded:
            yield decoded


def extract_pdf(raw: bytes, url: str) -> PageContent:
    if not raw.startswith(b"%PDF"):
        raise ExtractionError("the response is not a PDF document")
    collected: list[bytes] = []
    decompressed = 0
    streams = 0
    for match in PDF_STREAM_RE.finditer(raw):
        if streams >= MAX_PDF_STREAMS:
            break
        head_start = max(0, match.start() - 400)
        header = raw[head_start : match.start()]
        if not PDF_FLATE_RE.search(header):
            continue
        streams += 1
        data = match.group(1)
        try:
            inflated = zlib.decompress(data)
        except zlib.error:
            continue
        decompressed += len(inflated)
        if decompressed > MAX_PDF_DECOMPRESSED_BYTES:
            break
        for chunk in _iter_pdf_text_operators(inflated):
            collected.append(chunk)
            if sum(len(item) for item in collected) > MAX_PDF_TEXT_CHARS:
                break
    joined = b"".join(collected)
    if not joined.strip():
        raise ExtractionError(
            "could not extract text from this PDF (it may be scanned or use unsupported encodings)"
        )
    text = joined.decode("latin-1", errors="replace")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise ExtractionError(
            "could not extract text from this PDF (it may be scanned or use unsupported encodings)"
        )
    title = ""
    title_match = re.search(rb"/Title\s*\(([^)]{1,300})\)", raw)
    if title_match:
        title = _decode_pdf_string(title_match.group(1)).decode("latin-1", errors="replace")
    return PageContent(
        title=title or url,
        text=text,
        date="",
        author="",
        content_type="pdf",
    )


def _local_children(element: Any, local: str) -> list[Any]:
    return [
        child
        for child in element
        if isinstance(getattr(child, "tag", None), str) and child.tag.split("}")[-1] == local
    ]


def _local_child(element: Any, local: str) -> Any | None:
    for child in _local_children(element, local):
        return child
    return None


def _local_text(element: Any, local: str) -> str:
    child = _local_child(element, local)
    if child is None:
        return ""
    return (child.text or "").strip()


def extract_feed(raw: str, url: str) -> PageContent:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ExtractionError(f"could not parse this feed: {exc}") from exc
    tag = root.tag.lower()
    if tag.endswith("rss"):
        channel = _local_child(root, "channel")
        if channel is None:
            raise ExtractionError("RSS feed has no channel element")
        title = _local_text(channel, "title")
        date = _local_text(channel, "pubDate")
        author = _local_text(channel, "managingEditor")
        items = _local_children(channel, "item")[:MAX_FEED_ITEMS]
        lines: list[str] = []
        for item in items:
            item_title = _local_text(item, "title")
            item_date = _local_text(item, "pubDate")
            description = _local_text(item, "description")
            link = _local_text(item, "link")
            block = f"## {item_title}" if item_title else "## (untitled item)"
            if link:
                block += f" ({link})"
            if item_date:
                block += f" — {item_date}"
            if description:
                block += f"\n{description}"
            lines.append(block)
        return PageContent(
            title=title or url,
            text="\n\n".join(lines),
            date=date,
            author=author,
            content_type="feed",
        )
    if tag.endswith("feed"):
        title = _local_text(root, "title")
        date = _local_text(root, "updated")
        author_node = _local_child(root, "author")
        author_name = _local_text(author_node, "name") if author_node is not None else ""
        entries = _local_children(root, "entry")[:MAX_FEED_ITEMS]
        lines = []
        for entry in entries:
            entry_title = _local_text(entry, "title")
            entry_date = _local_text(entry, "updated") or _local_text(entry, "published")
            summary = _local_text(entry, "summary")
            link = ""
            for link_node in _local_children(entry, "link"):
                href = link_node.get("href") or ""
                if href:
                    link = href
                    break
            block = f"## {entry_title}" if entry_title else "## (untitled entry)"
            if link:
                block += f" ({link})"
            if entry_date:
                block += f" — {entry_date}"
            if summary:
                block += f"\n{summary}"
            lines.append(block)
        return PageContent(
            title=title or url,
            text="\n\n".join(lines),
            date=date,
            author=author_name,
            content_type="feed",
        )
    raise ExtractionError("this XML document is not an RSS or Atom feed")


def looks_like_feed(raw: str) -> bool:
    head = raw.lstrip()[:1000].lower()
    return "<rss" in head or "<feed" in head or "<rdf" in head


def extract_json(raw: str, url: str) -> PageContent:
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ExtractionError(f"could not parse this JSON document: {exc}") from exc
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return PageContent(title=url, text=text, date="", author="", content_type="json")


def extract_text(raw: str, url: str) -> PageContent:
    return PageContent(title=url, text=raw, date="", author="", content_type="text")


def extract_payload(
    payload: bytes, content_type: str, url: str, encoding: str = "utf-8"
) -> PageContent:
    family = content_type_family(content_type)
    if family == "html":
        return extract_html(payload.decode(encoding, errors="replace"), url)
    if family == "pdf":
        return extract_pdf(payload, url)
    if family == "feed":
        return extract_feed(payload.decode(encoding, errors="replace"), url)
    if family == "xml":
        decoded = payload.decode(encoding, errors="replace")
        if looks_like_feed(decoded):
            return extract_feed(decoded, url)
        return extract_text(decoded, url)
    if family == "json":
        return extract_json(payload.decode(encoding, errors="replace"), url)
    if family == "text":
        return extract_text(payload.decode(encoding, errors="replace"), url)
    raise ExtractionError(f"unsupported webpage content type: {content_type}")
