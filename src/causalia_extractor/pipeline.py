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
from . import original, quality as quality_mod, readability, sanitize
from . import screenshots, videos as videos_mod
from . import wacz as wacz_mod
from .identity import ArticleLocation
from .images import ImageExtractor, images_payload
from .ngstate import article_body_html, text_length
from .output import ArchiveFingerprint, ArtifactWriter, verify_unchanged
from .sites import rules_for
from .urls import host_of
from .videos import VideoExtractor, videos_payload

log = logging.getLogger(__name__)

#: The stages an extraction may run. `content` is the article - metadata,
#: readability, blocks, links, images, videos. `screenshot` is one file lifted
#: out of the capture.
#:
#: THE SPLIT EXISTS BECAUSE THE SCREENSHOT IS NEITHER CHEAP NOR CHEAPLY
#: RE-CREATED. It is 4.826 MB of the 5.353 MB an article writes (90.2%), and
#: suppressing it measured 15.206 -> 22.434 archives/s - 1.48x, because sustained
#: throughput on this hardware is bound by write bandwidth. It is also
#: STRATEGICALLY REQUIRED, so it is never removed: it is switched off for a run
#: and backfilled later, from the same .wacz, which stays the source of truth.
#:
#: Nothing is rendered in either stage. Browsertrix already took the screenshot
#: and stored it in the WARC as a `resource` record under urn:<variant>; this
#: extractor never starts a browser.
STAGE_CONTENT = "content"
STAGE_SCREENSHOT = "screenshot"
ALL_STAGES = (STAGE_CONTENT, STAGE_SCREENSHOT)

STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

#: What became of the screenshot. Mirrors corpus.extraction_task.screenshot_state.
SCREENSHOT_PRESENT = "present"
SCREENSHOT_NONE = "none_in_archive"
SCREENSHOT_SKIPPED = "skipped"
SCREENSHOT_FAILED = "failed"

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
    #: The quality contract's verdict - see quality.py. `status` is the
    #: database-facing spelling of the same fact.
    verdict: quality_mod.Verdict | None = None
    #: Every artifact written for this article, relative to the article
    #: directory. This is the manifest extraction.json publishes, and it is
    #: what makes a directory provably complete rather than merely present.
    artifacts: list[str] = field(default_factory=list)
    #: True when an identity was established, so a failure can still be
    #: recorded on disk at the place the article would have lived.
    marker_dir: Path | None = None
    #: What happened to the screenshot: present, none_in_archive, or skipped
    #: because the stage was not run. `skipped` and `none_in_archive` are
    #: different facts and a backfill needs to tell them apart.
    screenshot_state: str = "skipped"

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_SUCCESS, STATUS_PARTIAL)

    @property
    def quality(self) -> str:
        if self.verdict is not None:
            return self.verdict.quality
        return quality_mod.QUALITY_INVALID


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract(wacz_path, output_root, *, dry_run: bool = False,
            copy_wacz: bool = False, force: bool = False,
            stages=ALL_STAGES) -> ExtractionResult:
    """Extract one archive. Never raises for article-level problems.

    ``stages`` selects what to produce. Dropping ``screenshot`` leaves the
    article identical and writes 4.8 MB less per capture; a later
    ``extract_screenshot`` pass fills it in without re-reading metadata, blocks
    or links, and without creating a new reading of the article.

    ``force`` sweeps this extractor's own prior artifacts before writing, so a
    re-extraction cannot leave a stale file from the previous reading beside the
    new one - an images/image_004.jpg a newer reading no longer produces, say.
    It walks the ALLOWLIST rather than the directory, so anything this extractor
    did not write is left strictly alone, and `screenshot.*` is deliberately not
    in that allowlist: an existing screenshot is never deleted.
    """
    wacz_path = Path(wacz_path)
    started = time.monotonic()
    location = ArticleLocation.from_wacz(wacz_path)
    warnings: list[str] = []

    try:
        fingerprint = ArchiveFingerprint.of(wacz_path)
    except OSError as exc:
        return ExtractionResult(
            wacz_path, None, STATUS_FAILED,
            error=f"cannot stat archive: {exc}",
            verdict=quality_mod.invalid(quality_mod.ARCHIVE_NOT_STATABLE))

    try:
        result = _run(wacz_path, output_root, location, warnings,
                      dry_run=dry_run, copy_wacz=copy_wacz, force=force,
                      stages=tuple(stages))
    except wacz_mod.ArchiveUnreadable as exc:
        log.error("archive unreadable: %s: %s", wacz_path, exc)
        result = ExtractionResult(
            wacz_path, None, STATUS_FAILED, warnings=warnings,
            error=f"{type(exc).__name__}: {exc}",
            verdict=quality_mod.invalid(quality_mod.ARCHIVE_UNREADABLE))
    except Exception as exc:                       # noqa: BLE001 - reported, not swallowed
        log.exception("extraction failed: %s", wacz_path)
        result = ExtractionResult(
            wacz_path, None, STATUS_FAILED, warnings=warnings,
            error=f"{type(exc).__name__}: {exc}",
            verdict=quality_mod.invalid(quality_mod.EXTRACTION_ERROR))

    # The archive must be exactly as we found it.
    verify_unchanged(wacz_path, fingerprint)

    result.duration_ms = int((time.monotonic() - started) * 1000)
    if dry_run:
        return result

    if result.output_dir is not None:
        _write_extraction_json(result, result.output_dir)
        return result

    # A FAILURE IS A FACT ABOUT AN ARTICLE, SO IT HAS TO BE WRITTEN DOWN.
    # Every failure path above returns output_dir=None, which used to mean
    # nothing at all reached disk: a corrupt archive produced a log line and no
    # other trace, so `extraction_status = failed` could never be written by any
    # producer and a re-run repeated the same failure forever. When identity is
    # known - which it is for every archive sitting in the corpus layout - the
    # marker is written at the place the article would have lived. Only the
    # marker: no artifacts were produced, and none are invented here.
    if location.archive_id:
        result.marker_dir = location.output_dir(Path(output_root))
        _write_extraction_json(result, result.marker_dir)
    return result


def _run(wacz_path, output_root, location, warnings, *, dry_run, copy_wacz,
         force=False, stages=ALL_STAGES):
    contents = wacz_mod.read_archive_for_page(wacz_path)

    url = contents.page_url or contents.main_url
    if not url:
        return ExtractionResult(wacz_path, None, STATUS_FAILED, warnings=warnings,
                                error="the archive records no page URL",
                                verdict=quality_mod.invalid(quality_mod.NO_PAGE_URL))
    if not contents.main_html:
        return ExtractionResult(wacz_path, None, STATUS_FAILED, warnings=warnings,
                                error="the archive holds no HTML document",
                                verdict=quality_mod.invalid(
                                    quality_mod.NO_HTML_DOCUMENT))

    location = location.resolved(url)
    output_dir = location.output_dir(Path(output_root))
    writer = ArtifactWriter(output_dir, dry_run=dry_run)
    if force:
        removed = writer.remove_previous_artifacts()
        if removed:
            log.info("%s: swept %d prior artifact(s)", output_dir, len(removed))

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

    screenshot_state = SCREENSHOT_SKIPPED
    if STAGE_SCREENSHOT in stages:
        shot = screenshots.choose(contents, wacz_path)
        if shot is not None:
            writer.write_bytes(shot.filename, shot.body)
            log.debug("%s: screenshot from %s", url, shot.source)
            screenshot_state = SCREENSHOT_PRESENT
        else:
            screenshot_state = SCREENSHOT_NONE
            # A warning makes the article partial_valid. That is right when we
            # LOOKED and the capture had none; it would be nonsense when the
            # stage was switched off, which is why this sits inside the branch.
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
    verdict = quality_mod.assess(article_blocks, title=meta.title,
                                 published_at=meta.published_at,
                                 warnings=warnings)
    return ExtractionResult(wacz_path, output_dir, verdict.extraction_status,
                            warnings=warnings, counts=counts, verdict=verdict,
                            artifacts=list(writer.written),
                            screenshot_state=screenshot_state)


@dataclass
class ScreenshotResult:
    """One screenshot-only pass: no reading of the article is created."""
    wacz_path: Path
    output_dir: Path | None
    state: str
    filename: str | None = None
    source: str | None = None
    error: str | None = None
    duration_ms: int = 0


def extract_screenshot(wacz_path, output_root, *,
                       dry_run: bool = False) -> ScreenshotResult:
    """Lift the screenshot out of one capture. Nothing else is touched.

    THIS IS THE WHOLE POINT OF THE STAGE SPLIT: an urgent run can be extracted
    without screenshots and the screenshots filled in afterwards, from the same
    archives, without re-reading metadata or re-deriving a single block - and
    therefore without creating a new `article_extraction`, which would
    invalidate nothing but would churn every citation's provenance for no
    reason.

    It is not free. `read_archive` is 45% of a full extraction's 908 ms, so a
    backfill pass costs roughly 0.56 of an extraction pass plus the bytes it
    writes. Deferring buys time-to-searchable, not total machine time.

    ``html_only=True`` is what makes it as cheap as it can be: image and video
    bodies are skipped without being buffered, while the screenshot record is
    still collected. The article document is not even needed - only pages.jsonl,
    for identity when the path cannot supply it.

    ``extraction.json`` is deliberately NOT rewritten. That marker describes the
    CONTENT extraction and its manifest; a screenshot arriving later is a
    separate fact, recorded in corpus.extraction_task.screenshot_state and in
    corpus.article_artifact. Leaving the marker alone also means this pass can
    never race a content extraction into a half-written file.
    """
    wacz_path = Path(wacz_path)
    started = time.monotonic()
    location = ArticleLocation.from_wacz(wacz_path)

    try:
        fingerprint = ArchiveFingerprint.of(wacz_path)
    except OSError as exc:
        return ScreenshotResult(wacz_path, None, SCREENSHOT_FAILED,
                                error=f"cannot stat archive: {exc}")

    try:
        contents = wacz_mod.read_archive(wacz_path, html_only=True)
    except Exception as exc:                       # noqa: BLE001
        log.exception("screenshot pass failed: %s", wacz_path)
        return ScreenshotResult(wacz_path, None, SCREENSHOT_FAILED,
                                error=f"{type(exc).__name__}: {exc}",
                                duration_ms=int((time.monotonic() - started) * 1000))

    location = location.resolved(contents.page_url)
    output_dir = location.output_dir(Path(output_root))
    shot = screenshots.choose(contents, wacz_path)

    if shot is None:
        verify_unchanged(wacz_path, fingerprint)
        return ScreenshotResult(wacz_path, output_dir, SCREENSHOT_NONE,
                                duration_ms=int((time.monotonic() - started) * 1000))

    if not dry_run:
        ArtifactWriter(output_dir).write_bytes(shot.filename, shot.body)
    verify_unchanged(wacz_path, fingerprint)
    return ScreenshotResult(wacz_path, output_dir, SCREENSHOT_PRESENT,
                            filename=shot.filename, source=shot.source,
                            duration_ms=int((time.monotonic() - started) * 1000))


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


def _write_extraction_json(result: ExtractionResult, directory: Path) -> None:
    """THE COMMIT MARKER. Written last, after every other artifact.

    Counts, warnings, timings and per-phase statistics are still logged rather
    than persisted: they are facts about a run, not about an article, and the
    old extractor's habit of storing them made every re-extraction a diff.

    What IS persisted, beyond the original three fields:

    * ``quality`` and ``quality_reasons`` - the contract in quality.py, so a
      consumer never has to re-derive "is this article usable" from warnings it
      cannot see.
    * ``artifacts`` - the manifest. Every individual artifact is written
      atomically, but the SET of them is not: the directory is built up over
      ~900 ms and a worker killed in the middle leaves a plausible-looking
      article. Listing what must be there turns "the directory exists" into
      "the directory is complete", which a loader can actually check.

    ``wacz_sha256`` is deliberately NOT here. Hashing the capture would mean a
    second full read of 4.9 TB for a value public.archives already holds;
    ingestion copies it from there.
    """
    verdict = result.verdict
    writer = ArtifactWriter(directory)
    writer.write_json("extraction.json", {
        "extraction_version": f"{EXTRACTOR_NAME}/{__version__}",
        "extracted_at": _now_iso(),
        "extraction_status": result.status,
        "quality": verdict.quality if verdict else quality_mod.QUALITY_INVALID,
        "quality_reasons": list(verdict.reasons) if verdict else
                           [quality_mod.EXTRACTION_ERROR],
        "artifacts": sorted(result.artifacts),
        "error": result.error,
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
