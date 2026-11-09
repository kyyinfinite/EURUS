from __future__ import annotations

import sys
import zlib

import pytest

from eurus.research.errors import ExtractionError
from eurus.research.extract import (
    PageContent,
    content_type_family,
    extract_feed,
    extract_html,
    extract_json,
    extract_payload,
    extract_pdf,
    extract_text,
    looks_like_feed,
)

HTML_PAGE = """
<html><head><title>Battery report</title></head>
<body><nav>menu home about</nav>
<article><h1>Solid state batteries</h1>
<p>Solid state batteries reached 400 Wh/kg in 2026.</p>
<p>Manufacturers plan mass production by 2027.</p>
</article>
<footer>copyright</footer></body></html>
"""


def test_extract_html_uses_trafilatura_when_available() -> None:
    page = extract_html(HTML_PAGE, "https://example.com/report")
    assert isinstance(page, PageContent)
    assert page.content_type == "html"
    assert page.text.strip()


def test_extract_html_falls_back_when_trafilatura_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "trafilatura", None)
    page = extract_html(HTML_PAGE, "https://example.com/report")
    assert page.title == "https://example.com/report"
    assert "Solid state batteries" in page.text
    assert "menu home about" in page.text


def test_extract_html_fallback_drops_script_and_style(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "trafilatura", None)
    dirty = "<html><body><h1>T</h1>"
    dirty += "<script>evil()</script><style>.a{}</style><p>keep me</p>"
    dirty += "</body></html>"
    page = extract_html(dirty, "https://example.com/x")
    assert "keep me" in page.text
    assert "evil" not in page.text
    assert ".a{}" not in page.text


def build_pdf(*texts: bytes, title: str = "Report") -> bytes:
    streams = []
    for text in texts:
        operators = b"".join(b"(" + piece + b") Tj\n" for piece in text.split(b"\n"))
        streams.append(zlib.compress(operators))
    body = b"%PDF-1.4\n"
    body += b"/Title (" + title.encode("ascii") + b")\n"
    for compressed in streams:
        body += (
            b"<< /Length "
            + str(len(compressed)).encode()
            + b" /Filter /FlateDecode >>\nstream\n"
            + compressed
            + b"\nendstream\n"
        )
    body += b"trailer\n<< >>\n%%EOF\n"
    return body


def test_extract_pdf_pulls_text_from_content_streams() -> None:
    payload = build_pdf(b"line one about batteries\nline two about capacity")
    page = extract_pdf(payload, "https://example.com/report.pdf")
    assert "line one about batteries" in page.text
    assert "line two about capacity" in page.text
    assert page.title == "Report"
    assert page.content_type == "pdf"


def test_extract_pdf_uses_tj_arrays() -> None:
    stream = zlib.compress(b"[(Hel) 20 (lo ) 12 (wor) 8 (ld)] TJ\n[(second line)] TJ\n")
    payload = (
        b"%PDF-1.4\n"
        b"<< /Length "
        + str(len(stream)).encode()
        + b" /Filter /FlateDecode >>\nstream\n"
        + stream
        + b"\nendstream\ntrailer\n<< >>\n%%EOF\n"
    )
    page = extract_pdf(payload, "https://example.com/array.pdf")
    assert "Hello world" in page.text
    assert "second line" in page.text


def test_extract_pdf_rejects_non_pdf() -> None:
    with pytest.raises(ExtractionError, match="not a PDF"):
        extract_pdf(b"plain bytes", "https://example.com/x.pdf")


def test_extract_pdf_reports_unextractable_document() -> None:
    payload = build_pdf(b"")
    with pytest.raises(ExtractionError, match="could not extract text"):
        extract_pdf(payload, "https://example.com/empty.pdf")
    image_only = b"%PDF-1.4\nstream\n\x00\x01\x02\nendstream\n%%EOF\n"
    with pytest.raises(ExtractionError, match="could not extract text"):
        extract_pdf(image_only, "https://example.com/scanned.pdf")


RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Example News</title>
<pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate>
<managingEditor>editor@example.com</managingEditor>
<item><title>Battery milestone</title><link>https://example.com/1</link>
<pubDate>Tue, 27 Oct 2026</pubDate><description>Capacity improved again.</description></item>
<item><title>Second item</title><link>https://example.com/2</link>
<description>Another update.</description></item>
</channel></rss>
"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>Example Atom</title>
<updated>2026-10-27T00:00:00Z</updated>
<author><name>Ada</name></author>
<entry><title>First entry</title><link href="https://example.com/a" />
<updated>2026-10-26T00:00:00Z</updated><summary>Atom summary text.</summary></entry>
</feed>
"""


def test_extract_feed_parses_rss() -> None:
    page = extract_feed(RSS, "https://example.com/feed")
    assert page.title == "Example News"
    assert page.date.startswith("Mon, 26")
    assert page.author == "editor@example.com"
    assert "Battery milestone" in page.text
    assert "https://example.com/1" in page.text
    assert "Another update." in page.text


def test_extract_feed_parses_atom() -> None:
    page = extract_feed(ATOM, "https://example.com/atom")
    assert page.title == "Example Atom"
    assert page.author == "Ada"
    assert "First entry" in page.text
    assert "https://example.com/a" in page.text
    assert "Atom summary text." in page.text


def test_extract_feed_rejects_malformed_xml() -> None:
    with pytest.raises(ExtractionError, match="could not parse"):
        extract_feed("<rss><channel><title>", "https://example.com/broken")


def test_extract_feed_rejects_non_feed_root() -> None:
    with pytest.raises(ExtractionError, match="not an RSS or Atom"):
        extract_feed("<data><item>1</item></data>", "https://example.com/xml")


def test_looks_like_feed() -> None:
    assert looks_like_feed(RSS) is True
    assert looks_like_feed("<html><body>hello</body></html>") is False


def test_extract_json_pretty_prints() -> None:
    page = extract_json('{"a": [1, 2], "b": "text"}', "https://example.com/data.json")
    assert '"a"' in page.text
    assert page.title == "https://example.com/data.json"


def test_extract_json_rejects_invalid_payload() -> None:
    with pytest.raises(ExtractionError, match="could not parse this JSON"):
        extract_json("{broken", "https://example.com/bad.json")


def test_extract_text_passthrough() -> None:
    page = extract_text("plain text content", "https://example.com/notes.txt")
    assert page.text == "plain text content"


@pytest.mark.parametrize(
    ("content_type", "family"),
    [
        ("text/html; charset=utf-8", "html"),
        ("application/xhtml+xml", "html"),
        ("application/pdf", "pdf"),
        ("application/rss+xml", "feed"),
        ("application/atom+xml", "feed"),
        ("application/json", "json"),
        ("text/plain", "text"),
        ("application/zip", "unsupported"),
        ("", "html"),
    ],
)
def test_content_type_family(content_type: str, family: str) -> None:
    assert content_type_family(content_type) == family


def test_extract_payload_dispatches_by_family() -> None:
    html_page = extract_payload(HTML_PAGE.encode(), "text/html", "https://example.com/r")
    assert html_page.content_type == "html"
    feed = extract_payload(RSS.encode(), "application/xml", "https://example.com/feed")
    assert feed.content_type == "feed"
    with pytest.raises(ExtractionError, match="unsupported webpage content type"):
        extract_payload(b"zip", "application/zip", "https://example.com/z")
