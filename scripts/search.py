#!/usr/bin/env python3
"""The first query layer: PostgreSQL full-text search over the corpus.

Deliberately only Postgres. No embeddings, no vector similarity, no external
engine - the point is to measure how far the corpus search vectors + GIN take
us before adding anything.

No DDL. Every function here is a query over columns migrations 001-007 already
created, so the approved schema stays frozen:

    article.search_tsv        title(A) subtitle(B) description(B) authors(C) tags(C)
    content_block.text_tsv    the block's own text

There is deliberately no third whole-body vector: an article matches on its body
through a semi-join over its blocks (docs/postgres-schema.md D.2). The cost of
that choice is that whole-document ts_rank is unavailable, so the article score
combines the metadata rank with the best matching block's rank - for a citation
tool the best passage is what you want to surface anyway.

Usage:  scripts/search.py "Orbán Viktor" [--outlet origo.hu] [--tag Magyarország]
        scripts/search.py --tag-only "Magyarország"
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

#: A headline is for a human reading a result list; the block's full text is
#: returned beside it so an agent never has to parse the markers.
HEADLINE_OPTS = ("MaxFragments=2,FragmentDelimiter= … ,"
                 "MinWords=5,MaxWords=22,StartSel=«,StopSel=»")


def connect(dsn: str | None = None):
    return psycopg2.connect(dsn or os.environ.get("CX_DEV_DSN", DEFAULT_DSN))


def _blocks_for(cur, article_id: int, query: str, limit: int) -> list[dict]:
    """The matching blocks of an article, best first.

    Restricted to the article's CURRENT extraction. Today superseded content is
    deleted on supersede so the filter is redundant - but retention is a config
    knob (F.2), and the day it is turned on, search must not start returning
    passages from a reading nobody can cite any more.
    """
    cur.execute(f"""
        SELECT b.block_index, b.block_type, b.xpath, b.block_text,
               ts_rank(b.text_tsv, {QUERY}(%(q)s)) AS rank,
               ts_headline('{HEADLINE_CONFIG}', b.block_text,
                           {QUERY}(%(q)s),
                           %(opts)s) AS headline
        FROM corpus.content_block b
        JOIN corpus.article a ON a.id = b.article_id
        WHERE b.article_id = %(id)s
          AND b.extraction_id = a.current_extraction_id
          AND b.text_tsv @@ {QUERY}(%(q)s)
        ORDER BY rank DESC, b.block_index
        LIMIT %(limit)s
    """, {"id": article_id, "q": query, "opts": HEADLINE_OPTS, "limit": limit})
    return [dict(row) for row in cur.fetchall()]


def search_articles(cur, query: str, *, limit: int = 10, outlet: str | None = None,
                    tag: str | None = None, blocks_per_article: int = 3) -> list[dict]:
    """Search article metadata AND article prose.

    Returns enough for a UI or an agent to act without a second round trip:
    identity, the display fields, WHY it matched, and - when the match is in the
    prose - the matching blocks with the selector needed to cite them.
    """
    cur.execute(f"""
        WITH q AS (SELECT {QUERY}(%(query)s) AS tsq)
        SELECT a.id, a.url_hash, a.title, a.subtitle, a.outlet, a.section,
               a.published_at, a.canonical_url, a.source_url, a.tags, a.authors,
               e.extraction_status,
               a.search_tsv @@ q.tsq                       AS meta_match,
               ts_rank(a.search_tsv, q.tsq)                AS meta_rank,
               (SELECT max(ts_rank(b.text_tsv, q.tsq))
                  FROM corpus.content_block b
                 WHERE b.article_id = a.id
                   AND b.extraction_id = a.current_extraction_id
                   AND b.text_tsv @@ q.tsq)                AS body_rank
        FROM corpus.article a
        CROSS JOIN q
        LEFT JOIN corpus.article_extraction e ON e.id = a.current_extraction_id
        WHERE (a.search_tsv @@ q.tsq
               OR EXISTS (SELECT 1 FROM corpus.content_block b
                           WHERE b.article_id = a.id
                             AND b.extraction_id = a.current_extraction_id
                             AND b.text_tsv @@ q.tsq))
          AND (%(outlet)s IS NULL OR a.outlet = %(outlet)s)
          AND (%(tag)s   IS NULL OR a.tags @> ARRAY[%(tag)s]::text[])
        ORDER BY (ts_rank(a.search_tsv, q.tsq)
                  + coalesce((SELECT max(ts_rank(b.text_tsv, q.tsq))
                                FROM corpus.content_block b
                               WHERE b.article_id = a.id
                                 AND b.extraction_id = a.current_extraction_id
                                 AND b.text_tsv @@ q.tsq), 0)) DESC,
                 a.published_at DESC NULLS LAST
        LIMIT %(limit)s
    """, {"query": query, "outlet": outlet, "tag": tag, "limit": limit})

    results = []
    for row in cur.fetchall():
        hit = dict(row)
        hit["body_rank"] = float(hit["body_rank"] or 0.0)
        hit["meta_rank"] = float(hit["meta_rank"] or 0.0)
        hit["score"] = hit["meta_rank"] + hit["body_rank"]
        hit["match_reason"] = ("both" if hit["meta_match"] and hit["body_rank"]
                              else "metadata" if hit["meta_match"] else "body")
        hit["blocks"] = (_blocks_for(cur, hit["id"], query, blocks_per_article)
                         if hit["body_rank"] else [])
        results.append(hit)
    return results


def search_article_content(cur, query: str, *, limit: int = 20) -> list[dict]:
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
        ORDER BY rank DESC, a.published_at DESC NULLS LAST, b.block_index
        LIMIT %(limit)s
    """, {"q": query, "opts": HEADLINE_OPTS, "limit": limit})
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
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--tag-only", action="store_true",
                        help="exact tag filter instead of full-text search")
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
        elif args.blocks:
            rows = search_article_content(cur, args.query, limit=args.limit)
            print(f"== block search {args.query!r}: {len(rows)} block(s)")
            for row in rows:
                print(f"\n   [{row['rank']:.4f}] {row['outlet']} block "
                      f"{row['block_index']} ({row['block_type']})")
                print(f"   {row['headline']}")
                print(f"   {row['xpath']}")
        else:
            rows = search_articles(cur, args.query, limit=args.limit,
                                   outlet=args.outlet, tag=args.tag)
            print(f"== search {args.query!r}: {len(rows)} article(s)")
            _print(rows)
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
