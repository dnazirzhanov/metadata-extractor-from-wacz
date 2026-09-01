"""
images.py
=========
images.json and the local images/ directory.

Ported from ``causalia-final/extractor/core/images.py``. The load-bearing
decision is that images are collected FROM THE READABILITY BODY ONLY, so page
chrome is excluded by construction rather than by a blocklist. Measured
upstream: copying every image in the WARC cost 2.9 MB per article, about 828 GB
across ripost alone; taking media from the reader body costs ~70 KB per article.

Dimensions come from Pillow reading the header only - ``.load()`` is never
called, so the pixels are never decoded.

CHANGED FROM THE PORT: ``position`` and ``size_bytes`` are gone, and so is
``assign_positions``. An image's place in the article is expressed once, by its
content block's xpath, and the file's size is a property of the filesystem
rather than of the article.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .output import ArtifactWriter
from .urls import (
    MEDIA_URL_ATTRIBUTES,
    best_from_srcset,
    extension_for,
    normalize_url,
)
from .wacz import ArchiveContents

log = logging.getLogger(__name__)

#: Pillow is used ONLY to read dimensions from an image header. We never
#: call .load(), so pixel data from an untrusted archive is never decoded.
try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 64_000_000        # refuse decompression bombs
    HAVE_PILLOW = True
except ImportError:                            # dimensions become null
    HAVE_PILLOW = False


@dataclass
class ImageRecord:
    id: str
    filename: str | None = None
    original_url: str | None = None
    caption: str | None = None
    alt: str | None = None
    credit: str | None = None
    width: int | None = None
    height: int | None = None
    mime_type: str | None = None
    image_available: bool = True
    reason: str | None = None

    def to_dict(self) -> dict:
        payload = {
            "id": self.id,
            "filename": self.filename,
            "original_url": self.original_url,
            "caption": self.caption,
            "alt": self.alt,
            "credit": self.credit,
            "width": self.width,
            "height": self.height,
            "mime_type": self.mime_type,
            "image_available": self.image_available,
        }
        if not self.image_available:
            payload["reason"] = self.reason
        return payload


def _dimensions(body: bytes) -> tuple[int | None, int | None]:
    """(width, height) from the image header, or (None, None)."""
    if not HAVE_PILLOW or not body:
        return None, None
    try:
        with Image.open(io.BytesIO(body)) as image:
            return image.width, image.height
    except Exception:
        # SVG, AVIF without a plugin, or a truncated capture. A missing
        # dimension is not worth failing an article over.
        return None, None


def _caption_and_credit(element: Tag) -> tuple[str | None, str | None]:
    """Caption and credit for an image, read from its surroundings."""
    caption = credit = None

    figure = element.find_parent("figure")
    if figure is not None:
        figcaption = figure.find("figcaption")
        if figcaption is not None:
            caption = " ".join(figcaption.get_text(" ", strip=True).split()) or None

    for node in (element.parent, figure):
        if node is None:
            continue
        for candidate in node.find_all(True):
            identifier = " ".join(filter(None, [
                candidate.get("id", "") or "",
                " ".join(candidate.get("class", []) or []),
            ])).lower()
            # 'forras' / 'szerzo' are the Hungarian equivalents
            if any(m in identifier for m in ("credit", "forras", "szerzo", "copyright")):
                text = " ".join(candidate.get_text(" ", strip=True).split())
                if text:
                    credit = text
                    break
        if credit:
            break

    if credit is None:
        title = (element.get("title") or "").strip()
        if title and title != (element.get("alt") or "").strip():
            credit = title

    return caption, credit


class ImageExtractor:
    """Pulls article images out of the archive and localises the markup."""

    def __init__(self, contents: ArchiveContents, base_url: str,
                 writer: ArtifactWriter, rules):
        self.contents = contents
        self.base_url = base_url
        self.writer = writer
        self.rules = rules
        self.records: list[ImageRecord] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self._by_url: dict[str, ImageRecord] = {}
        #: keyed by SOURCE image rather than URL, so a CDN rendition of a
        #: photo already in the body is recognised as the same picture
        self._by_identity: dict[str, ImageRecord] = {}
        self._count = 0

    def _resolve(self, value: str) -> str:
        return urljoin(self.base_url, (value or "").strip())

    def _candidate_urls(self, element: Tag) -> list[str]:
        """Every URL this element might be pointing at, best first.

        data-* attributes come before ``src`` because lazy-loading themes
        leave a placeholder in ``src`` and put the real image in data-src.
        """
        candidates: list[str] = []
        for attribute in MEDIA_URL_ATTRIBUTES:
            value = element.get(attribute)
            if value and not str(value).startswith("data:"):
                candidates.append(self._resolve(str(value)))
        for attribute in ("srcset", "data-srcset"):
            if element.get(attribute):
                best = best_from_srcset(str(element[attribute]))
                if best and not best.startswith("data:"):
                    candidates.append(self._resolve(best))
        return list(dict.fromkeys(candidates))

    def _save(self, url: str, element: Tag) -> ImageRecord | None:
        """Write one image, or return None if it is not in the archive."""
        key = normalize_url(url)
        if key in self._by_url:
            return self._by_url[key]

        payload = self.contents.payloads.get(key)
        if payload is None or not payload.content_type.startswith("image/"):
            return None
        if not payload.body:
            self.warnings.append(f"empty image payload: {url}")
            return None

        self._count += 1
        identifier = f"image_{self._count:03d}"
        filename = f"{identifier}{extension_for(payload.content_type, url, is_video=False)}"
        self.writer.write_bytes(f"images/{filename}", payload.body)

        width, height = _dimensions(payload.body)
        caption, credit = _caption_and_credit(element)
        record = ImageRecord(
            id=identifier,
            filename=f"images/{filename}",
            original_url=url,
            caption=caption,
            alt=(element.get("alt") or "").strip() or None,
            credit=credit,
            width=width,
            height=height,
            mime_type=payload.content_type,
        )
        self.records.append(record)
        self._by_url[key] = record
        self._by_identity[self.rules.image_identity(url)] = record
        return record

    def _process_element(self, element: Tag) -> None:
        candidates = self._candidate_urls(element)
        if not candidates:
            return

        real = [c for c in candidates if not self.rules.is_generic_image(c)]
        if not real:
            # Every candidate is site furniture (the ripost masthead SVG
            # under /assets/images/mw/ is the common one). Readability
            # occasionally keeps such an image; it is not article content,
            # so it is removed rather than recorded as a MISSING article
            # image - which is what an earlier version did, inventing
            # image-loss reports for a logo we deliberately skipped.
            element.decompose()
            return

        for candidate in real:
            record = self._save(candidate, element)
            if record is not None:
                element["src"] = record.filename
                element["data-image-id"] = record.id
                if record.width:
                    element["width"] = str(record.width)
                if record.height:
                    element["height"] = str(record.height)
                for stale in ("srcset", "data-srcset", "data-src", "data-original",
                              "data-lazy-src", "data-url", "loading", "fetchpriority"):
                    element.attrs.pop(stale, None)
                return

        # Referenced by the article but absent from the capture. Record it
        # honestly and make sure the markup cannot fetch it: sanitize.py
        # will refuse the absolute URL in src, but we do not even offer it.
        self._count += 1
        identifier = f"image_{self._count:03d}"
        caption, credit = _caption_and_credit(element)
        self.records.append(ImageRecord(
            id=identifier,
            filename=None,
            original_url=candidates[0],
            caption=caption,
            alt=(element.get("alt") or "").strip() or None,
            credit=credit,
            image_available=False,
            reason="image response not present in archive",
        ))
        element.attrs.pop("src", None)
        element["data-image-id"] = identifier
        element["data-original-src"] = candidates[0]
        element["data-archive-missing"] = "true"
        self.warnings.append(f"image not captured in archive: {candidates[0]}")
        log.debug("image referenced but not archived: %s", candidates[0])

    def process(self, soup: BeautifulSoup) -> None:
        for element in soup.find_all("img"):
            self._process_element(element)

    def splice_lead_image(self, soup: BeautifulSoup, full_soup: BeautifulSoup,
                          article_node: dict, title: str | None) -> None:
        """Put the article's hero image at the top of the body, if needed.

        Readability's body block does not contain the lead photo on every
        layout - ripost.hu's puts it in a sibling component - so a
        body-only scrape yields an illustrated article with no
        illustration. We splice it in only when the capture actually holds
        the bytes, so we never render a broken hero.
        """
        lead_url = self.rules.lead_image(full_soup, article_node)
        if not lead_url:
            return
        absolute = urljoin(self.base_url, lead_url)

        # Already in the body, possibly as a different CDN rendition.
        if (normalize_url(absolute) in self._by_url
                or self.rules.image_identity(absolute) in self._by_identity):
            return

        if normalize_url(absolute) not in self.contents.payloads:
            # A NOTE, not a warning. Measured over a 50-article ripost
            # sample: 23 articles declared a JSON-LD lead image absent
            # from the capture, and 21 of those were a genuinely
            # different photograph that the page itself never loads -
            # i.e. a social-share image, not article content the crawl
            # missed. Treating it as a defect would mark ~40% of the
            # corpus "partial" and drain that status of meaning.
            self.notes.append(f"lead image declared in metadata but not "
                              f"loaded by the page: {absolute}")
            return

        figure = soup.new_tag("figure")
        image = soup.new_tag("img", src=absolute)
        image["alt"] = title or ""
        figure.append(image)
        target = soup.body if soup.body is not None else soup
        target.insert(0, figure)


def images_payload(records: list[ImageRecord]) -> list[dict]:
    return [record.to_dict() for record in records]
