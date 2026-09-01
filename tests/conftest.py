"""
conftest.py
===========
Fixtures for the extractor tests.

NO BINARY FIXTURES ARE COMMITTED. WACZ archives are built at test time, for the
reason the first-generation suite recorded and which still holds: a real capture
is 4-9 MB, committing a handful would add tens of megabytes to the tree, and
they still would not cover the cases that matter most - a truncated zip, a
capture with no HTML record, a WARC with several HTML responses, a 206 video
range. Those have to be constructed.

Real archived captures ARE used, by ``test_integration.py``, from a directory
outside the repository. That suite skips cleanly when the directory is absent,
so ``pytest`` is self-contained on a fresh checkout.

The builders are ported from ``causalia-final/tests/conftest.py``.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

ARTICLE_URL = "https://ripost.hu/sztardzsusz/2026/06/teszt-cikk"


# Real encoded images, generated rather than hand-written as hex.
#
# The first attempt upstream used hand-assembled 1x1 byte strings; Pillow could
# not read dimensions back out of the WebP one, which silently turned the
# width/height assertions into "null == null" and would have let a real
# regression through. Distinct sizes per format make a wrong dimension
# impossible to mistake for a right one.
def _encode(fmt: str, size: tuple[int, int], colour: str) -> bytes:
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format=fmt)
    return buffer.getvalue()


PNG_IMAGE = _encode("PNG", (200, 100), "red")
JPEG_IMAGE = _encode("JPEG", (400, 200), "green")
#: webp is what ripost.hu's CDN actually serves, often from an extensionless
#: URL - which is why extension_for() trusts Content-Type over the path.
WEBP_IMAGE = _encode("WEBP", (300, 150), "blue")

PNG_SIZE = (200, 100)
JPEG_SIZE = (400, 200)
WEBP_SIZE = (300, 150)

#: Enough of an mp4 for the writer to accept it as a real payload.
MP4_BODY = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 9000


def make_warc(records: list[dict]) -> bytes:
    """Build a gzipped WARC from simple record descriptions.

    Each record is ``{"uri", "content_type", "body", "status"?, "type"?}``.
    ``type`` defaults to ``response``; pass ``resource`` with a ``urn:`` uri to
    emulate a Browsertrix screenshot record.
    """
    buffer = io.BytesIO()
    writer = WARCWriter(buffer, gzip=True)

    for record in records:
        body = record["body"]
        if isinstance(body, str):
            body = body.encode("utf-8")
        record_type = record.get("type", "response")

        if record_type == "resource":
            written = writer.create_warc_record(
                record["uri"], "resource",
                payload=io.BytesIO(body),
                length=len(body),
                warc_content_type=record.get("content_type", "image/png"),
            )
        else:
            header_list = [("Content-Type", record["content_type"]),
                           ("Content-Length", str(len(body)))]
            # Extra headers matter for video: a 206 body is one byte range, and
            # Content-Range is the only way to tell a whole file from a fragment.
            for name, value in (record.get("headers") or {}).items():
                header_list.append((name, value))
            headers = StatusAndHeaders(
                record.get("status", "200 OK"), header_list, protocol="HTTP/1.1")
            written = writer.create_warc_record(
                record["uri"], "response",
                payload=io.BytesIO(body),
                length=len(body),
                http_headers=headers,
            )
        writer.write_record(written)

    return buffer.getvalue()


def make_wacz(path: Path, *, page_url: str = ARTICLE_URL, records: list[dict],
              page_title: str = "Test article",
              captured_at: str = "2026-08-03T08:50:32.956Z",
              include_pages: bool = True) -> Path:
    """Write a minimal but structurally faithful page.wacz."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        if include_pages:
            lines = [
                json.dumps({"format": "json-pages-1.0", "id": "pages",
                            "title": "Pages"}),
                json.dumps({"id": "test-page", "url": page_url,
                            "title": page_title, "loadState": 4,
                            "ts": captured_at, "mime": "text/html",
                            "status": 200}),
            ]
            archive.writestr("pages/pages.jsonl", "\n".join(lines))
        archive.writestr("archive/page.warc.gz", make_warc(records))
        archive.writestr("datapackage.json", json.dumps({"profile": "data-package"}))
    return path


def html_document(body: str, *, title: str = "Teszt cikk",
                  description: str = "A teszt cikk leadje.",
                  extra_head: str = "") -> str:
    """A page with the metadata shape these Hungarian outlets actually serve."""
    json_ld = json.dumps({
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": title,
        "description": description,
        "datePublished": "2026-06-01T09:00:00+02:00",
        "dateModified": "2026-06-01T10:30:00+02:00",
        "articleSection": "sztardzsusz",
        "author": {"@type": "Person", "name": "Kovacs Anna"},
        "publisher": {"@type": "Organization", "name": "Ripost"},
    }, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="hu">
<head>
<meta charset="utf-8">
<title>{title}</title>
<link rel="canonical" href="{ARTICLE_URL}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<script type="application/ld+json">{json_ld}</script>
{extra_head}
</head>
<body>
<nav><a href="/">Fooldal</a></nav>
<article class="article">
{body}
</article>
<footer>Impresszum</footer>
</body>
</html>"""


#: A full article exercising every supported block type and the inline markup
#: that character offsets have to survive.
ARTICLE_BODY = """
<div class="left-column">
  <div class="block-content">
    <p>Donald <strong>Trump</strong> bejelentette
       <em>valamit</em> a sajtotajekoztaton.</p>
    <p>A masodik bekezdes <a href="https://example.com/hir">egy hivatkozast</a>
       tartalmaz, es meg <a href="https://ripost.hu/belfold/2026/01/masik">egy
       belsot</a> is.</p>
    <h2>Egy alcim</h2>
    <p>A harmadik bekezdes.</p>
    <figure>
      <img src="https://cdn.example.com/kep.jpg" alt="Egy kep"
           title="Egy kep cime">
      <figcaption>A kep alairasa</figcaption>
    </figure>
    <blockquote><p>Az elso idezett mondat.</p><p>A masodik.</p></blockquote>
    <ul><li>Elso elem</li><li>Masodik elem</li></ul>
    <p>Az utolso bekezdes zarja a cikket, hogy legyen eleg szoveg
       ahhoz, hogy a readability ezt a blokkot valassza ki.</p>
  </div>
</div>
"""


@pytest.fixture
def article_wacz(tmp_path):
    """A complete, ordinary capture: text, inline markup, an image, a screenshot."""
    return make_wacz(
        tmp_path / "pages" / "ripost.hu" / "aa" / ("a" * 64) / "page.wacz",
        records=[
            {"uri": ARTICLE_URL, "content_type": "text/html",
             "body": html_document(ARTICLE_BODY)},
            {"uri": "https://cdn.example.com/kep.jpg",
             "content_type": "image/jpeg", "body": JPEG_IMAGE},
            {"uri": f"urn:fullPage:{ARTICLE_URL}", "type": "resource",
             "content_type": "image/png", "body": PNG_IMAGE},
        ])


@pytest.fixture
def extracted(article_wacz, tmp_path):
    """``article_wacz`` extracted into a fresh output tree."""
    from causalia_extractor.pipeline import extract
    output = tmp_path / "out"
    result = extract(article_wacz, output)
    assert result.ok, result.error
    return result.output_dir


def load(directory: Path, name: str):
    return json.loads((Path(directory) / name).read_text(encoding="utf-8"))


def reader_tree(directory: Path):
    """The re-parsed readability.html, as a consumer of the artifacts would read it."""
    from causalia_extractor.dom import reparse
    return reparse((Path(directory) / "readability.html").read_text(encoding="utf-8"))
