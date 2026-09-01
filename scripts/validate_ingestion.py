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

import os
import re
import unicodedata
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from causalia_extractor.dom import reparse                     # noqa: E402
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


# Ingestion lives in cx_ingest so this harness and the development-database
# loader cannot drift apart on the SQL.
from cx_ingest import ingest, load, seed_crawler_rows        # noqa: E402,F401




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

    The cases are DERIVED from whatever was ingested. They used to be hardcoded
    to one article in the 13-capture sample (Zrinyi/Szigetvar/kazamata), which
    made this function report failures on any other sample - a property of the
    fixture masquerading as a property of the schema.
    """
    config = "corpus.hungarian_ci"

    def fold(text: str) -> str:
        return "".join(c for c in unicodedata.normalize("NFD", (text or "").lower())
                       if not unicodedata.combining(c))

    def hits(query: str) -> set[int]:
        cur.execute(f"""
            SELECT a.id FROM corpus.article a
            WHERE a.search_tsv @@ websearch_to_tsquery('{config}', %(q)s)
               OR EXISTS (SELECT 1 FROM corpus.content_block b
                           WHERE b.article_id = a.id
                             AND b.text_tsv @@ websearch_to_tsquery('{config}', %(q)s))
        """, {"q": query})
        return {row[0] for row in cur.fetchall()}

    # 1. An accented word from a real title must be findable, and must be
    #    findable typed without accents - the whole reason hungarian_ci exists.
    cur.execute("SELECT id, title FROM corpus.article WHERE title IS NOT NULL")
    titles = cur.fetchall()
    probe = None
    for article_id, title in titles:
        for word in re.findall(r"[A-Za-zÁÉÍÓÖŐÚÜŰáéíóöőúüű]{6,}", title):
            if fold(word) != word.lower():          # i.e. it carries an accent
                probe = (article_id, word)
                break
        if probe:
            break
    if probe is None:
        check(True, "")                             # no accented title word to probe
    else:
        article_id, word = probe
        accented, plain = hits(word), hits(fold(word))
        check(article_id in accented,
              f"title search for {word!r} did not find its own article")
        check(accented == plain,
              f"{word!r} and its unaccented form {fold(word)!r} return different "
              f"articles ({len(accented)} vs {len(plain)}) - unaccent is not working")

    # 2. A word that is in the prose and in no metadata anywhere: only the block
    #    index can answer it, which is what proves there is no third vector needed.
    cur.execute("""SELECT b.article_id, b.block_text FROM corpus.content_block b
                   WHERE b.block_text IS NOT NULL AND length(b.block_text) > 80""")
    blocks = cur.fetchall()
    cur.execute("""SELECT string_agg(coalesce(title,'') || ' ' || coalesce(subtitle,'')
                          || ' ' || coalesce(description,'') || ' '
                          || corpus.text_array_to_string(tags), ' ')
                   FROM corpus.article""")
    all_metadata = fold(cur.fetchone()[0] or "")
    for article_id, text in blocks:
        found = next((w for w in re.findall(r"[A-Za-zÁÉÍÓÖŐÚÜŰáéíóöőúüű]{9,}", text)
                      if fold(w) not in all_metadata), None)
        if found:
            check(article_id in hits(found),
                  f"body search for the prose-only word {found!r} found nothing")
            break

    # 3. Exact tag filtering must be satisfied by a tag and not by prose.
    cur.execute("""SELECT unnest(tags) AS tag, count(*) FROM corpus.article
                   GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1""")
    row = cur.fetchone()
    if row is not None:
        tag, expected = row
        cur.execute("SELECT count(*) FROM corpus.article WHERE tags @> ARRAY[%s]::text[]",
                    (tag,))
        check(cur.fetchone()[0] == expected,
              f"exact tag filter for {tag!r} did not return {expected} article(s)")


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
