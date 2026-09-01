"""
pipeline.py
===========
One WACZ in, one article directory out.

    page.wacz
       |
       +-- original.html          the captured markup, network references dead
       |
       +-- article.json           metadata, read from the ORIGINAL document
       |
       +-- readability.html       THE CANONICAL DOCUMENT
       |        |
       |        +-- re-parsed from the bytes just written
       |               |
       |               +-- content.json   blocks, each with a validated xpath
       |               +-- links.json     each with an XPath+offset+quote selector
       |
       +-- images.json + images/
       +-- videos.json + videos/
       +-- screenshot.png         Browsertrix's own capture, preferred
       +-- extraction.json        extraction_version, extracted_at, extraction_status

ORDERING CONSTRAINTS - these are not stylistic
----------------------------------------------
* Metadata reads the ORIGINAL, unstripped document. JSON-LD, OpenGraph and the
  canonical link live in <head> and inside elements furniture-stripping deletes.
* Media localisation runs BEFORE sanitising. The sanitiser only permits local
  ``images/`` and ``videos/`` sources, so anything not yet localised loses its
  src and is marked as missing.
* ``adopt_raw_embeds`` runs BEFORE ``restore_embeds``. Running it after would
  re-process the <video> elements restore creates and double-count them.
* The canonical DOM is built, serialised and RE-PARSED before any xpath is
  generated. See dom.py.

The extractor opens no socket and no database connection, and treats the .wacz
as read-only: a stat fence over (size, mtime_ns, inode) is checked afterwards
and voids the extraction if the archive changed underneath it.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from . import EXTRACTOR_NAME, __version__
from . import blocks as blocks_mod
from . import boilerplate, dom, links as links_mod, metadata as metadata_mod
from . import original, readability, sanitize, screenshots, videos as videos_mod
from . import wacz as wacz_mod
from .identity import ArticleLocation
from .images import ImageExtractor, images_payload
from .ngstate import article_body_html, text_length
from .output import ArchiveFingerprint, ArtifactWriter, verify_unchanged
from .sites import rules_for
from .urls import host_of
from .videos import VideoExtractor, videos_payload

log = logging.getLogger(__name__)

STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

#: The ng-state body only wins when it is meaningfully bigger, so a
#: well-served page is never second-guessed on a rounding difference.
NG_STATE_MIN_RATIO = 1.10
NG_STATE_MIN_GAIN = 100


@dataclass
class ExtractionResult:
    wacz_path: Path
    output_dir: Path | None
    status: str
    warnings: list[str] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    error: str | None = None
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_SUCCESS, STATUS_PARTIAL)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract(wacz_path, output_root, *, dry_run: bool = False,
            copy_wacz: bool = False) -> ExtractionResult:
    """Extract one archive. Never raises for article-level problems."""
    wacz_path = Path(wacz_path)
    started = time.monotonic()
    location = ArticleLocation.from_wacz(wacz_path)
    warnings: list[str] = []

    try:
        fingerprint = ArchiveFingerprint.of(wacz_path)
    except OSError as exc:
        return ExtractionResult(wacz_path, None, STATUS_FAILED,
                                error=f"cannot stat archive: {exc}")

    try:
        result = _run(wacz_path, output_root, location, warnings,
                      dry_run=dry_run, copy_wacz=copy_wacz)
    except Exception as exc:                       # noqa: BLE001 - reported, not swallowed
        log.exception("extraction failed: %s", wacz_path)
        result = ExtractionResult(wacz_path, None, STATUS_FAILED,
                                  warnings=warnings, error=f"{type(exc).__name__}: {exc}")

    # The archive must be exactly as we found it.
    verify_unchanged(wacz_path, fingerprint)

    result.duration_ms = int((time.monotonic() - started) * 1000)
    if result.output_dir is not None and not dry_run:
        _write_extraction_json(result)
    return result


def _run(wacz_path, output_root, location, warnings, *, dry_run, copy_wacz):
    contents = wacz_mod.read_archive_for_page(wacz_path)

    url = contents.page_url or contents.main_url
    if not url:
        return ExtractionResult(wacz_path, None, STATUS_FAILED, warnings=warnings,
                                error="the archive records no page URL")
    if not contents.main_html:
        return ExtractionResult(wacz_path, None, STATUS_FAILED, warnings=warnings,
                                error="the archive holds no HTML document")

    location = location.resolved(url)
    output_dir = location.output_dir(Path(output_root))
    writer = ArtifactWriter(output_dir, dry_run=dry_run)

    html_text = wacz_mod.decode_html(contents)
    rules = rules_for(location.outlet or host_of(url))

    # ---- the original document, for metadata and for archival ------------
    full_soup = BeautifulSoup(html_text, "lxml")
    writer.write_text("original.html", original.build_original_html(html_text))

    # ---- readability -----------------------------------------------------
    stripped = boilerplate.strip_furniture_html(html_text, rules.keep_furniture_tags)
    readable = readability.run_readability(stripped, url)
    body_html = readable.content_html

    # ---- ng-state fallback ----------------------------------------------
    recovered = article_body_html(html_text, url)
    if recovered:
        have, gained = text_length(body_html), text_length(recovered)
        if gained > have * NG_STATE_MIN_RATIO and gained - have >= NG_STATE_MIN_GAIN:
            log.info("%s: body recovered from ng-state (%d -> %d chars)",
                     url, have, gained)
            body_html = recovered

    reader_soup = BeautifulSoup(body_html, "lxml")

    # ---- metadata (from the ORIGINAL document) ---------------------------
    meta = metadata_mod.build_metadata(
        full_soup, url, rules,
        readability_title=readable.title, readability_byline=readable.byline)
    if not meta.title:
        warnings.append("no title could be extracted")
    if not meta.published_at:
        warnings.append("no publication date could be extracted")
    log.debug("%s: metadata sources %s", url, getattr(meta, "resolved_sources", {}))

    # ---- embeds, then media localisation, then sanitise ------------------
    readability.adopt_raw_embeds(reader_soup, url)
    readability.restore_embeds(reader_soup, readable.embeds, url)

    article_node = metadata_mod.pick_article_node(
        metadata_mod.json_ld_blocks(full_soup))
    image_extractor = ImageExtractor(contents, url, writer, rules)
    image_extractor.splice_lead_image(reader_soup, full_soup, article_node, meta.title)
    image_extractor.process(reader_soup)
    warnings.extend(image_extractor.warnings)

    video_extractor = VideoExtractor(contents, url, writer)
    video_extractor.process(reader_soup)
    video_extractor.scan_document(full_soup)
    video_extractor.attach_payloads()
    warnings.extend(video_extractor.warnings)

    # Notes are expected non-defects - a size cap, an adaptive stream that
    # cannot be muxed, a declared lead image the page never loaded. They must
    # not become warnings (that would mark a third of the corpus `partial`) and
    # they are not article metadata, so the log is where they belong.
    for note in image_extractor.notes + video_extractor.notes:
        log.info("%s: %s", url, note)

    sanitize.sanitize(reader_soup)

    # ---- the canonical document ------------------------------------------
    built = dom.build(reader_soup, metadata=meta, wacz_name=wacz_path.name)
    warnings.extend(built.warnings)
    readability_html = dom.serialize(built.tree)
    writer.write_text("readability.html", readability_html)

    # Everything below is generated against the bytes just written.
    tree = dom.reparse(readability_html)
    article_blocks, block_warnings = blocks_mod.build_blocks(tree, built.specs)
    warnings.extend(block_warnings)

    link_records, link_warnings = links_mod.extract_links(
        tree, article_blocks, host_of(meta.canonical_url or url))
    warnings.extend(link_warnings)

    if not article_blocks:
        reason = rules.detect_interstitial(full_soup.get_text(" ", strip=True))
        warnings.append(
            f"no article content blocks were extracted ({reason} interstitial)"
            if reason else "no article content blocks were extracted")

    # ---- the remaining artifacts -----------------------------------------
    writer.write_json("article.json", _article_payload(meta, location, contents))
    writer.write_json("content.json", blocks_mod.blocks_to_content(article_blocks))
    writer.write_json("images.json", images_payload(image_extractor.records))
    writer.write_json("videos.json", videos_payload(video_extractor.records))
    writer.write_json("links.json", links_mod.links_payload(link_records))

    shot = screenshots.choose(contents, wacz_path)
    if shot is not None:
        writer.write_bytes(shot.filename, shot.body)
        log.debug("%s: screenshot from %s", url, shot.source)
    else:
        warnings.append("the archive holds no screenshot and none sits beside it")

    if copy_wacz and not dry_run:
        _copy_archive(wacz_path, output_dir)

    counts = {
        "blocks": len(article_blocks),
        "images": len(image_extractor.records),
        "videos": len(video_extractor.records),
        "links": len(link_records),
        "words": blocks_mod.word_count(article_blocks),
    }
    status = STATUS_PARTIAL if (warnings or not article_blocks) else STATUS_SUCCESS
    return ExtractionResult(wacz_path, output_dir, status,
                            warnings=warnings, counts=counts)


def _article_payload(meta, location, contents) -> dict:
    """article.json: identity first, then the metadata the page declared.

    ``source_url`` and ``canonical_url`` are both kept and are not assumed
    equal - the crawled URL can redirect during capture.
    """
    payload = {
        "archive_id": location.archive_id,
        "outlet": location.outlet,
    }
    payload.update(meta.to_dict())
    payload["captured_at"] = contents.captured_at
    return payload


def _write_extraction_json(result: ExtractionResult) -> None:
    """The lifecycle record. Three fields, and no diagnostics.

    Counts, warnings, timings and per-phase statistics are logged, not
    persisted: they are facts about a run, not about an article, and the old
    extractor's habit of storing them made every re-extraction a diff.
    """
    writer = ArtifactWriter(result.output_dir)
    writer.write_json("extraction.json", {
        "extraction_version": f"{EXTRACTOR_NAME}/{__version__}",
        "extracted_at": _now_iso(),
        "extraction_status": result.status,
    })


def _copy_archive(wacz_path: Path, output_dir: Path) -> None:
    """Copy page.wacz beside the artifacts. Off by default, and never a move.

    The corpus is ~30 TB; copying every archive into a second tree would double
    it. The source path is always recorded, so the archive is findable without
    the copy.
    """
    target = Path(output_dir) / wacz_path.name
    if target.exists() and target.stat().st_size == wacz_path.stat().st_size:
        return
    shutil.copy2(wacz_path, target)
