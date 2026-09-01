"""
wacz.py
=======
Read everything the extractor needs out of one per-page ``page.wacz``, in a
single streaming pass.

Ported from ``causalia-final/extractor/core/wacz_reader.py``. Extraction needs
no browser and no replay: replay is only required to VIEW a page so its own JS
runs. To recover the article we need the bytes the server originally sent, and
those sit in the WACZ as ordinary WARC ``response`` records.

Three details were learned the hard way upstream and must not be "simplified":

* Browsertrix stores screenshots as ``resource`` records under
  ``urn:<variant>:<url>`` - the word "screenshot" appears nowhere in the URI, so
  matching on it finds nothing. The variant names are matched instead,
  best-first. Verified against real captures: on bama.hu the ``urn:fullPage``
  record is 3.3 MB whose body begins with the PNG magic number, with no HTTP
  envelope.
* An exact URL match must NOT win outright. When the seed 301s, the matching
  record is the redirect STUB and Readability sees an empty document. The
  redirect chain is followed instead.
* The WARC member is ``archive/page.warc.gz`` in per-page archives but
  ``archive/data.warc.gz`` in crawl-level ones, so the name is globbed.

CHANGED FROM THE PORT: the WARC member is opened as a STREAM rather than read
whole. ``archive.read(member)`` pulls the entire member into memory, which on a
1.4 GB capture measured ~5 GB RSS; ``scripts/wacz_screenshot.py`` upstream
already avoids it for the same reason.

The archive is opened READ ONLY and is never written to.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

from warcio.archiveiterator import ArchiveIterator

from .urls import (
    SKIP_CONTENT_TYPES,
    SKIP_EXTENSIONS,
    VIDEO_EXTENSION_BY_TYPE,
    normalize_url,
)

#: Screenshot variants Browsertrix can emit, in the order we prefer them:
#: the final full page beats the in-progress one, which beats the viewport,
#: which beats a thumbnail.
SCREENSHOT_VARIANTS = ("fullPageFinal", "fullPage", "view", "thumbnail")

#: Screenshot files the 2026-08-07 Playwright backfill may have left beside
#: page.wacz. webp first, because that is what it produced.
BACKFILL_SCREENSHOT_NAMES = ("screenshot.webp", "screenshot.png",
                             "screenshot.jpg", "screenshot.jpeg")

SCREENSHOT_MEMBER_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


class ArchiveUnreadable(Exception):
    """The .wacz could not be opened or holds no usable article document."""


@dataclass
class WarcPayload:
    content_type: str
    body: bytes
    #: HTTP status the capture recorded. A 206 body is ONE BYTE RANGE, not a
    #: file: writing one that does not start at byte 0 produces an unplayable
    #: container. Most captured video in this corpus is 206.
    status: str = ""
    content_range: str | None = None


@dataclass
class ArchiveContents:
    """Everything needed out of one page.wacz, read in a single pass."""

    main_html: bytes | None = None
    main_url: str | None = None
    main_content_type: str = ""
    page_entry: dict = field(default_factory=dict)
    #: normalized url -> payload, for images and videos only
    payloads: dict[str, WarcPayload] = field(default_factory=dict)
    screenshot: bytes | None = None
    screenshot_ext: str = ".png"
    #: Where the screenshot came from: a urn: variant name, a zip member name,
    #: or None when the archive holds none.
    screenshot_source: str | None = None
    screenshot_rank: int = len(SCREENSHOT_VARIANTS)     # lower = better
    html_record_count: int = 0

    @property
    def page_url(self) -> str | None:
        """Browsertrix's own record of what it was asked to capture."""
        url = self.page_entry.get("url")
        return url if isinstance(url, str) and url else None

    @property
    def captured_at(self) -> str | None:
        ts = self.page_entry.get("ts")
        return ts if isinstance(ts, str) and ts else None


@dataclass
class _HtmlRecord:
    """One ``text/html`` response, held until we know which is the article.

    ``body`` is None for a redirect: only its ``Location`` matters, and a
    capture can carry several, so buffering their bodies is waste.
    """

    url: str
    status: str
    location: str | None
    body: bytes | None
    content_type: str = ""

    @property
    def is_redirect(self) -> bool:
        return self.body is None


def _pick_main_html(records: list[_HtmlRecord],
                    target_url: str | None) -> _HtmlRecord | None:
    """Choose the article document, following redirects to get there.

    An exact URL match must not win outright: when the seed 301s, the matching
    record is the redirect stub. Measured upstream on ``origo.hu/f1/2025/11/...``
    - the seed returns 301 with a 1,056-byte body while the article sits at
    ``/sport/f1/2025/11/...`` with 200 and 105,558 bytes, both in the same WACZ.
    origo moved a whole section this way, so it is not a one-off.
    """
    if not records:
        return None

    by_url: dict[str, _HtmlRecord] = {}
    for record in records:
        by_url.setdefault(normalize_url(record.url), record)

    if target_url:
        hop, seen = target_url, set()
        while hop and hop not in seen:        # `seen` guards redirect loops
            seen.add(hop)
            record = by_url.get(hop)
            if record is None:
                break                         # target not captured; fall back
            if not record.is_redirect:
                return record
            if not record.location:
                break
            hop = normalize_url(urljoin(record.url, record.location))

    # No target, or its chain led nowhere we captured: first real document.
    for record in records:
        if not record.is_redirect:
            return record
    return None


def find_backfilled_screenshot(wacz_path: Path) -> Path | None:
    """The screenshot sitting beside this archive, if the backfill ran.

    For ripost.hu this is the normal case: that crawl ran without
    ``--screenshot`` and every page got a ``screenshot.webp`` neighbour from the
    backfill that completed 2026-08-07. It is not a defect.
    """
    for name in BACKFILL_SCREENSHOT_NAMES:
        candidate = wacz_path.parent / name
        if candidate.is_file():
            return candidate
    return None


def _take_screenshot_record(contents: ArchiveContents, uri: str, record) -> None:
    """Record a urn: screenshot resource if it beats what we already hold."""
    _, _, remainder = uri.partition("urn:")
    variant, _, _ = remainder.partition(":")
    if variant not in SCREENSHOT_VARIANTS:
        return
    rank = SCREENSHOT_VARIANTS.index(variant)
    if rank >= contents.screenshot_rank:
        return
    body = record.content_stream().read()
    if not body:
        return
    contents.screenshot = body
    contents.screenshot_rank = rank
    contents.screenshot_source = variant
    contents.screenshot_ext = _image_extension(body)


def _image_extension(body: bytes) -> str:
    """Sniff the container from its magic number rather than trusting a name."""
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if body[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def read_archive(wacz_path: Path, expected_url: str | None = None) -> ArchiveContents:
    """Single read-only pass over a page.wacz.

    CSS/JS/font bodies are skipped WITHOUT being buffered - on a 6 MB archive
    they are most of the bytes and would only be discarded.
    """
    contents = ArchiveContents()
    wacz_path = Path(wacz_path)

    try:
        archive = zipfile.ZipFile(wacz_path)          # mode 'r' - never written
    except (OSError, zipfile.BadZipFile) as exc:
        raise ArchiveUnreadable(f"{wacz_path}: {exc}") from exc

    with archive:
        names = archive.namelist()

        # ---- pages/pages.jsonl: Browsertrix's own record of the capture
        if "pages/pages.jsonl" in names:
            raw = archive.read("pages/pages.jsonl").decode("utf-8", "replace")
            # Deliberately split on \n only. str.splitlines() also breaks on
            # U+0085 and U+2028, which occur inside real article titles in this
            # corpus and shred the JSON entry they appear in.
            for line in raw.split("\n"):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and "url" in entry:   # skip the header
                    contents.page_entry = entry
                    break

        # ---- a screenshot stored as a plain zip member (some WACZ producers)
        for name in names:
            lowered = name.lower()
            if "screenshot" in lowered and lowered.endswith(SCREENSHOT_MEMBER_SUFFIXES):
                body = archive.read(name)
                if body:
                    contents.screenshot = body
                    contents.screenshot_ext = _image_extension(body)
                    contents.screenshot_source = name
                break

        target_url = normalize_url(expected_url) if expected_url else None
        html_records: list[_HtmlRecord] = []

        warc_names = [n for n in names if n.startswith("archive/") and ".warc" in n]
        for warc_name in warc_names:
            # Streamed, not read whole: a 1.4 GB member costs gigabytes resident.
            with archive.open(warc_name) as stream:
                for record in ArchiveIterator(stream):
                    uri = record.rec_headers.get_header("WARC-Target-URI") or ""

                    if record.rec_type == "resource" and uri.startswith("urn:"):
                        _take_screenshot_record(contents, uri, record)
                        continue

                    if record.rec_type != "response" or uri.startswith("urn:"):
                        continue

                    content_type = ""
                    if record.http_headers is not None:
                        content_type = record.http_headers.get_header("Content-Type") or ""
                    content_type_main = content_type.split(";")[0].strip().lower()

                    if any(skip in content_type_main for skip in SKIP_CONTENT_TYPES):
                        continue
                    if Path(urlparse(uri).path).suffix.lower() in SKIP_EXTENSIONS:
                        continue

                    if content_type_main == "text/html":
                        contents.html_record_count += 1
                        status, location = "", None
                        if record.http_headers is not None:
                            status = record.http_headers.get_statuscode() or ""
                            location = record.http_headers.get_header("Location")
                        redirect = status.startswith("3") and bool(location)
                        # Collected, not chosen: which document is the article
                        # depends on the redirect chain, which is only known
                        # once the whole WARC has been walked.
                        html_records.append(_HtmlRecord(
                            url=uri, status=status, location=location,
                            body=None if redirect else record.content_stream().read(),
                            content_type=content_type))
                        continue

                    if (content_type_main.startswith(("image/", "video/"))
                            or content_type_main in VIDEO_EXTENSION_BY_TYPE):
                        status, content_range = "", None
                        if record.http_headers is not None:
                            status = record.http_headers.get_statuscode() or ""
                            content_range = record.http_headers.get_header("Content-Range")
                        contents.payloads[normalize_url(uri)] = WarcPayload(
                            content_type_main, record.content_stream().read(),
                            status, content_range)

        main = _pick_main_html(html_records, target_url)
        if main is not None:
            contents.main_html = main.body
            contents.main_url = main.url
            contents.main_content_type = main.content_type

    return contents


def read_archive_for_page(wacz_path: Path) -> ArchiveContents:
    """``read_archive`` plus the re-read that pins the right HTML document.

    The first pass is needed to learn the page URL from pages.jsonl; only if the
    HTML we happened to grab is NOT that page do we pay for a second pass. On a
    well-formed capture that second read never happens.
    """
    contents = read_archive(wacz_path)
    page_url = contents.page_url
    if not page_url:
        return contents
    if contents.main_url and normalize_url(contents.main_url) != normalize_url(page_url):
        return read_archive(wacz_path, expected_url=page_url)
    return contents


def decode_html(contents: ArchiveContents) -> str:
    """Decode the captured document, honouring the charset the server declared.

    utf-8 covers this corpus, but a handful of older archive pages on
    magyarnemzet were served as iso-8859-2, and decoding those as utf-8 turns
    every Hungarian long vowel into a replacement character.
    """
    body = contents.main_html or b""
    charset = ""
    declared = contents.main_content_type or ""
    if "charset=" in declared.lower():
        charset = declared.lower().split("charset=", 1)[1].split(";")[0].strip(' "\'')
    if not charset:
        head = body[:4096].lower()
        marker = b"charset="
        index = head.find(marker)
        if index >= 0:
            charset = head[index + len(marker):index + len(marker) + 32].split(
                b'"')[0].split(b"'")[0].split(b">")[0].split(b";")[0].decode(
                    "ascii", "ignore").strip()
    for candidate in (charset, "utf-8"):
        if not candidate:
            continue
        try:
            return body.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")
