"""Prove the schema can hold real extractor output, and that the invariants
survive the round trip through PostgreSQL.

This is SCHEMA VALIDATION, not the production ingestion pipeline. It is a
throwaway harness that answers one question: if we build this schema, does the
extractor's actual output fit it, and do the guarantees the extractor's own test
suite asserts on the files still hold once the data is in the database?

Nothing here touches milab2. It expects a local throwaway Postgres reachable on
127.0.0.1 with the migrations already applied - scripts/validate_ingestion.sh
sets that up and tears it down.

Usage:  scripts/validate_ingestion.sh [<extraction-output-dir>]
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from causalia_extractor.dom import reparse                     # noqa: E402
from causalia_extractor.identity import archive_id_for, canonical_url_for_identity  # noqa: E402
from causalia_extractor.normalize import normalize_text        # noqa: E402
from causalia_extractor.xpath import resolve                   # noqa: E402

DSN = os.environ.get("CX_VALIDATE_DSN",
                     "host=127.0.0.1 port=55432 user=causalia password=validate dbname=causalia")

failures: list[str] = []
checks = 0


def check(condition: bool, message: str) -> None:
    global checks
    checks += 1
    if not condition:
        failures.append(message)


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


# ---------------------------------------------------------------------
# The assertions. This is the part that matters.
# ---------------------------------------------------------------------

def verify_counts(cur, directory: Path, article_id: int) -> None:
    """What went in came out."""
    blocks = load(directory, "content.json")["blocks"]
    for table, expected in (
            ("content_block", len(blocks)),
            ("article_image", len(load(directory, "images.json"))),
            ("article_video", len(load(directory, "videos.json"))),
            # links are filtered at ingestion, so the expectation is the
            # subset that is genuinely article prose
            ("article_link", len([
                r for r in load(directory, "links.json")
                if (r.get("selector") or {}).get("value") not in
                   {b["xpath"] for b in blocks
                    if b["type"] in ("image", "video")}]))):
        cur.execute(f"SELECT count(*) FROM corpus.{table} WHERE article_id = %s",
                    (article_id,))
        got = cur.fetchone()[0]
        check(got == expected,
              f"{directory.name[:8]}/{table}: {got} rows, expected {expected}")


def verify_invariant_a(cur, directory: Path, article_id: int) -> None:
    """content_block.block_text == normalize_text(XPath(readability.html)).

    The extractor's own suite asserts this on the files. The point here is that
    it must still be true of what the DATABASE holds - if ingestion mangles a
    Hungarian character or an offset, this is where it shows.
    """
    tree = reparse((directory / "readability.html").read_text(encoding="utf-8"))
    cur.execute("""
        SELECT block_index, block_type, xpath, block_text
        FROM corpus.content_block WHERE article_id = %s ORDER BY block_index
    """, (article_id,))
    rows = cur.fetchall()
    for index, block_type, xpath, block_text in rows:
        element = resolve(tree, xpath)
        check(element is not None,
              f"{directory.name[:8]} block {index}: xpath from the DB does not "
              f"resolve: {xpath}")
        if element is not None and block_text is not None:
            check(block_text == normalize_text(element),
                  f"{directory.name[:8]} block {index}: DB text != document text")
    # Ordering must be contiguous 1..n, which is what makes block_index usable
    # as reading order and catches a truncated ingestion.
    check([r[0] for r in rows] == list(range(1, len(rows) + 1)),
          f"{directory.name[:8]}: block_index is not contiguous from 1")


def verify_invariant_b(cur, directory: Path, article_id: int) -> None:
    """Selector offsets and quote agree - checked in SQL, then in the document.

    The SQL half needs no DOM: given the block's text, the substring at
    [start, end) must equal quote_exact. That is the check the repair job in the
    re-extraction strategy runs at scale.
    """
    cur.execute("""
        SELECT l.id, l.anchor_text, l.selector_xpath, l.quote_start, l.quote_end,
               l.quote_exact,
               substring(b.block_text FROM l.quote_start + 1
                         FOR l.quote_end - l.quote_start) AS sliced
        FROM corpus.article_link l
        JOIN corpus.content_block b ON b.id = l.content_block_id
        WHERE l.article_id = %s AND l.selector_xpath IS NOT NULL
    """, (article_id,))
    rows = cur.fetchall()
    for _id, anchor, xpath, start, end, exact, sliced in rows:
        check(sliced == exact,
              f"{directory.name[:8]}: SQL slice != quote_exact for {anchor[:30]!r}")
        check(exact == anchor,
              f"{directory.name[:8]}: quote_exact != anchor_text for {anchor[:30]!r}")

    # And the same selectors against the real document, the way a frontend would.
    tree = reparse((directory / "readability.html").read_text(encoding="utf-8"))
    for _id, anchor, xpath, start, end, exact, _sliced in rows:
        element = resolve(tree, xpath)
        check(element is not None, f"{directory.name[:8]}: selector xpath unresolved")
        if element is not None:
            check(normalize_text(element)[start:end] == exact,
                  f"{directory.name[:8]}: document slice != quote_exact")


def verify_invariants_cd(cur, article_id: int, label: str) -> None:
    """Every media block resolves to a record; a record needs no block."""
    cur.execute("""
        SELECT count(*) FROM corpus.content_block b
        LEFT JOIN corpus.article_image i ON i.id = b.image_id
        WHERE b.article_id = %s AND b.block_type = 'image' AND i.id IS NULL
    """, (article_id,))
    check(cur.fetchone()[0] == 0, f"{label}: an image block resolves to no record")
    cur.execute("""
        SELECT count(*) FROM corpus.content_block b
        LEFT JOIN corpus.article_video v ON v.id = b.video_id
        WHERE b.article_id = %s AND b.block_type = 'video' AND v.id IS NULL
    """, (article_id,))
    check(cur.fetchone()[0] == 0, f"{label}: a video block resolves to no record")


def verify_one_current(cur) -> None:
    cur.execute("""
        SELECT article_id, count(*) FROM corpus.article_extraction
        WHERE is_current GROUP BY article_id HAVING count(*) <> 1
    """)
    check(not cur.fetchall(), "some article has other than exactly one current extraction")
    cur.execute("""
        SELECT count(*) FROM corpus.article a
        JOIN corpus.article_extraction e ON e.id = a.current_extraction_id
        WHERE NOT e.is_current
    """)
    check(cur.fetchone()[0] == 0,
          "article.current_extraction_id points at a non-current extraction")


def verify_search(cur) -> None:
    """Hungarian full-text search actually finds the right article.

    Not "does the index exist" - does a real query with real Hungarian
    inflection return the article a journalist meant.
    """
    cases = [
        # (query, a distinctive substring of the expected title)
        ("Zrínyi",   "Zrínyi"),          # stems zrínyire/zrínyit/zrínyinek
        ("zrinyi",   "Zrínyi"),          # unaccented, as typed on a foreign keyboard
        ("Szigetvár", "Zrínyi"),         # a tag, not in the title
    ]
    for query, expected in cases:
        cur.execute("""
            SELECT title FROM corpus.article
            WHERE search_tsv @@ websearch_to_tsquery('corpus.hungarian_ci', %s)
            ORDER BY ts_rank(search_tsv,
                     websearch_to_tsquery('corpus.hungarian_ci', %s)) DESC
            LIMIT 3
        """, (query, query))
        titles = [row[0] or "" for row in cur.fetchall()]
        check(any(expected in t for t in titles),
              f"article search for {query!r} did not surface {expected!r} "
              f"(got {[t[:40] for t in titles]})")

    # Body search must find a word that appears only in a paragraph, never in
    # the title or tags - proving the block index is what answers it.
    cur.execute("""
        SELECT a.title FROM corpus.article a
        WHERE EXISTS (SELECT 1 FROM corpus.content_block b
                      WHERE b.article_id = a.id
                        AND b.text_tsv @@ websearch_to_tsquery('corpus.hungarian_ci',
                                                               'kazamata'))
    """)
    check(cur.fetchall(), "body search for 'kazamata' found nothing")

    # Exact tag filter must not be satisfied by a body mention.
    cur.execute("SELECT count(*) FROM corpus.article WHERE tags @> ARRAY['Szigetvár']")
    check(cur.fetchone()[0] >= 1, "exact tag filter for 'Szigetvár' found nothing")


def verify_agent_queries(cur, article_id: int) -> None:
    """The MCP-shaped operations must each be one straightforward query."""
    # get_article
    cur.execute("SELECT url_hash, title, outlet FROM corpus.article WHERE id = %s",
                (article_id,))
    check(cur.fetchone() is not None, "get_article returned nothing")
    # get_article_content
    cur.execute("""
        SELECT block_index, block_type, xpath, block_text
        FROM corpus.content_block WHERE article_id = %s ORDER BY block_index
    """, (article_id,))
    check(cur.fetchall(), "get_article_content returned nothing")
    # get_article_images / videos / links
    for table in ("article_image", "article_video", "article_link"):
        cur.execute(f"SELECT count(*) FROM corpus.{table} WHERE article_id = %s",
                    (article_id,))
        cur.fetchone()
    # the readability.html path for the viewer, by kind
    cur.execute("""
        SELECT file_path, media_type FROM corpus.article_artifact
        WHERE article_id = %s AND kind = 'readability_html'
    """, (article_id,))
    check(cur.fetchone() is not None, "no readability_html artifact to open")


def verify_passage_reference(cur, directory: Path, article_id: int) -> str | None:
    """A citation, created from a real selector, and what happens to it.

    This is the whole point of the design: the reference must survive
    re-extraction, and a drifted xpath must be detected rather than silently
    resolving elsewhere.
    """
    cur.execute("""
        SELECT content_block_id, selector_xpath, quote_start, quote_end,
               quote_exact, quote_prefix, quote_suffix
        FROM corpus.article_link
        WHERE article_id = %s AND selector_xpath IS NOT NULL LIMIT 1
    """, (article_id,))
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute("""
        INSERT INTO corpus.passage_reference
            (article_id, content_block_id, selector_xpath, quote_start, quote_end,
             quote_exact, quote_prefix, quote_suffix)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
    """, (article_id, *row))
    reference_id = cur.fetchone()[0]

    # The CHECK constraint must reject a selector whose quote and offsets
    # disagree - a malformed citation must not be storable at all.
    #
    # Inside a SAVEPOINT: a failed statement poisons the whole transaction, and
    # a plain rollback here would discard the good insert above. (It did, the
    # first time this harness ran.)
    cur.execute("SAVEPOINT deliberate_violation")
    try:
        cur.execute("""
            INSERT INTO corpus.passage_reference
                (article_id, selector_xpath, quote_start, quote_end, quote_exact)
            VALUES (%s, '/html/body/article/div/p[1]', 0, 5, 'much longer than five')
        """, (article_id,))
        check(False, "a selector whose quote length disagrees with its offsets was accepted")
        cur.execute("RELEASE SAVEPOINT deliberate_violation")
    except psycopg2.errors.CheckViolation:
        check(True, "")
        cur.execute("ROLLBACK TO SAVEPOINT deliberate_violation")
    return reference_id


def verify_passage_view(cur, reference_id: str) -> None:
    """The view returns the canonical selector shape, ready for an API."""
    cur.execute("SELECT selector, url_hash FROM corpus.passage_selector WHERE id = %s",
                (reference_id,))
    row = cur.fetchone()
    check(row is not None, "passage_selector view returned nothing")
    if row is None:
        return
    selector, url_hash = row
    check(selector["type"] == "XPathSelector", "view: wrong selector type")
    check(selector["refinedBy"]["type"] == "TextPositionSelector",
          "view: wrong refinedBy type")
    check("exact" in selector["quote"], "view: quote.exact missing")
    check(isinstance(selector["refinedBy"]["start"], int), "view: start is not an int")
    check(len(url_hash) == 64, "view: url_hash is not a sha256")


# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------

def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(
        os.environ.get("CX_SAMPLE", "")).expanduser()
    directories = sorted(p.parent for p in root.rglob("content.json"))
    if not directories:
        print(f"no extracted articles under {root}", file=sys.stderr)
        return 1

    connection = psycopg2.connect(DSN)
    connection.autocommit = False
    print(f"== ingesting {len(directories)} article directories")

    ids: dict[Path, tuple[int, int]] = {}
    synthetic_skipped = 0
    with connection.cursor() as cur:
        for directory in directories:
            article_id, extraction_id, skipped = ingest(cur, directory)
            ids[directory] = (article_id, extraction_id)
            synthetic_skipped += skipped
        connection.commit()

    print("== verifying the contract holds through the database")
    with connection.cursor() as cur:
        for directory, (article_id, _) in ids.items():
            label = directory.name[:8]
            verify_counts(cur, directory, article_id)
            verify_invariant_a(cur, directory, article_id)
            verify_invariant_b(cur, directory, article_id)
            verify_invariants_cd(cur, article_id, label)
        verify_one_current(cur)
        verify_search(cur)
        connection.commit()

    # A citation on the article that has links, then a re-extraction over it.
    with_links = next((d for d, (aid, _) in ids.items()
                       if load(d, "links.json")), None)
    reference_id = None
    if with_links is not None:
        article_id, _ = ids[with_links]
        with connection.cursor() as cur:
            verify_agent_queries(cur, article_id)
            reference_id = verify_passage_reference(cur, with_links, article_id)
            connection.commit()
        if reference_id is not None:
            with connection.cursor() as cur:
                verify_passage_view(cur, reference_id)
                connection.commit()

    print("== re-extraction: ingesting every article a second time")
    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM corpus.article")
        articles_before = cur.fetchone()[0]
        for directory in directories:
            ingest(cur, directory)
        connection.commit()

    with connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM corpus.article")
        check(cur.fetchone()[0] == articles_before,
              "re-extraction created duplicate article rows")
        cur.execute("SELECT count(*) FROM corpus.article_extraction")
        extractions = cur.fetchone()[0]
        check(extractions == 2 * articles_before,
              f"expected two extractions per article, got {extractions}")
        verify_one_current(cur)

        # Superseded content is gone, current content is intact.
        cur.execute("""
            SELECT count(*) FROM corpus.content_block b
            JOIN corpus.article_extraction e ON e.id = b.extraction_id
            WHERE NOT e.is_current
        """)
        check(cur.fetchone()[0] == 0, "content from a superseded extraction survived")

        # The citation survived, and its quote still validates in pure SQL even
        # though the block row it pointed at was deleted and replaced.
        if reference_id is not None:
            cur.execute("""
                SELECT p.content_block_id, p.quote_exact,
                       (SELECT substring(b.block_text FROM p.quote_start + 1
                                         FOR p.quote_end - p.quote_start)
                        FROM corpus.content_block b
                        WHERE b.article_id = p.article_id
                          AND b.xpath = p.selector_xpath
                          AND b.extraction_id = (SELECT current_extraction_id
                                                 FROM corpus.article
                                                 WHERE id = p.article_id)) AS sliced
                FROM corpus.passage_reference p WHERE p.id = %s
            """, (reference_id,))
            row = cur.fetchone()
            check(row is not None, "the passage reference did not survive re-extraction")
            if row is not None:
                block_ref, exact, sliced = row
                check(block_ref is None,
                      "content_block_id should have been SET NULL when the block "
                      "it pointed at was replaced")
                check(sliced == exact,
                      "the citation no longer resolves against the current "
                      f"extraction: {sliced!r} != {exact!r}")

        # Re-verify every invariant against the second reading.
        for directory, (article_id, _) in ids.items():
            verify_invariant_a(cur, directory, article_id)
            verify_invariant_b(cur, directory, article_id)
            verify_invariants_cd(cur, article_id, directory.name[:8])
        connection.commit()

    with connection.cursor() as cur:
        cur.execute("""
            SELECT
              (SELECT count(*) FROM corpus.article),
              (SELECT count(*) FROM corpus.article_extraction),
              (SELECT count(*) FROM corpus.content_block),
              (SELECT count(*) FROM corpus.article_image),
              (SELECT count(*) FROM corpus.article_video),
              (SELECT count(*) FROM corpus.article_link),
              (SELECT count(*) FROM corpus.article_artifact),
              (SELECT count(*) FROM corpus.passage_reference)
        """)
        counts = cur.fetchone()
    connection.close()

    print("\n== rows after two ingestion passes")
    for name, value in zip(("article", "article_extraction", "content_block",
                            "article_image", "article_video", "article_link",
                            "article_artifact", "passage_reference"), counts):
        print("   %-20s %d" % (name, value))

    print("\n   %-20s %d %s"
          % ("links skipped", synthetic_skipped,
             "(none - links.py filters them at the source)" if not synthetic_skipped
             else "(the extractor's own embed fallback anchors: PRE-FIX output)"))
    print(f"\n{checks} checks run")
    if failures:
        print(f"\n{len(failures)} FAILURES:")
        for message in failures[:40]:
            print("  -", message)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
