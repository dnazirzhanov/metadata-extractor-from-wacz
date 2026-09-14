"""The ingestion path: extractor output -> the corpus schema.

Extracted from scripts/validate_ingestion.py so the throwaway validation
harness and the development-database loader run the SAME SQL. Two copies of an
INSERT is how a validated schema quietly stops being the one you are loading.

This is the single-article INSERT path. Batching, retry, concurrency and the
work ledger live in ingest/, which calls this function - so there is still
exactly one copy of the SQL, and the development harnesses exercise the same
statements production runs.

Ingestion is strictly READ-ONLY over the extractor output. Nothing here opens a
.wacz, and psycopg2 is the optional [db] extra - the extractor package itself
still opens no socket.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from causalia_extractor.identity import (                     # noqa: E402
    archive_id_for, canonical_url_for_identity)
from causalia_extractor.quality import USABLE                 # noqa: E402

#: Artifacts a loadable article directory must hold, whatever the manifest says.
#: The manifest is the extractor's own list; this is the ingestion path's
#: independent floor, so a truncated manifest cannot smuggle a directory in.
REQUIRED_ARTIFACTS = ("article.json", "content.json", "images.json",
                      "videos.json", "links.json", "extraction.json")


class CaptureNotFound(Exception):
    """No successful capture in public.archives for this article.

    Ingestion refuses rather than inventing one. The capture is the evidence
    anchor; an article whose capture the crawler has no record of cannot be
    traced back to anything.
    """


class IncompleteExtraction(Exception):
    """The article directory is not a completed extraction.

    Either the commit marker is missing, or an artifact the marker promises is
    not on disk. Measured on the 2026-09-10 benchmark output: 5 directories in
    46,253 were left in exactly this state by killed workers, and the loader
    died on the first one it reached.
    """


class UnusableExtraction(Exception):
    """The extraction is `invalid` under the quality contract.

    Never ingested, so it can never become searchable - the strongest of the
    three enforcement layers, because no row exists for a query to reach.
    """


def load(directory: Path, name: str):
    return json.loads((directory / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------
# Ingestion. One transaction per article, insert-then-flip.
# ---------------------------------------------------------------------

def resolve_capture(cur, url_hash: str):
    """The capture this article was extracted from. A LOOKUP, never a write.

    THIS REPLACED AN INSERT, AND THAT IS THE WHOLE POINT. The previous version
    INSERTed a fresh `urls` row and a fresh `archives` row for every article on
    every run, and hashed page.wacz when it found one. Pointed at production
    that fabricates one capture record per article per ingest - corrupting the
    crawler's own history rather than merely being slow - and re-reads 4.9 TB to
    recompute a hash `archives.wacz_sha256` already holds. It was honest about
    being a development stand-in; nothing stopped it being pointed at the real
    database, and the 2026-09-10 benchmark had to neutralise it before it could
    measure anything.

    Ingestion now writes NOTHING outside the `corpus` schema. The one outbound
    reference is the read below.

    The newest successful capture wins: `archives` is append-only history and a
    URL can accumulate several. Ordering by finished_at makes the choice
    explicit instead of leaving it to whatever the planner returns.
    """
    cur.execute("""
        SELECT id, wacz_sha256
          FROM archives
         WHERE url_hash = %s AND status = 'success'
         ORDER BY finished_at DESC NULLS LAST, id DESC
         LIMIT 1
    """, (url_hash,))
    row = cur.fetchone()
    if row is None:
        raise CaptureNotFound(
            f"no successful capture in public.archives for {url_hash[:12]}...; "
            f"refusing to invent one")
    return row[0], row[1]


def read_marker(directory: Path) -> dict:
    """Load extraction.json and prove the directory is complete.

    Presence of a directory proves nothing: artifacts are written one at a time
    over ~900 ms and each is individually atomic, but the SET of them is not.
    `extraction.json` is written last, after every other artifact, which makes
    it a commit marker - and since 2026-09-11 it also carries the manifest of
    what it committed, so "complete" is checkable rather than assumed.
    """
    marker = directory / "extraction.json"
    if not marker.is_file():
        raise IncompleteExtraction(f"{directory}: no extraction.json")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IncompleteExtraction(f"{directory}: unreadable marker: {exc}") from None

    quality = payload.get("quality")
    if quality is None:
        raise IncompleteExtraction(
            f"{directory}: marker predates the quality contract; re-extract")
    if quality not in USABLE:
        raise UnusableExtraction(
            f"{directory}: quality={quality} "
            f"({', '.join(payload.get('quality_reasons') or [])})")

    missing = [name for name in REQUIRED_ARTIFACTS
               if not (directory / name).is_file()]
    missing += [name for name in payload.get("artifacts") or []
                if not (directory / name).exists()]
    if missing:
        raise IncompleteExtraction(
            f"{directory}: marker promises artifacts that are not there: "
            f"{', '.join(sorted(set(missing))[:5])}")
    return payload


def seed_crawler_rows(cur, directory: Path, article: dict, *,
                      confirm_development: bool = False):
    """DEVELOPMENT ONLY. Create the urls/archives rows a throwaway DB lacks.

    Kept because the validation harnesses run against empty throwaway databases
    where the FK has nothing to resolve against, and exercising the constraint
    beats avoiding it. It is NOT reachable from `ingest()` any more, and it
    refuses to run unless the caller says out loud that it is a development
    database.
    """
    if not confirm_development:
        raise RuntimeError(
            "seed_crawler_rows writes to the CRAWLER's tables and must never "
            "run against production; pass confirm_development=True from a "
            "harness that owns its database")
    cur.execute(
        "INSERT INTO urls (url_hash, url, outlet) VALUES (%s, %s, %s) "
        "ON CONFLICT (url_hash) DO NOTHING",
        (article["archive_id"], canonical_url_for_identity(article["source_url"]),
         article["outlet"]))
    wacz = directory / "page.wacz"
    sha = (hashlib.sha256(wacz.read_bytes()).hexdigest() if wacz.exists()
           else hashlib.sha256(article["archive_id"].encode()).hexdigest())
    cur.execute(
        "INSERT INTO archives (url_hash, outlet, status, wacz_sha256, wacz_path, "
        "                      finished_at) "
        "VALUES (%s, %s, 'success', %s, %s, now()) RETURNING id",
        (article["archive_id"], article["outlet"], sha,
         f"{article['outlet']}/{article['archive_id'][:2]}/{article['archive_id']}/page.wacz"))
    return cur.fetchone()[0], sha


SCREENSHOT_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
                          ".jpeg": "image/jpeg", ".webp": "image/webp"}


def find_screenshot(directory: Path):
    """The screenshot to record, preferring a Browsertrix capture over a sidecar.

    A directory can hold more than one: an early capture carries the 2026-08-07
    Playwright `screenshot.webp` beside the archive, and a later re-extraction
    can write the in-archive `screenshot.png` next to it. The previous code took
    `sorted(glob("screenshot.*"))[-1]`, so ALPHABETICAL ORDER decided which one
    the database recorded - and .webp sorts last, which is exactly the wrong
    way round. Browsertrix's own capture is the better evidence, so it wins
    explicitly.
    """
    candidates = [p for p in sorted(directory.glob("screenshot.*"))
                  if p.suffix.lower() in SCREENSHOT_MEDIA_TYPES]
    if not candidates:
        return None
    raster = [p for p in candidates if p.suffix.lower() != ".webp"]
    return (raster or candidates)[0]


def attach_screenshot(cur, url_hash: str, directory: Path,
                      relative_dir: str) -> str | None:
    """Record a screenshot against an article that is ALREADY ingested.

    This is what makes the screenshot stage independently rerunnable end to end:
    a backfill writes the file and then calls this, and no new
    `article_extraction` is created. Creating one would be wrong - nothing about
    the READING of the article changed, and a new reading would supersede the
    one every existing citation was verified against.

    Returns the artifact path written, or None when the article is not in the
    corpus yet (in which case the ordinary ingest will pick the file up).
    """
    shot = find_screenshot(directory)
    if shot is None:
        return None
    cur.execute("""
        SELECT a.id, a.current_extraction_id
          FROM corpus.article a WHERE a.url_hash = %s
    """, (url_hash,))
    row = cur.fetchone()
    if row is None:
        return None
    article_id, extraction_id = row
    file_path = f"{relative_dir}/{shot.name}"
    cur.execute("""
        INSERT INTO corpus.article_artifact
            (article_id, extraction_id, kind, file_path, media_type, byte_size)
        VALUES (%s, %s, 'screenshot', %s, %s, %s)
        ON CONFLICT (article_id, kind) DO UPDATE SET
            extraction_id = EXCLUDED.extraction_id,
            file_path = EXCLUDED.file_path,
            media_type = EXCLUDED.media_type,
            byte_size = EXCLUDED.byte_size
    """, (article_id, extraction_id, file_path,
          SCREENSHOT_MEDIA_TYPES[shot.suffix.lower()], shot.stat().st_size))
    return file_path


def ingest(cur, directory: Path, *, capture=None) -> tuple[int, int, int]:
    """Ingest one article directory, in the caller's transaction.

    Returns (article_id, extraction_id, synthetic_links_skipped).

    Raises IncompleteExtraction if the directory is not a completed extraction,
    UnusableExtraction if the quality contract rejected it, and CaptureNotFound
    if the crawler has no successful capture for it. All three are refusals, not
    errors to paper over: each one would otherwise put a row in the corpus that
    cannot be traced back to an archived page.

    ``capture`` is (archive_row_id, wacz_sha256) when the caller has already
    resolved it - the batch loader looks them up once per batch rather than once
    per article.
    """
    extraction = read_marker(directory)
    article = load(directory, "article.json")
    blocks = load(directory, "content.json")["blocks"]
    images = load(directory, "images.json")
    videos = load(directory, "videos.json")
    links = load(directory, "links.json")

    archive_row_id, wacz_sha = (
        capture if capture is not None
        else resolve_capture(cur, article["archive_id"]))
    rel = f"{article['outlet']}/{article['archive_id'][:2]}/{article['archive_id']}"

    # --- article: UPSERT. Metadata is corrected in place by re-extraction.
    cur.execute("""
        INSERT INTO corpus.article (
            url_hash, outlet, source_url, canonical_url, title, subtitle,
            description, authors, publisher, section, language, tags,
            published_at, updated_at_source, captured_at,
            published_at_raw, updated_at_raw)
        VALUES (%(url_hash)s, %(outlet)s, %(source_url)s, %(canonical_url)s,
                %(title)s, %(subtitle)s, %(description)s, %(authors)s,
                %(publisher)s, %(section)s, %(language)s, %(tags)s,
                %(published_at)s, %(updated_at)s, %(captured_at)s,
                %(published_at_raw)s, %(updated_at_raw)s)
        ON CONFLICT (url_hash) DO UPDATE SET
            outlet = EXCLUDED.outlet, source_url = EXCLUDED.source_url,
            canonical_url = EXCLUDED.canonical_url, title = EXCLUDED.title,
            subtitle = EXCLUDED.subtitle, description = EXCLUDED.description,
            authors = EXCLUDED.authors, publisher = EXCLUDED.publisher,
            section = EXCLUDED.section, language = EXCLUDED.language,
            tags = EXCLUDED.tags, published_at = EXCLUDED.published_at,
            updated_at_source = EXCLUDED.updated_at_source,
            captured_at = EXCLUDED.captured_at,
            published_at_raw = EXCLUDED.published_at_raw,
            updated_at_raw = EXCLUDED.updated_at_raw,
            row_updated_at = now()
        RETURNING id
    """, {
        "url_hash": article["archive_id"], "outlet": article["outlet"],
        "source_url": article["source_url"], "canonical_url": article["canonical_url"],
        "title": article["title"], "subtitle": article["subtitle"],
        "description": article["description"], "authors": article["author"],
        "publisher": article["publisher"], "section": article["section"],
        "language": article["language"], "tags": article["tags"],
        # The parsed forms; the raw strings are kept beside them.
        "published_at": article["published_at"], "updated_at": article["updated_at"],
        "captured_at": article["captured_at"],
        "published_at_raw": article["published_at"],
        "updated_at_raw": article["updated_at"],
    })
    article_id = cur.fetchone()[0]

    # --- the new reading, not yet current
    # content_block_count is denormalised deliberately: corpus.searchable_article
    # tests it, and a per-row EXISTS over content_block in the search candidate
    # path would put an index lookup on the hot query for every candidate
    # article. It is written in the same transaction as the blocks it counts, so
    # it cannot drift. See migrations/025.
    prose_blocks = sum(1 for b in blocks
                       if b["type"] not in ("image", "video")
                       and (b.get("text") or "").strip())
    cur.execute("""
        INSERT INTO corpus.article_extraction
            (article_id, extractor_version, extraction_status, extracted_at,
             wacz_sha256, archive_row_id, is_current, content_block_count)
        VALUES (%s, %s, %s, %s, %s, %s, false, %s) RETURNING id
    """, (article_id, extraction["extraction_version"],
          extraction["extraction_status"], extraction["extracted_at"],
          wacz_sha, archive_row_id, prose_blocks))
    extraction_id = cur.fetchone()[0]

    # --- images and videos before blocks, because a block references them
    image_ids: dict[str, int] = {}
    for record in images:
        cur.execute("""
            INSERT INTO corpus.article_image
                (article_id, extraction_id, local_ref, file_path, original_url,
                 media_type, width, height, alt, caption, credit, is_available)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (article_id, extraction_id, record["id"],
              f"{rel}/{record['filename']}" if record["filename"] else None,
              record["original_url"], record["mime_type"], record["width"],
              record["height"], record["alt"], record["caption"],
              record["credit"], record["image_available"]))
        image_ids[record["id"]] = cur.fetchone()[0]

    video_ids: dict[str, int] = {}
    for record in videos:
        cur.execute("""
            INSERT INTO corpus.article_video
                (article_id, extraction_id, local_ref, platform, external_id,
                 source_type, canonical_url, embed_url, thumbnail_url, title,
                 caption, file_path, is_archived)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (article_id, extraction_id, record["id"], record["platform"],
              record["external_id"], record["type"], record["url"],
              record["embed_url"], record["thumbnail_url"], record["title"],
              record["caption"],
              f"{rel}/{record['local_file']}" if record["local_file"] else None,
              record["archived"]))
        video_ids[record["id"]] = cur.fetchone()[0]

    # --- content blocks, in document order
    block_ids: dict[str, int] = {}       # xpath -> id, for the link join
    for block in blocks:
        cur.execute("""
            INSERT INTO corpus.content_block
                (extraction_id, article_id, block_index, block_type, xpath,
                 block_text, heading_level, image_id, video_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (extraction_id, article_id, block["index"], block["type"],
              block["xpath"], block.get("text"), block.get("level"),
              image_ids.get(block.get("image_id")),
              video_ids.get(block.get("video_id"))))
        block_ids[block["xpath"]] = cur.fetchone()[0]

    # --- links, with their selector
    #
    # INGESTION RULE, found by this harness: skip a link whose selector targets
    # an image or video block. Those are not article links - they are the
    # reader view's OWN fallback anchors, which dom.py writes inside
    # <div class="embed"> as '<platform>: <url>' so an offline page can reach a
    # player it must never auto-load. links.py then read them back as if the
    # journalist had written them: 7 of 20 links in the first sample, exactly
    # the rows whose `context` is null.
    #
    # links.py was fixed on 2026-09-01, so on freshly extracted output this
    # counter reads zero and the rule is a no-op. It stays as a GUARD: output
    # extracted before that date still carries these rows, and the ingestion
    # layer must not import them. A non-zero count means pre-fix output, not a
    # new defect. Nothing is lost either way - the URL is on the video row as
    # embed_url, and the canonical watch URL as `url`.
    media_block_xpaths = {b["xpath"] for b in blocks
                          if b["type"] in ("image", "video")}
    skipped_synthetic = 0
    for record in links:
        selector = record.get("selector") or {}
        if selector.get("value") in media_block_xpaths:
            skipped_synthetic += 1
            continue
        refined = selector.get("refinedBy") or {}
        quote = selector.get("quote") or {}
        target_hash = (archive_id_for(record["url"])
                       if record["url"].startswith(("http://", "https://")) else None)
        cur.execute("""
            INSERT INTO corpus.article_link
                (article_id, extraction_id, content_block_id, target_url,
                 target_url_hash, anchor_text, context, is_internal,
                 selector_xpath, quote_start, quote_end, quote_exact,
                 quote_prefix, quote_suffix)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (article_id, extraction_id, block_ids.get(selector.get("value")),
              record["url"], target_hash, record["text"], record["context"],
              record["internal"], selector.get("value"),
              refined.get("start"), refined.get("end"),
              quote.get("exact"), quote.get("prefix"), quote.get("suffix")))

    # --- artifacts: paths only, relative to the storage root
    for name, kind, media in (("readability.html", "readability_html", "text/html"),
                              ("original.html", "original_html", "text/html")):
        path = directory / name
        if path.exists():
            cur.execute("""
                INSERT INTO corpus.article_artifact
                    (article_id, extraction_id, kind, file_path, media_type, byte_size)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (article_id, kind) DO UPDATE SET
                    extraction_id = EXCLUDED.extraction_id,
                    file_path = EXCLUDED.file_path,
                    media_type = EXCLUDED.media_type,
                    byte_size = EXCLUDED.byte_size
            """, (article_id, extraction_id, kind, f"{rel}/{name}", media,
                  path.stat().st_size))
    shot = find_screenshot(directory)
    if shot is not None:
        media = SCREENSHOT_MEDIA_TYPES[shot.suffix.lower()]
        cur.execute("""
            INSERT INTO corpus.article_artifact
                (article_id, extraction_id, kind, file_path, media_type, byte_size)
            VALUES (%s,%s,'screenshot',%s,%s,%s)
            ON CONFLICT (article_id, kind) DO UPDATE SET
                extraction_id = EXCLUDED.extraction_id,
                file_path = EXCLUDED.file_path,
                media_type = EXCLUDED.media_type,
                byte_size = EXCLUDED.byte_size
        """, (article_id, extraction_id, f"{rel}/{shot.name}", media,
              shot.stat().st_size))

    # --- flip: the new reading becomes current, the old one stops being
    cur.execute("SELECT id FROM corpus.article_extraction "
                "WHERE article_id = %s AND is_current", (article_id,))
    previous = [row[0] for row in cur.fetchall()]
    cur.execute("UPDATE corpus.article_extraction SET is_current = false "
                "WHERE article_id = %s AND is_current", (article_id,))
    cur.execute("UPDATE corpus.article_extraction SET is_current = true WHERE id = %s",
                (extraction_id,))
    cur.execute("UPDATE corpus.article SET current_extraction_id = %s WHERE id = %s",
                (extraction_id, article_id))
    # Superseded content is deleted; the extraction row is kept as an audit trail.
    for old in previous:
        cur.execute("DELETE FROM corpus.content_block WHERE extraction_id = %s", (old,))
        cur.execute("DELETE FROM corpus.article_link WHERE extraction_id = %s", (old,))
        cur.execute("DELETE FROM corpus.article_image WHERE extraction_id = %s", (old,))
        cur.execute("DELETE FROM corpus.article_video WHERE extraction_id = %s", (old,))

    return article_id, extraction_id, skipped_synthetic
