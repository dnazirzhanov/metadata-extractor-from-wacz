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

That design note named only the ranking cost. It had a RECALL cost too, and it
was the larger one: handing the whole tsquery to one vector required every term
of a multi-word AND to land in the same paragraph, so 'ukrajnai fejlesztés'
returned 4 of the 11 articles that contain both words. Since 012 the terms are
addressable separately (corpus.search_terms) and an article matches when every
term appears SOMEWHERE in it - see CANDIDATES below.

Ranking follows from that. ts_rank scores a vector against a query, and returns
zero when the vector does not satisfy the whole query - so every article the 012
fix recovered scored zero and sorted last. The score is now

    term_rank   sum over TERMS of what each term is worth, wherever it sits
  + meta_rank   \
  + body_rank    >  the whole query satisfied by ONE vector - the old score,
  + caption_rank/   kept whole, now read as a CONCENTRATION BONUS

The base is always positive for anything that matched, so a document-level hit
is scored on its body rather than on nothing. Before this, body_rank and
caption_rank were guarded by `@@ q.tsq` and so were structurally zero for such
an article - its entire body contributed nothing, and the only surviving signal
was whatever partial credit ts_rank gave the metadata (ts_rank does not require
the query to match, which is why the score was not always literally zero).

The bonus weights concentration; it does not tier it. A heavily scattered
article can outrank a weakly concentrated one, and should.

For a SINGLE-TERM query the base is identically the old score, so the total is
exactly twice it and the order is unchanged - which is the property to check
first when touching any of this.

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

#: An article's METADATA hit. Since 012 this no longer GATES matching - the
#: candidate set does - it only labels why a result came back, feeding
#: `meta_match` and `match_reason`. Under --phrase the prose fields are
#: rechecked as one string, and each tag and each author is rechecked
#: SEPARATELY.
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

#: The candidate set: articles where EVERY query term appears SOMEWHERE in the
#: article - metadata, any block, or any image caption - rather than where the
#: whole query appears in ONE of those.
#:
#: This is the fix for the defect that produced 12 of the 13 standing recall
#: misses (migrations/012). Handing the whole tsquery to one vector forces every
#: term of a multi-word AND into the same paragraph, so an article that plainly
#: contains all of them, spread over its body, matched nothing. Measured on the
#: evaluation corpus: 'ukrajnai fejlesztés' returned 4 of 11 true matches.
#:
#: Shape matters as much as semantics. Each term is resolved by its OWN
#: index-served UNION - GIN on article.search_tsv, on content_block.text_tsv,
#: on article_image.caption_tsv - and the terms are intersected by counting
#: distinct ordinals. A correlated NOT EXISTS over the terms would express the
#: same set and force a sequential scan of corpus.article, which is the
#: difference between a query that works at 4.2M articles and one that does not.
#:
#: A single-term query reduces to exactly the old union, so one-word search is
#: unchanged - confirmed on the probe set, where only the three multi-word
#: queries moved.
#:
#: Zero terms (an empty query, or one that is all stopwords) yields no rows and
#: therefore matches nothing, which is what corpus.search_query already answered.
CANDIDATES = """
        q AS (SELECT corpus.search_query(%(query)s) AS tsq,
                     corpus.search_terms(%(query)s) AS terms),
        doc AS (
            SELECT m.article_id
            FROM q,
                 unnest(q.terms) WITH ORDINALITY AS t(tsq, ord),
                 LATERAL (
                     SELECT a2.id AS article_id
                       FROM corpus.article a2
                      WHERE a2.search_tsv @@ t.tsq
                     UNION
                     SELECT b.article_id
                       FROM corpus.content_block b
                       JOIN corpus.article a3 ON a3.id = b.article_id
                      WHERE b.extraction_id = a3.current_extraction_id
                        AND b.text_tsv @@ t.tsq
                     UNION
                     SELECT i.article_id
                       FROM corpus.article_image i
                       JOIN corpus.article a4 ON a4.id = i.article_id
                      WHERE i.extraction_id = a4.current_extraction_id
                        AND i.caption_tsv @@ t.tsq
                 ) m
            GROUP BY m.article_id
            HAVING count(DISTINCT t.ord) = (SELECT cardinality(terms) FROM q)
        )"""

#: The phrase recheck, unchanged in meaning and now stated once.
#:
#: A phrase is contiguous words, so it cannot span two blocks - under --phrase
#: the single-vector test is the CORRECT one, and it stays. It composes with the
#: document-level candidate set for free: if a phrase occurs in some vector then
#: every one of its terms occurs in that vector, so a phrase match is always a
#: subset of the candidates. The index still runs first and this only removes
#: survivors that matched as a bag of words.
PHRASE_HIT = ("""(
               {PHRASE_FN}(concat_ws(' ', a.title, a.subtitle, a.description),
                           %(query)s)
               OR EXISTS (SELECT 1 FROM unnest(a.tags) AS tg
                           WHERE {PHRASE_FN}(tg, %(query)s))
               OR EXISTS (SELECT 1 FROM unnest(a.authors) AS au
                           WHERE {PHRASE_FN}(au, %(query)s))
               OR EXISTS (SELECT 1 FROM corpus.content_block b
                           WHERE b.article_id = a.id
                             AND b.extraction_id = a.current_extraction_id
                             AND b.text_tsv @@ q.tsq
                             AND {PHRASE_FN}(b.block_text, %(query)s))
               OR EXISTS (SELECT 1 FROM corpus.article_image i
                           WHERE i.article_id = a.id
                             AND i.extraction_id = a.current_extraction_id
                             AND i.caption_tsv @@ q.tsq
                             AND {PHRASE_FN}(concat_ws(' ', i.caption, i.alt),
                                             %(query)s)))"""
              .replace("{PHRASE_FN}", PHRASE))

#: The match predicate itself, shared by search_articles() and matching_ids().
#: Two copies of a WHERE clause is how a ranked result list and the recall set
#: measured against it quietly stop meaning the same thing - the same argument
#: that put the ingestion INSERTs in one module (scripts/cx_ingest.py).
#: Expects the CTEs in CANDIDATES, and the same parameter names.
MATCH_WHERE = ("""a.id IN (SELECT article_id FROM doc)
          AND (NOT %(phrase)s OR """ + PHRASE_HIT + """)
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


def _blocks_for_terms(cur, article_id: int, query: str, limit: int) -> list[dict]:
    """The passages of an article matched across vectors, best term first.

    An article can now come back because its terms are spread over several
    paragraphs. No block matches the whole query - that is the definition of
    this case - so `_blocks_for` would return nothing and the caller would get a
    result it cannot cite anything from.

    So return the best block for EACH term instead: the evidence exists, it is
    just distributed, and the honest presentation is one passage per term rather
    than a single passage pretending to support the whole query. Ranked by the
    term's own rank, deduplicated when one block happens to carry two terms.
    """
    cur.execute(f"""
        SELECT DISTINCT ON (b.id)
               b.id AS block_id, b.block_index, b.block_type, b.xpath,
               b.block_text, t.ord AS term_ordinal,
               ts_rank(b.text_tsv, t.tsq) AS rank,
               ts_headline('{HEADLINE_CONFIG}', b.block_text, t.tsq,
                           %(opts)s) AS headline
        FROM corpus.article a
        JOIN corpus.content_block b
          ON b.article_id = a.id AND b.extraction_id = a.current_extraction_id
        CROSS JOIN unnest(corpus.search_terms(%(q)s)) WITH ORDINALITY AS t(tsq, ord)
        WHERE a.id = %(id)s
          AND b.text_tsv @@ t.tsq
        ORDER BY b.id, rank DESC
    """, {"id": article_id, "q": query, "opts": HEADLINE_OPTS})
    rows = [dict(row) for row in cur.fetchall()]
    rows.sort(key=lambda r: (r["term_ordinal"], -float(r["rank"])))
    return rows[:limit]


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
        WITH {CANDIDATES}
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
                                    %(query)s)))               AS caption_rank,
               tr.term_rank
        FROM corpus.article a
        CROSS JOIN q
        LEFT JOIN corpus.article_extraction e ON e.id = a.current_extraction_id
        CROSS JOIN LATERAL (
            -- The BASE score: what each term is worth wherever it sits. Summed
            -- over terms, so an article scores even when no single vector holds
            -- the whole query - which is every article the 012 fix recovered.
            SELECT coalesce(sum(
                       ts_rank(a.search_tsv, t.tsq)
                     + coalesce((SELECT max(ts_rank(b.text_tsv, t.tsq))
                                   FROM corpus.content_block b
                                  WHERE b.article_id = a.id
                                    AND b.extraction_id = a.current_extraction_id
                                    AND b.text_tsv @@ t.tsq), 0)
                     + coalesce((SELECT max(ts_rank(i.caption_tsv, t.tsq))
                                   FROM corpus.article_image i
                                  WHERE i.article_id = a.id
                                    AND i.extraction_id = a.current_extraction_id
                                    AND i.caption_tsv @@ t.tsq), 0)), 0) AS term_rank
            FROM unnest(q.terms) AS t(tsq)
        ) tr
        WHERE {MATCH_WHERE}
        ORDER BY (tr.term_rank
                  -- CONCENTRATION BONUS: the whole query satisfied by ONE
                  -- vector, which is exactly the old score, kept whole and now
                  -- added on top of the base rather than being the whole story.
                  -- It WEIGHTS concentration, it does not tier it: a heavily
                  -- scattered article can still outrank a weakly concentrated
                  -- one, and should - repeated mentions throughout a piece are
                  -- better evidence than one thin co-occurrence in a sentence.
                  -- Measured on 'ukrajnai fejlesztés': concentrated hits score
                  -- 0.163-1.886, document-level hits 0.091-0.469, and they
                  -- interleave.
                  + ts_rank(a.search_tsv, q.tsq)
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
                 a.published_at DESC NULLS LAST,
                 -- Deterministic last resort. 'Magyarország' puts 82 articles
                 -- on one score and 63 on another, so without this the tail of
                 -- a result list is in unspecified order and a harness that
                 -- diffs two runs reports changes that are not changes.
                 a.id
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
        hit["term_rank"] = float(hit["term_rank"] or 0.0)
        # base + concentration bonus, matching the ORDER BY exactly. Keeping the
        # two visible separately is the point: term_rank says the terms are
        # there, the bonus says they are there TOGETHER.
        hit["score"] = (hit["term_rank"] + hit["meta_rank"]
                        + hit["body_rank"] + hit["caption_rank"])
        hit["match_reason"] = (
            "both" if hit["meta_match"] and hit["body_rank"]
            else "metadata" if hit["meta_match"]
            else "body" if hit["body_rank"]
            else "caption" if hit["caption_rank"]
            # Every term is in the article but no single vector holds them all.
            # Before 012 this article was not returned at all; it is the case
            # the fix exists for, and it is labelled rather than disguised as a
            # body hit, because no one passage supports the whole query.
            else "document")
        if hit["body_rank"]:
            hit["blocks"] = _blocks_for(cur, hit["id"], query,
                                        blocks_per_article, phrase=phrase)
        elif hit["match_reason"] == "document":
            hit["blocks"] = _blocks_for_terms(cur, hit["id"], query,
                                              blocks_per_article)
        else:
            hit["blocks"] = []
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
        WITH {CANDIDATES}
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
