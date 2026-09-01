"""The ingestion path: extractor output -> the corpus schema.

Extracted from scripts/validate_ingestion.py so the throwaway validation
harness and the development-database loader run the SAME SQL. Two copies of an
INSERT is how a validated schema quietly stops being the one you are loading.

This is still not a production pipeline: it has no batching, no retry, no
concurrency and no CLI of its own. It is one function that puts one article
directory into the database in one transaction, which is exactly what both
callers need.

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


def load(directory: Path, name: str):
    return json.loads((directory / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------
# Ingestion. One transaction per article, insert-then-flip.
# ---------------------------------------------------------------------

def seed_crawler_rows(cur, directory: Path, article: dict) -> None:
    """The urls/archives rows the FK depends on.

    In production these already exist - the capture only exists because the
    crawler claimed the URL. Here they are seeded so the FK can be exercised
    rather than avoided.
    """
    cur.execute(
        "INSERT INTO urls (url_hash, url, outlet) VALUES (%s, %s, %s) "
        "ON CONFLICT (url_hash) DO NOTHING",
        (article["archive_id"], canonical_url_for_identity(article["source_url"]),
         article["outlet"]))
    wacz = directory / "page.wacz"
    sha = (hashlib.sha256(wacz.read_bytes()).hexdigest() if wacz.exists()
           else hashlib.sha256(article["archive_id"].encode()).hexdigest())
    cur.execute(
        "INSERT INTO archives (url_hash, outlet, status, wacz_sha256, wacz_path) "
        "VALUES (%s, %s, 'success', %s, %s) RETURNING id",
        (article["archive_id"], article["outlet"], sha,
         f"{article['outlet']}/{article['archive_id'][:2]}/{article['archive_id']}/page.wacz"))
    return cur.fetchone()[0], sha


def ingest(cur, directory: Path) -> tuple[int, int, int]:
    """Ingest one article directory.

    Returns (article_id, extraction_id, synthetic_links_skipped).
    """
    article = load(directory, "article.json")
    extraction = load(directory, "extraction.json")
    blocks = load(directory, "content.json")["blocks"]
    images = load(directory, "images.json")
    videos = load(directory, "videos.json")
    links = load(directory, "links.json")

    archive_row_id, wacz_sha = seed_crawler_rows(cur, directory, article)
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
    cur.execute("""
        INSERT INTO corpus.article_extraction
            (article_id, extractor_version, extraction_status, extracted_at,
             wacz_sha256, archive_row_id, is_current)
        VALUES (%s, %s, %s, %s, %s, %s, false) RETURNING id
    """, (article_id, extraction["extraction_version"],
          extraction["extraction_status"], extraction["extracted_at"],
          wacz_sha, archive_row_id))
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
    for shot in sorted(directory.glob("screenshot.*")):
        media = {".png": "image/png", ".jpg": "image/jpeg",
                 ".jpeg": "image/jpeg", ".webp": "image/webp"}[shot.suffix]
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
