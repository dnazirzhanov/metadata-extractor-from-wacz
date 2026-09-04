#!/usr/bin/env python3
"""Validate the DEVELOPMENT database against the extractor output that fed it.

Reuses every assertion scripts/validate_ingestion.py already makes - block
contiguity, block text against readability.html, selector/quote agreement in SQL
and in the document, media resolution, one-current-extraction - and adds what
that harness does not cover:

    schema        the catalogs, not the migration files: tables, indexes,
                  hungarian_ci, generated columns, FKs and CHECKs
    identity      url_hash recomputed from source_url
    artifacts     paths relative, and the files actually there
    search        Hungarian FTS, derived from what was ingested rather than
                  hardcoded against a retired sample
    citation      xpath + offsets + quote.exact + prefix + suffix, resolved in
                  the real readability.html
    failures      what the schema must reject, and - stated separately - what it
                  cannot possibly reject
    re-ingestion  extraction history, run automatically once a second pass has
                  happened

Adds no DDL and changes no migration. Read-only over the extractor output.

Usage:  scripts/dev_validate.py <output-dir> [--dsn DSN] [--section NAME]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import search as search_layer                                   # noqa: E402
import validate_ingestion as vi                                 # noqa: E402
from cx_ingest import load                                      # noqa: E402
from causalia_extractor.dom import reparse                      # noqa: E402
from causalia_extractor.identity import archive_id_for          # noqa: E402
from causalia_extractor.normalize import normalize_text         # noqa: E402
from causalia_extractor.xpath import resolve                    # noqa: E402

DEFAULT_DSN = ("host=127.0.0.1 port=55433 user=causalia password=dev "
               "dbname=causalia_dev")

# One counter for everything, shared with the reused harness assertions.
check = vi.check

notes: list[str] = []


def note(text: str) -> None:
    notes.append(text)
    print(f"   NOTE  {text}")


def head(title: str) -> None:
    print(f"\n== {title}")


def fold(text: str) -> str:
    """Accent-folded lowercase, for deciding whether a word occurs in a string.

    Mirrors what unaccent + hungarian_ci do to a token, so a Python 'is this
    word in the title' answer agrees with what the tsvector would say.
    """
    return "".join(c for c in unicodedata.normalize("NFD", (text or "").lower())
                   if not unicodedata.combining(c))


# ---------------------------------------------------------------------
# 1. Schema: read the catalogs, not the files
# ---------------------------------------------------------------------

EXPECTED_TABLES = {
    "article", "article_extraction", "content_block", "article_image",
    "article_video", "article_link", "article_artifact", "passage_reference",
    "schema_migrations",
}


def _starts_word(haystack: str, term: str) -> bool:
    """True when `term` begins a word in `haystack`. Both already accent-folded."""
    if not term:
        return False
    at = haystack.find(term)
    while at != -1:
        if at == 0 or not (haystack[at - 1].isalnum() or haystack[at - 1] == "_"):
            return True
        at = haystack.find(term, at + 1)
    return False


def verify_schema(cur, repo: Path) -> None:
    head("SCHEMA")
    cur.execute("SELECT count(*) FROM information_schema.schemata WHERE schema_name='corpus'")
    check(cur.fetchone()[0] == 1, "schema 'corpus' does not exist")

    cur.execute("""SELECT table_name FROM information_schema.tables
                   WHERE table_schema='corpus' AND table_type='BASE TABLE'""")
    tables = {row[0] for row in cur.fetchall()}
    missing = EXPECTED_TABLES - tables
    extra = tables - EXPECTED_TABLES
    check(not missing, f"corpus tables missing: {sorted(missing)}")
    check(not extra, f"unexpected corpus tables: {sorted(extra)}")
    print(f"   tables            {len(tables)}  ({', '.join(sorted(tables))})")

    # Indexes: every name the migrations create must exist.
    declared = set()
    for path in sorted(repo.glob("migrations/*.sql")):
        declared |= set(re.findall(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS\s+(\w+)",
                                   path.read_text(encoding="utf-8")))
    cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname='corpus'")
    present = {row[0] for row in cur.fetchall()}
    check(declared <= present,
          f"indexes declared but absent: {sorted(declared - present)}")
    print(f"   indexes           {len(present)} present, {len(declared)} named in migrations "
          f"(+{len(present) - len(declared)} PK/UNIQUE constraint indexes)")

    cur.execute("""SELECT count(*) FROM pg_ts_config c
                   JOIN pg_namespace n ON n.oid = c.cfgnamespace
                   WHERE n.nspname='corpus' AND c.cfgname='hungarian_ci'""")
    check(cur.fetchone()[0] == 1, "text search configuration corpus.hungarian_ci missing")
    cur.execute("SELECT count(*) FROM pg_extension WHERE extname='unaccent'")
    check(cur.fetchone()[0] == 1, "extension unaccent not installed")

    cur.execute("""SELECT provolatile FROM pg_proc p JOIN pg_namespace n
                   ON n.oid = p.pronamespace
                   WHERE n.nspname='corpus' AND proname='text_array_to_string'""")
    row = cur.fetchone()
    check(row is not None and row[0] == "i",
          "corpus.text_array_to_string is not IMMUTABLE (a STORED generated "
          "column cannot use it)")

    # The generated tsvector columns must be STORED and must actually populate.
    for table, column in (("article", "search_tsv"), ("content_block", "text_tsv")):
        cur.execute("""SELECT attgenerated FROM pg_attribute a
                       JOIN pg_class c ON c.oid = a.attrelid
                       JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE n.nspname='corpus' AND c.relname=%s AND attname=%s""",
                    (table, column))
        row = cur.fetchone()
        check(row is not None and row[0] == "s",
              f"corpus.{table}.{column} is not a STORED generated column")
        cur.execute(f"SELECT count(*) FROM corpus.{table} "
                    f"WHERE {column} IS NULL OR {column} = ''::tsvector")
        empty = cur.fetchone()[0]
        cur.execute(f"SELECT count(*) FROM corpus.{table}")
        total = cur.fetchone()[0]
        check(total == 0 or empty < total,
              f"every {table}.{column} is empty - the generated column is not populating")
        print(f"   {table}.{column:<12} {total - empty}/{total} rows carry a vector")

    # Stemming and accent-folding, the two properties the config exists for.
    cur.execute("""SELECT to_tsvector('corpus.hungarian_ci','Magyarországra')
                        @@ websearch_to_tsquery('corpus.hungarian_ci','Magyarország'),
                          to_tsvector('corpus.hungarian_ci','Magyarországra')
                        @@ websearch_to_tsquery('corpus.hungarian_ci','magyarorszag'),
                          to_tsvector('corpus.hungarian_ci','Zrínyire a japánok is felnéznek')
                        @@ websearch_to_tsquery('corpus.hungarian_ci','zrinyi')""")
    stems, unaccents, both = cur.fetchone()
    check(stems, "hungarian_ci does not stem Magyarországra -> Magyarország")
    check(unaccents, "hungarian_ci does not fold accents (magyarorszag)")
    check(both, "hungarian_ci fails the stem+unaccent case (zrinyi -> Zrínyire)")

    # Constraints. Names come from the migrations; presence from the catalog.
    cur.execute("""SELECT conname, contype, confdeltype FROM pg_constraint c
                   JOIN pg_namespace n ON n.oid = c.connamespace
                   WHERE n.nspname='corpus'""")
    constraints = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
    for name in ("passage_reference_range", "passage_reference_quote_length",
                 "article_link_selector_complete"):
        check(name in constraints, f"CHECK constraint {name} is missing")
    checks_n = sum(1 for t, _ in constraints.values() if t == "c")
    fks = sum(1 for t, _ in constraints.values() if t == "f")
    print(f"   constraints       {checks_n} CHECK, {fks} FOREIGN KEY")

    # The one outbound FK, and its delete action, which is the whole B.1.1 argument.
    cur.execute("""SELECT confdeltype FROM pg_constraint c
                   JOIN pg_class t ON t.oid = c.conrelid
                   JOIN pg_namespace n ON n.oid = t.relnamespace
                   JOIN pg_class r ON r.oid = c.confrelid
                   WHERE n.nspname='corpus' AND t.relname='article'
                     AND r.relname='urls' AND c.contype='f'""")
    row = cur.fetchone()
    check(row is not None, "corpus.article has no FK to urls")
    check(row is not None and row[0] == "r",
          f"article.url_hash -> urls is not ON DELETE RESTRICT (got {row and row[0]!r})")


# ---------------------------------------------------------------------
# 2. Ingestion fidelity
# ---------------------------------------------------------------------

def article_id_for(cur, directory: Path) -> tuple[int, dict]:
    article = load(directory, "article.json")
    cur.execute("SELECT id FROM corpus.article WHERE url_hash = %s",
                (article["archive_id"],))
    row = cur.fetchone()
    return (row[0] if row else None), article


def verify_identity(cur, directory: Path, article_id: int, article: dict) -> None:
    """The stable URL identity, recomputed rather than trusted."""
    cur.execute("""SELECT url_hash, source_url, canonical_url, outlet
                   FROM corpus.article WHERE id = %s""", (article_id,))
    url_hash, source_url, canonical_url, outlet = cur.fetchone()
    check(url_hash == archive_id_for(article["source_url"]),
          f"{directory.name[:8]}: url_hash is not sha256(normalize_url(source_url))")
    check(len(url_hash) == 64, f"{directory.name[:8]}: url_hash is not a sha256")
    check(source_url == article["source_url"],
          f"{directory.name[:8]}: source_url was not preserved verbatim")
    check(canonical_url == article["canonical_url"],
          f"{directory.name[:8]}: canonical_url was not preserved verbatim")
    check(outlet == article["outlet"], f"{directory.name[:8]}: outlet mismatch")
    # The FK must actually resolve - the point of ON DELETE RESTRICT.
    cur.execute("SELECT count(*) FROM urls WHERE url_hash = %s", (url_hash,))
    check(cur.fetchone()[0] == 1,
          f"{directory.name[:8]}: no urls row for this article's url_hash")


def verify_extraction_metadata(cur, directory: Path, article_id: int) -> None:
    extraction = load(directory, "extraction.json")
    cur.execute("""SELECT e.extractor_version, e.extraction_status, e.extracted_at,
                          e.is_current, e.id = a.current_extraction_id
                   FROM corpus.article_extraction e
                   JOIN corpus.article a ON a.id = e.article_id
                   WHERE e.article_id = %s AND e.is_current""", (article_id,))
    row = cur.fetchone()
    check(row is not None, f"{directory.name[:8]}: no current extraction")
    if row is None:
        return
    version, status, extracted_at, is_current, is_pointed_at = row
    check(version == extraction["extraction_version"],
          f"{directory.name[:8]}: extractor_version != extraction.json")
    check(status == extraction["extraction_status"],
          f"{directory.name[:8]}: extraction_status != extraction.json")
    check(extracted_at.isoformat().startswith(extraction["extracted_at"][:19]),
          f"{directory.name[:8]}: extracted_at != extraction.json "
          f"({extracted_at.isoformat()} vs {extraction['extracted_at']})")
    check(is_current and is_pointed_at,
          f"{directory.name[:8]}: current extraction is not the one article points at")

    # Every block belongs to an extraction, and to the CURRENT one.
    cur.execute("""SELECT count(*) FROM corpus.content_block b
                   JOIN corpus.article a ON a.id = b.article_id
                   WHERE b.article_id = %s
                     AND (b.extraction_id IS NULL
                          OR b.extraction_id <> a.current_extraction_id)""",
                (article_id,))
    check(cur.fetchone()[0] == 0,
          f"{directory.name[:8]}: a content block belongs to no/another extraction")


def verify_artifacts(cur, directory: Path, article_id: int, root: Path) -> None:
    """Paths relative to the storage root, and the files really there."""
    cur.execute("""SELECT kind, file_path, media_type, byte_size
                   FROM corpus.article_artifact WHERE article_id = %s ORDER BY kind""",
                (article_id,))
    rows = cur.fetchall()
    check(rows, f"{directory.name[:8]}: no artifacts at all")
    kinds = {row[0] for row in rows}
    check("readability_html" in kinds,
          f"{directory.name[:8]}: no readability_html artifact - nothing to cite into")
    for kind, file_path, media_type, byte_size in rows:
        check(not file_path.startswith("/"),
              f"{directory.name[:8]}/{kind}: file_path is absolute ({file_path[:60]})")
        check(".." not in Path(file_path).parts,
              f"{directory.name[:8]}/{kind}: file_path escapes the storage root")
        resolved = root / file_path
        check(resolved.is_file(),
              f"{directory.name[:8]}/{kind}: stored path does not exist: {file_path}")
        if resolved.is_file() and byte_size is not None:
            check(resolved.stat().st_size == byte_size,
                  f"{directory.name[:8]}/{kind}: byte_size disagrees with the file")
        check(bool(media_type), f"{directory.name[:8]}/{kind}: no media_type")

    # Media files, where the record claims one.
    for table, flag in (("article_image", "is_available"), ("article_video", "is_archived")):
        cur.execute(f"""SELECT local_ref, file_path, {flag} FROM corpus.{table}
                        WHERE article_id = %s AND file_path IS NOT NULL""",
                    (article_id,))
        for local_ref, file_path, claimed in cur.fetchall():
            check(not file_path.startswith("/"),
                  f"{directory.name[:8]}/{local_ref}: file_path is absolute")
            if claimed:
                check((root / file_path).is_file(),
                      f"{directory.name[:8]}/{local_ref}: {table} claims a file that "
                      f"is not on disk: {file_path}")


def verify_ingestion(cur, root: Path) -> list[tuple[Path, int]]:
    head("INGESTION FIDELITY")
    directories = sorted(p.parent for p in root.rglob("content.json"))
    pairs = []
    for directory in directories:
        article_id, article = article_id_for(cur, directory)
        check(article_id is not None, f"{directory.name[:8]}: not in the database")
        if article_id is None:
            continue
        pairs.append((directory, article_id))
        # Reused, unchanged, from the validated harness.
        vi.verify_counts(cur, directory, article_id)
        vi.verify_invariant_a(cur, directory, article_id)
        vi.verify_invariant_b(cur, directory, article_id)
        vi.verify_invariants_cd(cur, article_id, directory.name[:8])
        # New here.
        verify_identity(cur, directory, article_id, article)
        verify_extraction_metadata(cur, directory, article_id)
        verify_artifacts(cur, directory, article_id, root)
    vi.verify_one_current(cur)
    print(f"   {len(pairs)} articles verified against their own output files")

    cur.execute("""SELECT count(*) FROM corpus.article
                   WHERE canonical_url IS NOT NULL AND canonical_url <> source_url""")
    differ = cur.fetchone()[0]
    print(f"   source_url != canonical_url on {differ} article(s) - both stored, "
          f"neither derived from the other")
    return pairs


# ---------------------------------------------------------------------
# 3. Search, with the cases derived from what was ingested
# ---------------------------------------------------------------------

def corpus_text(cur) -> dict[int, dict]:
    """Everything searchable, per article, straight from the database."""
    cur.execute("""SELECT a.id, a.title, a.subtitle, a.description, a.tags,
                          string_agg(coalesce(b.block_text,''), ' ') AS body
                   FROM corpus.article a
                   LEFT JOIN corpus.content_block b ON b.article_id = a.id
                   GROUP BY a.id""")
    out = {}
    for row in cur.fetchall():
        out[row[0]] = {"title": row[1] or "", "subtitle": row[2] or "",
                       "description": row[3] or "", "tags": row[4] or [],
                       "body": row[5] or ""}
    return out


def pick_paragraph_only_term(corpus: dict[int, dict]) -> tuple[str, int] | None:
    """A word in the prose of exactly one article and in no metadata anywhere."""
    for article_id, doc in corpus.items():
        meta = fold(" ".join([doc["title"], doc["subtitle"], doc["description"],
                              " ".join(doc["tags"])]))
        for word in re.findall(r"[A-Za-zÁÉÍÓÖŐÚÜŰáéíóöőúüű]{8,}", doc["body"]):
            folded = fold(word)
            if folded in meta:
                continue
            # and in no other article's metadata either
            if any(folded in fold(" ".join([d["title"], d["subtitle"], d["description"],
                                            " ".join(d["tags"])]))
                   for i, d in corpus.items() if i != article_id):
                continue
            return word, article_id
    return None


def pick_tag_also_in_prose(corpus: dict[int, dict]) -> tuple[str, int, int] | None:
    """A tag on some articles that also appears in the prose of others.

    That is the case where an exact tag filter and a full-text search must give
    DIFFERENT answers - which is why both exist (docs/postgres-schema.md D.3).
    """
    tagged: dict[str, set[int]] = {}
    for article_id, doc in corpus.items():
        for tag in doc["tags"]:
            tagged.setdefault(tag, set()).add(article_id)
    for tag, owners in sorted(tagged.items(), key=lambda kv: -len(kv[1])):
        mentions = {i for i, d in corpus.items()
                    if fold(tag) in fold(d["body"]) and i not in owners}
        if mentions:
            return tag, len(owners), len(owners) + len(mentions)
    return None


def verify_search(cur, root: Path) -> dict:
    head("SEARCH (Hungarian, cases derived from the ingested corpus)")
    corpus = corpus_text(cur)
    report: dict = {"present": {}, "absent": {}, "latency_ms": {}}

    def run(query: str, **kwargs) -> list[dict]:
        """The RANKED list a user would see. Truncated, and only ever used for
        display, latency and 'did anything come back'."""
        started = time.monotonic()
        rows = search_layer.search_articles(cur, query, limit=25, **kwargs)
        report["latency_ms"][query] = (time.monotonic() - started) * 1000
        return rows

    def all_ids(query: str, **kwargs) -> set[int]:
        """The COMPLETE match set, for every assertion that compares two
        queries to each other.

        A set comparison against a rank-truncated list measures the LIMIT, not
        the search. With 36 articles the limit above never bound and the two
        were interchangeable; at 1,008 every broad query saturates it, and this
        harness reported four failures - accent folding "broken", two stemming
        subsets "violated", tag-filter and full-text "indistinguishable" - all
        of which were 25 == 25 and none of which were real.
        """
        return search_layer.matching_ids(cur, query, **kwargs)

    requested = ["Orbán Viktor", "Donald Trump", "Magyarország", "orosz-ukrán háború"]
    for query in requested:
        rows = run(query)
        # Is the term genuinely in the corpus? Decided from the DATA, so a zero
        # result is attributed to the sample and never to a broken query.
        needles = [fold(w) for w in query.replace("-", " ").split()]
        # Word-INITIAL, not "anywhere in the string". -ban/-ben is the inessive
        # case ending, so a bare substring test finds "Orban" inside elsosorban,
        # musorban, szektorban and taborban, and then blames the search engine
        # for not returning them.
        in_corpus = sum(
            1 for doc in corpus.values()
            if all(_starts_word(fold(" ".join([doc["title"], doc["subtitle"],
                                               doc["description"], doc["body"],
                                               " ".join(doc["tags"])])), n)
                   for n in needles))
        found = len(all_ids(query))
        print(f"   {query!r:<24} {found:>3} hit(s)   "
              f"({in_corpus} article(s) start a word with every term)")
        if in_corpus > found > 0:
            note(f"{query!r}: {in_corpus} article(s) contain the term but only "
                 f"{found} match - a word is not a lexeme; see the stemmer "
                 f"diagnostics")
        if in_corpus:
            check(len(rows) > 0,
                  f"search for {query!r} found nothing although {in_corpus} "
                  f"article(s) contain every term")
            report["present"][query] = len(rows)
        else:
            check(len(rows) == 0,
                  f"search for {query!r} returned {len(rows)} hit(s) although no "
                  f"article contains it")
            report["absent"][query] = "absent from the sample, 0 rows, no error"
            note(f"{query!r} does not occur anywhere in this sample - the zero "
                 f"result is the corpus, not the query")

    # Accent folding: the accented and unaccented spellings must agree exactly.
    head("SEARCH - accents and stemming")
    for accented, plain in (("Magyarország", "magyarorszag"), ("Orbán", "Orban")):
        run(accented), run(plain)          # latency + display only
        a, b = all_ids(accented), all_ids(plain)
        print(f"   {accented!r:<16} {len(a):>3} hit(s)   {plain!r:<16} {len(b):>3} hit(s)")
        if a:
            check(a == b, f"{accented!r} and {plain!r} return different articles "
                          f"({len(a)} vs {len(b)}) - unaccent is not doing its job")
            report["present"][plain] = len(b)

    # Stemming: an inflected form must find what the base form finds.
    report["stemming"] = {}
    for inflected, base in (("Magyarországra", "Magyarország"),
                            ("Orbánnak", "Orbán"), ("Orbánt", "Orbán")):
        run(inflected), run(base)          # latency + display only
        a, b = all_ids(inflected), all_ids(base)
        print(f"   {inflected!r:<16} {len(a):>3} hit(s)   vs base {base!r} {len(b):>3}")
        if not b:
            continue
        if a:
            check(a <= b, f"stemming: {inflected!r} -> {len(a)} hits is not a "
                          f"subset of {base!r} -> {len(b)}")
            report["stemming"][inflected] = "reaches the base form"
        else:
            # Not our defect and not a schema problem: the snowball hungarian
            # stemmer simply does not reduce this suffix. Recorded as a finding.
            report["stemming"][inflected] = "does NOT reach the base form"
            note(f"the hungarian stemmer does not reduce {inflected!r} to "
                 f"{base!r}'s lexeme - searching the base form misses this "
                 f"inflection")

    # A term in the prose and in no metadata: the block semi-join is what finds it.
    head("SEARCH - prose-only and tag-only terms")
    picked = pick_paragraph_only_term(corpus)
    if picked is None:
        note("no word occurs in prose and in no metadata anywhere - "
             "the paragraph-only case cannot be tested on this sample")
    else:
        word, expected_id = picked
        rows = run(word)
        ids = {r["id"] for r in rows}
        print(f"   prose-only {word!r}: {len(rows)} hit(s), match_reason="
              f"{ {r['match_reason'] for r in rows} }")
        check(expected_id in ids,
              f"prose-only term {word!r} did not find the article containing it")
        hit = next((r for r in rows if r["id"] == expected_id), None)
        check(hit is not None and hit["match_reason"] == "body",
              f"prose-only term {word!r} matched as "
              f"{hit and hit['match_reason']!r}, expected 'body'")
        check(bool(hit and hit["blocks"]),
              f"prose-only term {word!r} returned no matching block to cite")
        report["prose_only"] = {"term": word, "hits": len(rows)}

    # Exact tag filtering, kept separate from full-text.
    picked = pick_tag_also_in_prose(corpus)
    if picked is None:
        note("no tag also appears in another article's prose - the "
             "filter-vs-full-text distinction cannot be shown on this sample")
    else:
        tag, tagged_count, fts_count = picked
        filtered = search_layer.filter_by_tag(cur, tag, limit=100000)
        run(tag)                           # latency + display only
        fts = all_ids(tag)
        print(f"   tag {tag!r}: exact filter {len(filtered)}, full-text {len(fts)} "
              f"(expected {tagged_count} / {fts_count})")
        check(len(filtered) == tagged_count,
              f"exact tag filter for {tag!r} returned {len(filtered)}, "
              f"expected {tagged_count}")
        check(len(fts) > len(filtered),
              f"full-text for {tag!r} returned {len(fts)}, not more than the "
              f"exact filter's {len(filtered)} - the two are not distinguishable")
        for row in filtered:
            check(tag in row["tags"], f"tag filter returned an article without {tag!r}")
        report["tag_filter"] = {"tag": tag, "exact": len(filtered), "fts": len(fts)}

    # Ranking: a title/tag hit must outrank a body-only hit.
    head("SEARCH - ranking")
    rows = run("Orbán Viktor")
    if len(rows) >= 2:
        meta_scores = [r["score"] for r in rows if r["meta_match"]]
        body_scores = [r["score"] for r in rows if not r["meta_match"]]
        if meta_scores and body_scores:
            check(min(meta_scores) > max(body_scores),
                  "a body-only hit outranks a metadata hit - setweight is not "
                  "having the intended effect")
            print(f"   metadata hits score {min(meta_scores):.4f}-{max(meta_scores):.4f}, "
                  f"body-only {min(body_scores):.4f}-{max(body_scores):.4f}")
        report["ranking"] = [(r["outlet"], round(r["score"], 4), r["match_reason"])
                             for r in rows[:5]]

    # What the stemmer ACTUALLY does to the words this corpus contains. Not an
    # assertion - a measurement, because over- and under-stemming are properties
    # of snowball's hungarian dictionary, not of this schema.
    head("SEARCH - stemmer diagnostics (measured, not asserted)")
    probes = ["Orbán", "Orbánnak", "Orbánt", "Orbánnal", "orra", "ori",
              "Magyarország", "Magyarországra", "Magyarországról",
              "magyarországi", "ország", "Viktor", "Viktorral", "háború",
              "háborúban", "orosz", "oroszok"]
    lexemes: dict[str, str] = {}
    for word in probes:
        cur.execute("SELECT to_tsvector('corpus.hungarian_ci', %s)::text", (word,))
        raw = cur.fetchone()[0]
        lexemes[word] = raw.split("'")[1] if "'" in raw else ""
        print(f"   {word:<16} -> {lexemes[word]!r}")
    report["lexemes"] = lexemes

    collisions: dict[str, list[str]] = {}
    for word, lexeme in lexemes.items():
        collisions.setdefault(lexeme, []).append(word)
    for lexeme, words in sorted(collisions.items()):
        if len(words) > 1:
            print(f"   collision: {lexeme!r} <- {words}")
    report["collisions"] = {k: v for k, v in collisions.items() if len(v) > 1}

    # Block-level search returns the citable unit directly.
    blocks = search_layer.search_article_content(cur, "Magyarország", limit=5)
    check(bool(blocks), "block-level search returned nothing for 'Magyarország'")
    for block in blocks:
        check(bool(block["xpath"]), "a block search result carries no xpath")
    print(f"   block-level search: {len(blocks)} block(s), each with an xpath")
    return report


# ---------------------------------------------------------------------
# 4. The citation workflow, end to end
# ---------------------------------------------------------------------

def verify_citation(cur, root: Path, pairs: list[tuple[Path, int]]) -> dict:
    head("CITATION")
    # A paragraph block long enough to have a prefix and a suffix around the quote.
    chosen = None
    for directory, article_id in pairs:
        cur.execute("""SELECT id, block_index, xpath, block_text
                       FROM corpus.content_block
                       WHERE article_id = %s AND block_type = 'paragraph'
                         AND length(block_text) > 160
                       ORDER BY block_index LIMIT 1""", (article_id,))
        row = cur.fetchone()
        if row:
            chosen = (directory, article_id, row)
            break
    check(chosen is not None, "no paragraph block long enough to cite")
    if chosen is None:
        return {}
    directory, article_id, (block_id, block_index, xpath, block_text) = chosen

    # Quote a real sentence fragment out of the middle of the block, so prefix
    # and suffix are both non-empty - exactly the shape a highlighter needs.
    start = 60
    end = start + 40
    quote_exact = block_text[start:end]
    prefix = block_text[max(0, start - 32):start]
    suffix = block_text[end:end + 32]

    cur.execute("""INSERT INTO corpus.passage_reference
                     (article_id, content_block_id, selector_xpath, quote_start,
                      quote_end, quote_exact, quote_prefix, quote_suffix)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (article_id, block_id, xpath, start, end, quote_exact, prefix, suffix))
    reference_id = cur.fetchone()[0]
    print(f"   minted passage_reference {reference_id}")
    print(f"   xpath  {xpath}")
    print(f"   quote  {quote_exact!r}")

    # (a) The SQL-only half: no DOM needed.
    cur.execute("""SELECT substring(b.block_text FROM p.quote_start + 1
                                    FOR p.quote_end - p.quote_start) = p.quote_exact
                   FROM corpus.passage_reference p
                   JOIN corpus.content_block b ON b.id = p.content_block_id
                   WHERE p.id = %s""", (reference_id,))
    check(cur.fetchone()[0] is True, "SQL slice of block_text != quote_exact")

    # (b) The document half, exactly as a frontend would do it.
    tree = reparse((directory / "readability.html").read_text(encoding="utf-8"))
    element = resolve(tree, xpath)
    check(element is not None, f"citation xpath does not resolve: {xpath}")
    document_text = normalize_text(element) if element is not None else ""
    check(document_text[start:end] == quote_exact,
          "document slice at the stored offsets != quote_exact")
    check(document_text[max(0, start - len(prefix)):start] == prefix,
          "quote_prefix does not sit immediately before the quote in the document")
    check(document_text[end:end + len(suffix)] == suffix,
          "quote_suffix does not sit immediately after the quote in the document")
    print(f"   xpath + offsets + quote.exact + prefix + suffix all verified in "
          f"readability.html")

    # (c) prefix+exact+suffix must locate the passage on its own - the repair path
    #     when an xpath drifts.
    anchor = prefix + quote_exact + suffix
    check(document_text.count(anchor) == 1,
          "prefix+exact+suffix is not unique in the block - self-repair could "
          "re-find the wrong occurrence")
    check(document_text.index(anchor) + len(prefix) == start,
          "re-finding by prefix+exact+suffix does not reproduce quote_start")

    # (d) The view hands an API the canonical selector shape.
    vi.verify_passage_view(cur, reference_id)
    cur.execute("SELECT selector FROM corpus.passage_selector WHERE id = %s",
                (reference_id,))
    selector = cur.fetchone()[0]
    check(selector["value"] == xpath, "view: xpath differs from the column")
    check(selector["quote"]["prefix"] == prefix, "view: prefix missing or wrong")
    check(selector["quote"]["suffix"] == suffix, "view: suffix missing or wrong")
    check("block_id" not in str(selector),
          "the selector shape has acquired a block_id")

    cur.execute("SELECT resolution_status FROM corpus.passage_reference WHERE id = %s",
                (reference_id,))
    check(cur.fetchone()[0] == "unverified",
          "a fresh passage_reference is not 'unverified' - status must never be assumed")
    return {"reference_id": str(reference_id), "article_id": article_id,
            "xpath": xpath, "quote": quote_exact, "block_id": block_id,
            "directory": str(directory)}


# ---------------------------------------------------------------------
# 5. Failure cases: what the schema rejects, and what it cannot
# ---------------------------------------------------------------------

def expect_rejected(cur, label: str, sql: str, params: tuple = ()) -> None:
    """The database must refuse this. Attempted inside a SAVEPOINT."""
    cur.execute("SAVEPOINT deliberate")
    try:
        cur.execute(sql, params)
    except (psycopg2.errors.CheckViolation, psycopg2.errors.UniqueViolation,
            psycopg2.errors.ForeignKeyViolation, psycopg2.errors.NotNullViolation,
            psycopg2.errors.ExclusionViolation) as exc:
        cur.execute("ROLLBACK TO SAVEPOINT deliberate")
        check(True, "")
        print(f"   rejected   {label:<52} {type(exc).__name__}")
        return
    cur.execute("ROLLBACK TO SAVEPOINT deliberate")
    check(False, f"the database ACCEPTED invalid data: {label}")
    print(f"   ACCEPTED   {label:<52} <- should have been rejected")


def verify_failures(cur, root: Path, citation: dict) -> None:
    head("FAILURE CASES - the database must reject these")
    article_id = citation.get("article_id")
    block_id = citation.get("block_id")
    xpath = citation.get("xpath", "/html/body/article/div/p[1]")
    if article_id is None:
        note("no citation was minted; failure cases skipped")
        return
    if block_id is None:
        # Expected after a re-extraction: content_block_id was SET NULL when the
        # block it pointed at was replaced. The quote is the anchor, so the block
        # is re-found by xpath in the CURRENT extraction - which is the repair
        # path itself, not a workaround for it.
        cur.execute("""SELECT b.id FROM corpus.content_block b
                       JOIN corpus.article a ON a.id = b.article_id
                       WHERE b.article_id = %s AND b.xpath = %s
                         AND b.extraction_id = a.current_extraction_id""",
                    (article_id, xpath))
        row = cur.fetchone()
        block_id = row[0] if row else None
        if block_id is None:
            note("the citation's block cannot be re-found by xpath; the "
                 "cannot-enforce cases need a live block and were skipped")
        else:
            print(f"   citation's content_block_id is NULL after re-extraction; "
                  f"re-resolved by xpath to block {block_id}")

    expect_rejected(cur, "quote_exact length disagrees with the offsets", """
        INSERT INTO corpus.passage_reference
            (article_id, selector_xpath, quote_start, quote_end, quote_exact)
        VALUES (%s, %s, 0, 5, 'much longer than five')""", (article_id, xpath))

    expect_rejected(cur, "negative quote_start", """
        INSERT INTO corpus.passage_reference
            (article_id, selector_xpath, quote_start, quote_end, quote_exact)
        VALUES (%s, %s, -5, 3, 'abcdefgh')""", (article_id, xpath))

    expect_rejected(cur, "quote_end <= quote_start", """
        INSERT INTO corpus.passage_reference
            (article_id, selector_xpath, quote_start, quote_end, quote_exact)
        VALUES (%s, %s, 10, 10, '')""", (article_id, xpath))

    expect_rejected(cur, "resolution_status outside the enum", """
        INSERT INTO corpus.passage_reference
            (article_id, selector_xpath, quote_start, quote_end, quote_exact,
             resolution_status)
        VALUES (%s, %s, 0, 3, 'abc', 'probably-fine')""", (article_id, xpath))

    expect_rejected(cur, "a link selector with offsets but no quote", """
        INSERT INTO corpus.article_link
            (article_id, extraction_id, target_url, anchor_text, is_internal,
             selector_xpath, quote_start, quote_end)
        SELECT %s, a.current_extraction_id, 'https://x.example/', 'x', false,
               %s, 0, 5 FROM corpus.article a WHERE a.id = %s""",
                    (article_id, xpath, article_id))

    expect_rejected(cur, "block_type outside the enum", """
        INSERT INTO corpus.content_block
            (extraction_id, article_id, block_index, block_type, xpath, block_text)
        SELECT a.current_extraction_id, %s, 9001, 'sidebar', %s, 'text'
        FROM corpus.article a WHERE a.id = %s""", (article_id, xpath, article_id))

    expect_rejected(cur, "heading block with no heading_level", """
        INSERT INTO corpus.content_block
            (extraction_id, article_id, block_index, block_type, xpath, block_text)
        SELECT a.current_extraction_id, %s, 9002, 'heading', %s, 'text'
        FROM corpus.article a WHERE a.id = %s""", (article_id, xpath, article_id))

    expect_rejected(cur, "textual block with a NULL text", """
        INSERT INTO corpus.content_block
            (extraction_id, article_id, block_index, block_type, xpath)
        SELECT a.current_extraction_id, %s, 9003, 'paragraph', %s
        FROM corpus.article a WHERE a.id = %s""", (article_id, xpath, article_id))

    expect_rejected(cur, "artifact kind outside the enum", """
        INSERT INTO corpus.article_artifact
            (article_id, kind, file_path, media_type)
        VALUES (%s, 'pdf_export', 'x/y.pdf', 'application/pdf')""", (article_id,))

    expect_rejected(cur, "a second artifact of the same kind", """
        INSERT INTO corpus.article_artifact
            (article_id, kind, file_path, media_type)
        VALUES (%s, 'readability_html', 'x/y.html', 'text/html')""", (article_id,))

    expect_rejected(cur, "a second current extraction for one article", """
        INSERT INTO corpus.article_extraction
            (article_id, extractor_version, extraction_status, extracted_at, is_current)
        VALUES (%s, 'x/0.0.0', 'success', now(), true)""", (article_id,))

    expect_rejected(cur, "extraction_status outside the enum", """
        INSERT INTO corpus.article_extraction
            (article_id, extractor_version, extraction_status, extracted_at)
        VALUES (%s, 'x/0.0.0', 'mostly-fine', now())""", (article_id,))

    expect_rejected(cur, "an article whose url_hash is not in urls", """
        INSERT INTO corpus.article (url_hash, outlet, source_url)
        VALUES (repeat('f', 64), 'nowhere.hu', 'https://nowhere.hu/a')""")

    expect_rejected(cur, "deleting a urls row an article points at", """
        DELETE FROM urls WHERE url_hash =
            (SELECT url_hash FROM corpus.article WHERE id = %s)""", (article_id,))

    # -----------------------------------------------------------------
    head("FAILURE CASES - the database CANNOT reject these; verification must "
         "detect them")
    print("   Postgres has no DOM and stores paths rather than bytes, so these "
          "are\n   application-layer failures by design (docs/postgres-schema.md E.2).")
    directory = Path(citation["directory"])
    tree = reparse((directory / "readability.html").read_text(encoding="utf-8"))
    if block_id is None:
        return

    cur.execute("SAVEPOINT cannot_enforce")

    # (i) An invalid xpath. Accepted by the DB; unresolvable in the document.
    cur.execute("""INSERT INTO corpus.passage_reference
                     (article_id, content_block_id, selector_xpath, quote_start,
                      quote_end, quote_exact)
                   VALUES (%s,%s,'/html/body/article/div/p[9999]',0,3,'abc')
                   RETURNING id""", (article_id, block_id))
    bad_id = cur.fetchone()[0]
    check(resolve(tree, "/html/body/article/div/p[9999]") is None,
          "an xpath that should not resolve did")
    print("   accepted, detected   invalid xpath -> resolve() returns None")

    # (ii) quote_exact that is the right LENGTH but the wrong TEXT.
    cur.execute("SELECT block_text FROM corpus.content_block WHERE id = %s", (block_id,))
    text = cur.fetchone()[0]
    wrong = "x" * 20
    cur.execute("""INSERT INTO corpus.passage_reference
                     (article_id, content_block_id, selector_xpath, quote_start,
                      quote_end, quote_exact)
                   VALUES (%s,%s,%s,10,30,%s) RETURNING id""",
                (article_id, block_id, citation["xpath"], wrong))
    wrong_id = cur.fetchone()[0]
    cur.execute("""SELECT substring(b.block_text FROM p.quote_start + 1
                                    FOR p.quote_end - p.quote_start) = p.quote_exact
                   FROM corpus.passage_reference p
                   JOIN corpus.content_block b ON b.id = p.content_block_id
                   WHERE p.id = %s""", (wrong_id,))
    check(cur.fetchone()[0] is False,
          "a wrong quote_exact of the right length was not detected by the SQL check")
    print("   accepted, detected   wrong quote.exact -> SQL slice comparison fails")

    # (iii) Offsets past the end of the text. substring() truncates SILENTLY.
    beyond = len(text) + 50
    filler = "y" * 20
    cur.execute("""INSERT INTO corpus.passage_reference
                     (article_id, content_block_id, selector_xpath, quote_start,
                      quote_end, quote_exact)
                   VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (article_id, block_id, citation["xpath"], beyond, beyond + 20, filler))
    beyond_id = cur.fetchone()[0]
    cur.execute("""SELECT coalesce(substring(b.block_text FROM p.quote_start + 1
                                    FOR p.quote_end - p.quote_start), '') = p.quote_exact,
                          p.quote_end > length(b.block_text)
                   FROM corpus.passage_reference p
                   JOIN corpus.content_block b ON b.id = p.content_block_id
                   WHERE p.id = %s""", (beyond_id,))
    matched, out_of_range = cur.fetchone()
    check(matched is False and out_of_range is True,
          "offsets beyond the end of block_text were not detected")
    print("   accepted, detected   offsets past end -> quote_end > length(block_text)")

    # (iv) A missing artifact / image / video file.
    cur.execute("""UPDATE corpus.article_artifact
                   SET file_path = file_path || '.gone'
                   WHERE article_id = %s AND kind = 'readability_html'""", (article_id,))
    cur.execute("""SELECT file_path FROM corpus.article_artifact
                   WHERE article_id = %s AND kind = 'readability_html'""", (article_id,))
    gone = cur.fetchone()[0]
    check(not (root / gone).is_file(),
          "a deliberately broken artifact path still resolved to a file")
    print("   accepted, detected   missing artifact file -> path does not exist on disk")

    for table in ("article_image", "article_video"):
        cur.execute(f"""UPDATE corpus.{table} SET file_path = 'nope/missing.bin'
                        WHERE article_id = %s AND file_path IS NOT NULL""", (article_id,))
        cur.execute(f"""SELECT count(*) FROM corpus.{table}
                        WHERE article_id = %s AND file_path = 'nope/missing.bin'""",
                    (article_id,))
        if cur.fetchone()[0]:
            check(not (root / "nope/missing.bin").is_file(),
                  f"a deliberately broken {table} path resolved to a file")
            print(f"   accepted, detected   missing {table.split('_')[1]} file "
                  f"-> path does not exist on disk")

    cur.execute("ROLLBACK TO SAVEPOINT cannot_enforce")
    cur.execute("SELECT count(*) FROM corpus.passage_reference "
                "WHERE id = ANY(%s::uuid[])",
                ([str(bad_id), str(wrong_id), str(beyond_id)],))
    check(cur.fetchone()[0] == 0, "the deliberate bad rows survived the rollback")


# ---------------------------------------------------------------------
# 6. Re-ingestion / extraction history
# ---------------------------------------------------------------------

def verify_reingestion(cur, root: Path, expected_articles: int) -> dict:
    head("RE-INGESTION / EXTRACTION HISTORY")
    cur.execute("SELECT count(*) FROM corpus.article")
    articles = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM corpus.article_extraction")
    extractions = cur.fetchone()[0]
    generations = extractions / articles if articles else 0
    print(f"   articles {articles}, extractions {extractions} "
          f"({generations:.1f} per article)")
    check(articles == expected_articles,
          f"re-ingestion changed the article count: {articles} vs {expected_articles}")

    if extractions <= articles:
        note("only one extraction per article - run scripts/ingest_dev.py again "
             "to exercise re-extraction")
        return {"generations": generations, "reingested": False}

    # Exactly one current, and the superseded rows kept as an audit trail.
    vi.verify_one_current(cur)
    cur.execute("""SELECT count(*) FROM corpus.article_extraction WHERE NOT is_current""")
    superseded = cur.fetchone()[0]
    check(superseded == extractions - articles,
          f"{superseded} superseded rows for {extractions - articles} expected")
    print(f"   superseded extraction rows KEPT: {superseded} (audit trail)")

    # Superseded CONTENT is gone; only the current extraction's rows remain.
    for table in ("content_block", "article_link", "article_image", "article_video"):
        cur.execute(f"""SELECT count(*) FROM corpus.{table} t
                        JOIN corpus.article_extraction e ON e.id = t.extraction_id
                        WHERE NOT e.is_current""")
        stale = cur.fetchone()[0]
        check(stale == 0, f"{stale} {table} rows still belong to a superseded extraction")
    print("   superseded CONTENT deleted from all four content tables")

    cur.execute("""SELECT count(*) FROM corpus.article a
                   JOIN corpus.article_extraction e ON e.id = a.current_extraction_id
                   WHERE NOT e.is_current""")
    check(cur.fetchone()[0] == 0,
          "article.current_extraction_id points at a superseded extraction")

    # The citation must have survived, with its block link cut, not repointed.
    cur.execute("""SELECT id, content_block_id, selector_xpath, quote_start,
                          quote_end, quote_exact, resolution_status
                   FROM corpus.passage_reference ORDER BY created_at LIMIT 1""")
    row = cur.fetchone()
    if row is None:
        note("no passage_reference to test survival with")
        return {"generations": generations, "reingested": True}
    ref_id, block_id, xpath, start, end, quote, status = row
    print(f"   passage_reference {ref_id} survived; content_block_id="
          f"{block_id} status={status}")
    check(block_id is None,
          "content_block_id was not SET NULL when the block it pointed at was replaced")
    check(quote is not None and xpath is not None,
          "the citation lost its selector during re-extraction")

    # And it still validates against the NEW extraction's block at the same xpath.
    cur.execute("""SELECT substring(b.block_text FROM %s + 1 FOR %s - %s) = %s
                   FROM corpus.content_block b
                   JOIN corpus.article a ON a.id = b.article_id
                   JOIN corpus.passage_reference p ON p.article_id = a.id
                   WHERE p.id = %s AND b.xpath = %s
                     AND b.extraction_id = a.current_extraction_id""",
                (start, end, start, quote, ref_id, xpath))
    row = cur.fetchone()
    check(row is not None and row[0] is True,
          "the surviving citation no longer validates against the new extraction")
    print("   quote_exact still validates in SQL against the NEW extraction's block")
    return {"generations": generations, "reingested": True,
            "superseded": superseded, "reference_survived": True}


# ---------------------------------------------------------------------

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--dsn", default=os.environ.get("CX_DEV_DSN", DEFAULT_DSN))
    args = parser.parse_args(argv[1:])

    root = args.output_dir
    if not root.is_dir():
        print(f"no such output directory: {root}", file=sys.stderr)
        return 1
    repo = Path(__file__).resolve().parent.parent

    connection = psycopg2.connect(args.dsn)
    connection.autocommit = False
    cur = connection.cursor()
    dict_cur = connection.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cur.execute("SELECT version()")
    print(f"== {cur.fetchone()[0].split(' on ')[0]}")
    cur.execute("SELECT current_database(), current_schema()")
    print(f"== database {cur.fetchone()[0]}")

    verify_schema(cur, repo)
    pairs = verify_ingestion(cur, root)
    search_report = verify_search(dict_cur, root)

    cur.execute("SELECT count(*) FROM corpus.passage_reference")
    citation: dict = {}
    if cur.fetchone()[0] == 0:
        citation = verify_citation(cur, root, pairs)
        connection.commit()            # the citation must survive re-ingestion
    else:
        cur.execute("""SELECT p.id, p.article_id, p.selector_xpath, p.quote_exact,
                              p.content_block_id
                       FROM corpus.passage_reference p ORDER BY p.created_at LIMIT 1""")
        row = cur.fetchone()
        directory = next((str(d) for d, a in pairs if a == row[1]), None)
        citation = {"reference_id": str(row[0]), "article_id": row[1],
                    "xpath": row[2], "quote": row[3], "block_id": row[4],
                    "directory": directory}
        head("CITATION")
        print(f"   reusing the passage_reference minted on the first pass: {row[0]}")

    if citation.get("directory"):
        verify_failures(cur, root, citation)
    reingest_report = verify_reingestion(cur, root, len(pairs))
    connection.rollback()              # discard anything the failure cases left
    connection.close()

    head("RESULT")
    print(f"   {vi.checks} checks run")
    if notes:
        print(f"   {len(notes)} note(s)")
    if vi.failures:
        print(f"\n   {len(vi.failures)} FAILED:")
        for message in vi.failures[:40]:
            print("     -", message)
        return 1
    print("   ALL CHECKS PASSED")
    if search_report.get("latency_ms"):
        worst = max(search_report["latency_ms"].items(), key=lambda kv: kv[1])
        median = sorted(search_report["latency_ms"].values())[
            len(search_report["latency_ms"]) // 2]
        print(f"\n   search latency: median {median:.1f} ms, "
              f"slowest {worst[1]:.1f} ms ({worst[0]!r})")
    print(f"   extraction generations per article: "
          f"{reingest_report.get('generations', 0):.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
