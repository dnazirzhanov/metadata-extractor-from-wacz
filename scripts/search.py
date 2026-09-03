#!/usr/bin/env python3
"""The first query layer: PostgreSQL full-text search over the corpus.

Deliberately only Postgres. No embeddings, no vector similarity, no external
engine - the point is to measure how far the corpus search vectors + GIN take
us before adding anything.

No DDL. Every function here is a query over columns migrations 001-007 already
created, so the approved schema stays frozen:

    article.search_tsv          title(A) subtitle(B) description(B) authors(C) tags(C)
    content_block.text_tsv      the block's own text
    article_image.caption_tsv   caption + alt + credit (008; discoverable, and
                                deliberately NOT citable - it carries no selector)

There is deliberately no third whole-body vector: an article matches on its body
through a semi-join over its blocks (docs/postgres-schema.md D.2). The cost of
that choice is that whole-document ts_rank is unavailable, so the article score
combines the metadata rank with the best matching block's rank - for a citation
tool the best passage is what you want to surface anyway.

Filters (--author, --section, --from/--to) are EXACT and are applied as
predicates, never as full-text. --phrase is different in kind: it is a recheck
over the same candidate set the index already produced, because the stored
vectors cannot answer a positional question (migrations/009).

Usage:  scripts/search.py "Orbán Viktor" [--outlet origo.hu] [--tag Magyarország]
        scripts/search.py "Orbán Viktor" --phrase
        scripts/search.py "kormány" --author "Nagy Márton" --from 2026-01-01
        scripts/search.py --tag-only "Magyarország"
        scripts/search.py --author-only "Nagy Márton"
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg2
import psycopg2.extras

DEFAULT_DSN = ("host=127.0.0.1 port=55433 user=causalia password=dev "
               "dbname=causalia_dev")

#: Queries go through corpus.search_query(), not websearch_to_tsquery(): the
#: vectors hold two lexemes per word (unaccented lemma + unaccented surface) and
#: a query has to be expanded into the matching per-term alternation. See
#: migrations/007.
QUERY = "corpus.search_query"

#: ts_headline takes ONE configuration, so it cannot reproduce the union the
#: vectors are built from. The surface configuration is the safe half: it
#: highlights a word only when the accent-folded spelling really matches, so an
#: inflected form matched via the lemma side goes un-highlighted. That is the
#: right way to be wrong here - this project refuses to point at a passage it
#: cannot verify, and under-highlighting is a cosmetic loss where highlighting
#: the wrong word (hungarian_ci would offer "orra" for a query about "Orbán")
#: is a fabricated citation.
HEADLINE_CONFIG = "corpus.hungarian_surface"

#: Phrase search is a RECHECK, never the primary matcher. corpus.search_query
#: still runs first and is still served by the GIN index; corpus.phrase_match
#: then re-tests the survivors with document-order vectors. Doing it the other
#: way round forces a sequential scan over every block, and using <-> against
#: the stored vector is simply wrong - it can fabricate an adjacency that is not
#: in the text. Both are explained at length in migrations/009_search_filters.sql.
PHRASE = "corpus.phrase_match"

#: An article's METADATA hit. Under --phrase the prose fields are rechecked as
#: one string, and each tag and each author is rechecked SEPARATELY.
#:
#: Testing the arrays element by element rather than joined is the whole point.
#: Joining them would let a phrase straddle two independent tags - an adjacency
#: the page never printed - while skipping them altogether loses the case that
#: motivated this: an article tagged exactly "orosz-ukrán háború" is the best
#: possible answer to that phrase, and joining or skipping both get it wrong.
META_HIT = (
    "(a.search_tsv @@ q.tsq AND (NOT %(phrase)s"
    " OR " + PHRASE + "(concat_ws(' ', a.title, a.subtitle, a.description),"
    " %(query)s)"
    " OR EXISTS (SELECT 1 FROM unnest(a.tags) AS tg"
    "             WHERE " + PHRASE + "(tg, %(query)s))"
    " OR EXISTS (SELECT 1 FROM unnest(a.authors) AS au"
    "             WHERE " + PHRASE + "(au, %(query)s))))")

#: The match predicate itself, shared by search_articles() and matching_ids().
#: Two copies of a WHERE clause is how a ranked result list and the recall set
#: measured against it quietly stop meaning the same thing - the same argument
#: that put the ingestion INSERTs in one module (scripts/cx_ingest.py).
#: Expects a CTE `q` holding the tsquery, and the same parameter names.
MATCH_WHERE = ("(" + META_HIT + """
               OR EXISTS (SELECT 1 FROM corpus.content_block b
                           WHERE b.article_id = a.id
                             AND b.extraction_id = a.current_extraction_id
                             AND b.text_tsv @@ q.tsq
                             AND (NOT %(phrase)s
                                  OR {PHRASE_FN}(b.block_text, %(query)s)))
               OR EXISTS (SELECT 1 FROM corpus.article_image i
                           WHERE i.article_id = a.id
                             AND i.extraction_id = a.current_extraction_id
                             AND i.caption_tsv @@ q.tsq
                             AND (NOT %(phrase)s
                                  OR {PHRASE_FN}(concat_ws(' ', i.caption, i.alt),
                                              %(query)s))))
          AND (%(outlet)s  IS NULL OR a.outlet = %(outlet)s)
          AND (%(tag)s     IS NULL OR a.tags @> ARRAY[%(tag)s]::text[])
          AND (%(author)s  IS NULL OR a.authors @> ARRAY[%(author)s]::text[])
          AND (%(section)s IS NULL OR a.section = %(section)s)
          AND (%(date_from)s IS NULL
               OR a.published_at >= %(date_from)s::timestamptz)
          AND (%(date_to)s IS NULL
               OR a.published_at < (%(date_to)s::date + 1)::timestamptz)
""".replace("{PHRASE_FN}", PHRASE))

#: A headline is for a human reading a result list; the block's full text is
#: returned beside it so an agent never has to parse the markers.
HEADLINE_OPTS = ("MaxFragments=2,FragmentDelimiter= … ,"
                 "MinWords=5,MaxWords=22,StartSel=«,StopSel=»")


def connect(dsn: str | None = None):
    return psycopg2.connect(dsn or os.environ.get("CX_DEV_DSN", DEFAULT_DSN))


def _blocks_for(cur, article_id: int, query: str, limit: int,
                phrase: bool = False) -> list[dict]:
    """The matching blocks of an article, best first.

    Restricted to the article's CURRENT extraction. Today superseded content is
    deleted on supersede so the filter is redundant - but retention is a config
    knob (F.2), and the day it is turned on, search must not start returning
    passages from a reading nobody can cite any more.
    """
    cur.execute(f"""
        SELECT b.id AS block_id,
               b.block_index, b.block_type, b.xpath, b.block_text,
               ts_rank(b.text_tsv, {QUERY}(%(q)s)) AS rank,
               ts_headline('{HEADLINE_CONFIG}', b.block_text,
                           {QUERY}(%(q)s),
                           %(opts)s) AS headline
        FROM corpus.content_block b
        JOIN corpus.article a ON a.id = b.article_id
        WHERE b.article_id = %(id)s
          AND b.extraction_id = a.current_extraction_id
          AND b.text_tsv @@ {QUERY}(%(q)s)
          AND (NOT %(phrase)s OR {PHRASE}(b.block_text, %(q)s))
        ORDER BY rank DESC, b.block_index
        LIMIT %(limit)s
    """, {"id": article_id, "q": query, "opts": HEADLINE_OPTS, "limit": limit,
          "phrase": phrase})
    return [dict(row) for row in cur.fetchall()]


def search_articles(cur, query: str, *, limit: int = 10, outlet: str | None = None,
                    tag: str | None = None, blocks_per_article: int = 3,
                    author: str | None = None, section: str | None = None,
                    date_from: str | None = None, date_to: str | None = None,
                    phrase: bool = False) -> list[dict]:
    """Search article metadata, article prose AND image captions.

    Returns enough for a UI or an agent to act without a second round trip:
    identity, the display fields, WHY it matched, and - when the match is in the
    prose - the matching blocks with the selector needed to cite them.

    A caption match sets `caption_match` and contributes to the score, but
    yields no blocks: caption text has no selector, so it can be surfaced and
    ranked but not cited. `match_reason` says `caption` when that is the ONLY
    reason the article came back, so a caller can tell "found the evidence" from
    "found the article" without inspecting the ranks.

    `author`, `section` and the `date_from`/`date_to` range are EXACT predicates,
    deliberately not full-text. An author is matched by set membership on the
    authors array - the same argument as tags (docs/postgres-schema.md D.3): the
    weight-C match in search_tsv also fires on tags and on body prose, so a
    byline filter built on it would return articles the person did not write.

    `phrase=True` additionally requires the query to appear as an adjacent word
    sequence. It never replaces the tsquery - that still runs first, on the
    index - it only removes survivors that matched as a bag of words.
    `date_to` is INCLUSIVE of the whole day given.
    """
    cur.execute(f"""
        WITH q AS (SELECT {QUERY}(%(query)s) AS tsq)
        SELECT a.id, a.url_hash, a.title, a.subtitle, a.outlet, a.section,
               a.published_at, a.canonical_url, a.source_url, a.tags, a.authors,
               e.extraction_status,
               {META_HIT}                                  AS meta_match,
               ts_rank(a.search_tsv, q.tsq)                AS meta_rank,
               (SELECT max(ts_rank(b.text_tsv, q.tsq))
                  FROM corpus.content_block b
                 WHERE b.article_id = a.id
                   AND b.extraction_id = a.current_extraction_id
                   AND b.text_tsv @@ q.tsq
                   AND (NOT %(phrase)s
                        OR {PHRASE}(b.block_text, %(query)s)))  AS body_rank,
               (SELECT max(ts_rank(i.caption_tsv, q.tsq))
                  FROM corpus.article_image i
                 WHERE i.article_id = a.id
                   AND i.extraction_id = a.current_extraction_id
                   AND i.caption_tsv @@ q.tsq
                   AND (NOT %(phrase)s
                        OR {PHRASE}(concat_ws(' ', i.caption, i.alt),
                                    %(query)s)))               AS caption_rank
        FROM corpus.article a
        CROSS JOIN q
        LEFT JOIN corpus.article_extraction e ON e.id = a.current_extraction_id
        WHERE {MATCH_WHERE}
        ORDER BY (ts_rank(a.search_tsv, q.tsq)
                  + coalesce((SELECT max(ts_rank(b.text_tsv, q.tsq))
                                FROM corpus.content_block b
                               WHERE b.article_id = a.id
                                 AND b.extraction_id = a.current_extraction_id
                                 AND b.text_tsv @@ q.tsq), 0)
                  + coalesce((SELECT max(ts_rank(i.caption_tsv, q.tsq))
                                FROM corpus.article_image i
                               WHERE i.article_id = a.id
                                 AND i.extraction_id = a.current_extraction_id
                                 AND i.caption_tsv @@ q.tsq), 0)) DESC,
                 a.published_at DESC NULLS LAST
        LIMIT %(limit)s
    """, {"query": query, "outlet": outlet, "tag": tag, "limit": limit,
          "author": author, "section": section, "phrase": phrase,
          "date_from": date_from, "date_to": date_to})

    results = []
    for row in cur.fetchall():
        hit = dict(row)
        hit["body_rank"] = float(hit["body_rank"] or 0.0)
        hit["meta_rank"] = float(hit["meta_rank"] or 0.0)
        hit["caption_rank"] = float(hit["caption_rank"] or 0.0)
        hit["caption_match"] = hit["caption_rank"] > 0
        hit["score"] = hit["meta_rank"] + hit["body_rank"] + hit["caption_rank"]
        hit["match_reason"] = (
            "both" if hit["meta_match"] and hit["body_rank"]
            else "metadata" if hit["meta_match"]
            else "body" if hit["body_rank"]
            else "caption")
        hit["blocks"] = (_blocks_for(cur, hit["id"], query, blocks_per_article,
                                     phrase=phrase)
                         if hit["body_rank"] else [])
        results.append(hit)
    return results


def matching_ids(cur, query: str, *, outlet: str | None = None,
                 tag: str | None = None, author: str | None = None,
                 section: str | None = None, date_from: str | None = None,
                 date_to: str | None = None, phrase: bool = False) -> set[int]:
    """Every article id the query matches - no ranking, no headlines, no LIMIT.

    Recall measurement needs the COMPLETE set. Comparing a LIMITed result list
    against an unlimited yardstick counts everything below the cut as a recall
    miss, which is not a measurement of anything. On the 1,008-article
    evaluation corpus that mistake turned 13 real misses into 546 reported
    ones, because Magyarorszag matches 254 articles and the list stopped at 50.

    Use this for "did it find them"; use search_articles for "what to show".
    """
    cur.execute(f"""
        WITH q AS (SELECT {QUERY}(%(query)s) AS tsq)
        SELECT a.id
        FROM corpus.article a
        CROSS JOIN q
        WHERE {MATCH_WHERE}
    """, {"query": query, "outlet": outlet, "tag": tag, "author": author,
          "section": section, "phrase": phrase,
          "date_from": date_from, "date_to": date_to})
    return {row[0] for row in cur.fetchall()}


def search_article_content(cur, query: str, *, limit: int = 20,
                           phrase: bool = False) -> list[dict]:
    """Block-level search: the citable unit, straight out."""
    cur.execute(f"""
        SELECT b.id AS block_id, b.article_id, a.title, a.outlet, a.url_hash,
               b.block_index, b.block_type, b.xpath, b.block_text,
               ts_rank(b.text_tsv, {QUERY}(%(q)s)) AS rank,
               ts_headline('{HEADLINE_CONFIG}', b.block_text,
                           {QUERY}(%(q)s), %(opts)s) AS headline
        FROM corpus.content_block b
        JOIN corpus.article a ON a.id = b.article_id
        WHERE b.extraction_id = a.current_extraction_id
          AND b.text_tsv @@ {QUERY}(%(q)s)
          AND (NOT %(phrase)s OR {PHRASE}(b.block_text, %(q)s))
        ORDER BY rank DESC, a.published_at DESC NULLS LAST, b.block_index
        LIMIT %(limit)s
    """, {"q": query, "opts": HEADLINE_OPTS, "limit": limit, "phrase": phrase})
    return [dict(row) for row in cur.fetchall()]


def filter_by_tag(cur, tag: str, *, limit: int = 50) -> list[dict]:
    """Exact tag filtering, kept SEPARATE from full-text on purpose.

    `tags @> ARRAY[tag]` is an exact set-membership test served by the GIN index
    on tags. A tsvector match on the same word would also fire on body-adjacent
    prose, which is the wrong answer for a filter (docs/postgres-schema.md D.3).
    """
    cur.execute("""
        SELECT id, url_hash, title, outlet, published_at, canonical_url, tags
        FROM corpus.article
        WHERE tags @> ARRAY[%(tag)s]::text[]
        ORDER BY published_at DESC NULLS LAST
        LIMIT %(limit)s
    """, {"tag": tag, "limit": limit})
    return [dict(row) for row in cur.fetchall()]


def filter_by_author(cur, author: str, *, limit: int = 50) -> list[dict]:
    """Exact byline filter, served by article_authors_idx (migration 009).

    Kept separate from full-text for the same reason as filter_by_tag: the
    weight-C match in search_tsv also fires on tags and on body prose, so it
    answers "mentions this person", not "was written by them".
    """
    cur.execute("""
        SELECT id, url_hash, title, outlet, published_at, canonical_url, authors
        FROM corpus.article
        WHERE authors @> ARRAY[%(author)s]::text[]
        ORDER BY published_at DESC NULLS LAST
        LIMIT %(limit)s
    """, {"author": author, "limit": limit})
    return [dict(row) for row in cur.fetchall()]


def _print(results: list[dict]) -> None:
    if not results:
        print("   (no results)")
        return
    for hit in results:
        print(f"\n   [{hit['score']:.4f} {hit['match_reason']:>8}] "
              f"{hit['outlet']}  {str(hit['published_at'])[:10]}")
        print(f"   {hit['title']}")
        print(f"   {hit['canonical_url'] or hit['source_url']}")
        if hit["tags"]:
            print(f"   tags: {', '.join(hit['tags'][:8])}")
        for block in hit["blocks"]:
            print(f"     block {block['block_index']:>3} {block['block_type']:<9} "
                  f"{block['headline']}")
            print(f"       {block['xpath']}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--outlet", default=None)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--author", default=None,
                        help="exact byline filter (set membership on authors)")
    parser.add_argument("--section", default=None, help="exact section filter")
    # 'from' is a keyword, so the flag and the attribute have to differ.
    parser.add_argument("--from", dest="date_from", default=None,
                        metavar="YYYY-MM-DD", help="published on or after")
    parser.add_argument("--to", dest="date_to", default=None,
                        metavar="YYYY-MM-DD",
                        help="published on or before (inclusive of that day)")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--phrase", action="store_true",
                        help="require the query as an adjacent word sequence, "
                             "not as a bag of words")
    parser.add_argument("--tag-only", action="store_true",
                        help="exact tag filter instead of full-text search")
    parser.add_argument("--author-only", action="store_true",
                        help="exact byline filter instead of full-text search")
    parser.add_argument("--blocks", action="store_true",
                        help="block-level search instead of article search")
    args = parser.parse_args(argv[1:])

    connection = connect(args.dsn)
    with connection.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        if args.tag_only:
            rows = filter_by_tag(cur, args.query, limit=args.limit)
            print(f"== exact tag filter {args.query!r}: {len(rows)} article(s)")
            for row in rows:
                print(f"   {row['outlet']:<16} {row['title']}")
        elif args.author_only:
            rows = filter_by_author(cur, args.query, limit=args.limit)
            print(f"== exact byline filter {args.query!r}: {len(rows)} article(s)")
            for row in rows:
                print(f"   {str(row['published_at'])[:10]}  {row['outlet']:<16} "
                      f"{row['title']}")
        elif args.blocks:
            rows = search_article_content(cur, args.query, limit=args.limit,
                                          phrase=args.phrase)
            kind = "phrase" if args.phrase else "block"
            print(f"== {kind} search {args.query!r}: {len(rows)} block(s)")
            for row in rows:
                print(f"\n   [{row['rank']:.4f}] {row['outlet']} block "
                      f"{row['block_index']} ({row['block_type']})")
                print(f"   {row['headline']}")
                print(f"   {row['xpath']}")
        else:
            rows = search_articles(cur, args.query, limit=args.limit,
                                   outlet=args.outlet, tag=args.tag,
                                   author=args.author, section=args.section,
                                   date_from=args.date_from,
                                   date_to=args.date_to, phrase=args.phrase)
            active = [f"{k}={v}" for k, v in (
                ("outlet", args.outlet), ("tag", args.tag),
                ("author", args.author), ("section", args.section),
                ("from", args.date_from), ("to", args.date_to)) if v]
            if args.phrase:
                active.insert(0, "phrase")
            suffix = f"  [{', '.join(active)}]" if active else ""
            print(f"== search {args.query!r}{suffix}: {len(rows)} article(s)")
            _print(rows)
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
