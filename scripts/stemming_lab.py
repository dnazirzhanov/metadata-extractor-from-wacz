#!/usr/bin/env python3
"""Measure candidate Hungarian text-search configurations against each other.

The corpus loses search recall because `corpus.hungarian_ci` mishandles Hungarian
morphology. This scores every candidate fix on the SAME text with the SAME
queries, so the choice can be made on numbers rather than on argument.

It reads the 36 articles out of the development database and writes them into a
throwaway lab database as flat text. That is deliberate: the question here is
text -> lexeme -> match, which needs no schema, no migrations and no ingestion
path. The development database and the approved `corpus.hungarian_ci` are never
touched - this connects to the dev database read-only.

Ground truth for scoring is an accent-folded substring match computed in Python:
an article "contains" a term if the characters are there. That is a crude
yardstick, and deliberately independent of anything Postgres does, so a
configuration cannot score well by agreeing with itself.

A candidate whose configuration cannot be built (C and F need hu_HU hunspell
dictionaries in $SHAREDIR/tsearch_data) is reported UNAVAILABLE and skipped, so
one missing dictionary never blocks the rest of the measurement.

Usage:  scripts/stemming_lab.py [--dev-dsn ...] [--lab-dsn ...] [-o out.json]

Against the 1,008-article evaluation database on milab2, with a throwaway lab
container beside it:

    docker run -d --name cx-pg-lab -p 127.0.0.1:55434:5432 \
        -e POSTGRES_PASSWORD=lab -e POSTGRES_USER=causalia \
        -e POSTGRES_DB=causalia_lab postgres:16

    scripts/stemming_lab.py \
      --dev-dsn "host=127.0.0.1 port=55435 user=causalia password=eval dbname=causalia_eval" \
      --lab-dsn "host=127.0.0.1 port=55434 user=causalia password=lab dbname=causalia_lab" \
      -o /tmp/stemming_lab.json

    docker rm -f cx-pg-lab          # it is throwaway by design

MEASURED 2026-09-03 on those 1,008 articles, 19 queries:

    cand  recall  precision   what it is
    A      66.8%      90.0%   the retired corpus.hungarian_ci
    D      58.5%     100.0%   no stemming, prefix query
    E      73.2%      81.9%   pg_trgm word_similarity 0.6
    H      88.6%      89.8%   SHIPPED TODAY (migration 007)
    H1     83.5%      92.8%   min lemma length 4  - breaks Orbannak (99%->1%)
    H2     78.8%      93.4%   accent-free surface-only - breaks kormanynak
    H3     78.9%      93.8%   accent-free surface-prefix - same
    H4     88.6%      92.8%   drop 1-2 char lemmas   <-- no recall lost
    H5     88.6%      92.8%   H4 restricted to accent-free terms (identical)

H4 is the one worth shipping: identical recall to H on every single query, +3.0
points of mean precision, and it is a QUERY-SIDE change only - the stored vector
is untouched, so no generated column is rebuilt and no GIN index is reindexed.
The whole gain is one query, `orban`, whose precision goes 43% -> 100% as the
result set drops from 242 articles to 103, exactly matching accented `Orban`.
The guard fires on 114 of 4,400 distinct title words (2.6%), and they are
over-stems: simon->si, usa->us, iden->id, kik->ki.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import psycopg2
import psycopg2.extras

DEV_DSN = "host=127.0.0.1 port=55433 user=causalia password=dev dbname=causalia_dev"
LAB_DSN = "host=127.0.0.1 port=55434 user=causalia password=lab dbname=causalia_lab"

#: Each candidate: a key, a label, the SQL that creates its configuration, and
#: how a document vector and a query are built from it.
CANDIDATES = [
    ("A", "Current — unaccent → snowball",
     "accent-insensitive, but the stemmer is fed un-Hungarian input"),
    ("B", "Snowball only, accents kept",
     "the same stemmer, without unaccent in front of it"),
    ("C", "Hunspell hu_HU → snowball fallback",
     "a real morphological dictionary, accents kept"),
    ("D", "No stemming — unaccent + prefix query",
     "lexemes are whole words; the query matches as a prefix"),
    ("F", "Hunspell, then unaccent the lemmas",
     "lemmatise first, fold accents afterwards — both properties in one vector"),
    # --- the H family. One shared vector, four QUERY-side behaviours. -----
    ("H", "SHIPPED (migration 007) — (lemma | surface) per term",
     "the baseline: what corpus.search_query does today"),
    ("H1", "H + minimum lemma length 4",
     "drop an over-stemmed lemma like 'or'; keep every normal stem"),
    ("H2", "H + accent-free terms go surface-only",
     "distrust the stemmer when it was handed un-Hungarian input"),
    ("H3", "H + accent-free terms go surface-PREFIX",
     "same distrust, but 'orban':* recovers the inflections H2 loses"),
    ("H4", "H + drop lemmas of 1-2 characters",
     "kills the collision magnet 'or'; keeps the legitimate 3-char 'orb'"),
    ("H5", "H4, but only for accent-free terms",
     "the narrowest possible guard, on the only spelling that misleads snowball"),
]

PROBES = ["Orbán", "Orbánnak", "Orbánt", "kormány", "kormányban", "kormánynak",
          "Magyarország", "Magyarországról", "migráció", "migrációról",
          "külföld", "külföldön", "háború", "háborúban", "orra"]

#: Base forms a user would type, plus the inflected forms that currently fail,
#: plus accent-stripped spellings.
QUERIES = [
    ("kormány", "base"), ("kormányban", "inflected"), ("kormanynak", "inflected+unaccented"),
    ("külföld", "base"), ("külföldön", "inflected"),
    ("migráció", "base"), ("migrációról", "inflected"),
    ("Orbán", "base"), ("Orbánnak", "inflected"), ("Orbant", "inflected+unaccented"),
    ("Magyarország", "base"), ("Magyarországról", "inflected"),
    ("magyarorszag", "unaccented"), ("orban", "unaccented"),
    ("háború", "base"), ("háborúban", "inflected"),
    ("Európa", "base"), ("Szijjártó", "base"), ("koronavírus", "base"),
]


def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (text or "").lower())
                   if not unicodedata.combining(c))


# ---------------------------------------------------------------------
# Lab setup
# ---------------------------------------------------------------------

BASE_SQL = """
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
DROP SCHEMA IF EXISTS lab CASCADE;
CREATE SCHEMA lab;

CREATE OR REPLACE FUNCTION lab.unaccent_i(t text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $$ SELECT unaccent('unaccent', t) $$;

CREATE TABLE lab.doc (
    article_id  bigint PRIMARY KEY,
    outlet      text NOT NULL,
    title       text,
    tags        text[],
    meta        text NOT NULL,
    body        text NOT NULL,
    full_text   text NOT NULL
);
"""

#: Per-candidate setup. Each is attempted on its own savepoint and a candidate
#: whose configuration cannot be built is REPORTED AS UNAVAILABLE rather than
#: killing the run. C and F need hu_HU hunspell dictionaries in
#: $SHAREDIR/tsearch_data, which milab2 does not have - that is precisely the
#: reason migration 007 chose snowball, and it must not stop the other
#: candidates from being measured.
CHUNKS = {
"A": """
DROP TEXT SEARCH CONFIGURATION IF EXISTS lab.cfg_a CASCADE;
CREATE TEXT SEARCH CONFIGURATION lab.cfg_a (COPY = hungarian);
ALTER TEXT SEARCH CONFIGURATION lab.cfg_a
  ALTER MAPPING FOR hword, hword_part, word WITH unaccent, hungarian_stem;
""",
"B": """
DROP TEXT SEARCH CONFIGURATION IF EXISTS lab.cfg_b CASCADE;
CREATE TEXT SEARCH CONFIGURATION lab.cfg_b (COPY = hungarian);
""",
"C": """
DROP TEXT SEARCH CONFIGURATION IF EXISTS lab.cfg_c CASCADE;
CREATE TEXT SEARCH CONFIGURATION lab.cfg_c (COPY = hungarian);
ALTER TEXT SEARCH CONFIGURATION lab.cfg_c
  ALTER MAPPING FOR hword, hword_part, word WITH hunspell_hu, hungarian_stem;
""",
"D": """
DROP TEXT SEARCH CONFIGURATION IF EXISTS lab.cfg_d CASCADE;
CREATE TEXT SEARCH CONFIGURATION lab.cfg_d (COPY = simple);
ALTER TEXT SEARCH CONFIGURATION lab.cfg_d
  ALTER MAPPING FOR hword, hword_part, word WITH unaccent, simple;
""",
"F": """
CREATE OR REPLACE FUNCTION lab.lemma_text(t text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $$
  SELECT array_to_string(tsvector_to_array(to_tsvector('lab.cfg_c', t)), ' ')
$$;
SELECT lab.lemma_text('proba');
""",
"H": '\n-- =====================================================================\n-- The H family: what migration 007 actually shipped, and query-side fixes\n-- for the accent defect it left behind.\n--\n-- Every variant below shares ONE document vector, lab.sv(). That is the whole\n-- point of measuring them: if a query-side change is enough, the fix needs no\n-- table rewrite and no GIN rebuild on 4.2M articles.\n-- =====================================================================\n\n-- The two configurations 007 created.\nDROP TEXT SEARCH CONFIGURATION IF EXISTS lab.cfg_lemma CASCADE;\nCREATE TEXT SEARCH CONFIGURATION lab.cfg_lemma (COPY = pg_catalog.hungarian);\n\nDROP TEXT SEARCH DICTIONARY IF EXISTS lab.surface_dict CASCADE;\nCREATE TEXT SEARCH DICTIONARY lab.surface_dict\n    (TEMPLATE = pg_catalog.simple, STOPWORDS = hungarian);\nDROP TEXT SEARCH CONFIGURATION IF EXISTS lab.cfg_surface CASCADE;\nCREATE TEXT SEARCH CONFIGURATION lab.cfg_surface (COPY = pg_catalog.simple);\nALTER TEXT SEARCH CONFIGURATION lab.cfg_surface\n  ALTER MAPPING FOR asciiword, asciihword, hword_asciipart, word, hword,\n                    hword_part, numword, hword_numpart, numhword\n  WITH unaccent, lab.surface_dict;\n\n-- The stored vector: unaccented lemmas, unioned with unaccented surface forms.\nCREATE OR REPLACE FUNCTION lab.sv(t text) RETURNS tsvector\nLANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $$\n    SELECT to_tsvector(\'simple\',\n               lab.unaccent_i(\n                   array_to_string(\n                       tsvector_to_array(to_tsvector(\'lab.cfg_lemma\', t)), \' \')))\n        || to_tsvector(\'lab.cfg_surface\', t)\n$$;\n\n-- Per-term lexeme helpers.\nCREATE OR REPLACE FUNCTION lab.lemma_of(term text) RETURNS text[]\nLANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $$\n    SELECT array_agg(DISTINCT lab.unaccent_i(l))\n    FROM unnest(tsvector_to_array(to_tsvector(\'lab.cfg_lemma\', term))) l\n$$;\n\nCREATE OR REPLACE FUNCTION lab.surface_of(term text) RETURNS text[]\nLANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $$\n    SELECT tsvector_to_array(to_tsvector(\'lab.cfg_surface\', term))\n$$;\n\n-- ---------------------------------------------------------------------\n-- H  BASELINE - exactly corpus.search_query today: (lemma | surface), ANDed.\n-- ---------------------------------------------------------------------\nCREATE OR REPLACE FUNCTION lab.q_h(q text) RETURNS tsquery\nLANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT AS $$\nDECLARE term text; alts text[]; parts text[] := ARRAY[]::text[];\nBEGIN\n  FOREACH term IN ARRAY regexp_split_to_array(trim(q), \'[\\s]+\') LOOP\n    CONTINUE WHEN term = \'\';\n    SELECT array_agg(DISTINCT a ORDER BY a) INTO alts\n      FROM unnest(coalesce(lab.lemma_of(term),\'{}\') ||\n                  coalesce(lab.surface_of(term),\'{}\')) a;\n    CONTINUE WHEN alts IS NULL;\n    parts := parts || (\'(\' || array_to_string(\n               ARRAY(SELECT quote_literal(a) FROM unnest(alts) a), \' | \') || \')\');\n  END LOOP;\n  IF array_length(parts,1) IS NULL THEN RETURN \'\'::tsquery; END IF;\n  RETURN to_tsquery(\'simple\', array_to_string(parts, \' & \'));\nEND $$;\n\n-- ---------------------------------------------------------------------\n-- H1  MIN-LEMMA-LENGTH GUARD. Drop a lemma alternative shorter than 4 chars\n--     when it is not the surface form itself. Kills \'or\' (from folded\n--     "Orban", where snowball reads -ban as the inessive) without touching a\n--     normally-stemmed word.\n-- ---------------------------------------------------------------------\nCREATE OR REPLACE FUNCTION lab.q_h1(q text) RETURNS tsquery\nLANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT AS $$\nDECLARE term text; lem text[]; sur text[]; alts text[]; parts text[] := ARRAY[]::text[];\nBEGIN\n  FOREACH term IN ARRAY regexp_split_to_array(trim(q), \'[\\s]+\') LOOP\n    CONTINUE WHEN term = \'\';\n    lem := coalesce(lab.lemma_of(term), \'{}\');\n    sur := coalesce(lab.surface_of(term), \'{}\');\n    SELECT array_agg(DISTINCT a ORDER BY a) INTO alts FROM unnest(\n      ARRAY(SELECT l FROM unnest(lem) l WHERE length(l) >= 4 OR l = ANY(sur)) || sur) a;\n    CONTINUE WHEN alts IS NULL;\n    parts := parts || (\'(\' || array_to_string(\n               ARRAY(SELECT quote_literal(a) FROM unnest(alts) a), \' | \') || \')\');\n  END LOOP;\n  IF array_length(parts,1) IS NULL THEN RETURN \'\'::tsquery; END IF;\n  RETURN to_tsquery(\'simple\', array_to_string(parts, \' & \'));\nEND $$;\n\n-- ---------------------------------------------------------------------\n-- H2  ACCENT-FREE TERMS GO SURFACE-ONLY. If the user typed no ekezet, the\n--     stemmer was handed un-Hungarian input, so distrust its lemma entirely.\n--     Costs inflection tolerance for accent-free queries.\n-- ---------------------------------------------------------------------\nCREATE OR REPLACE FUNCTION lab.q_h2(q text) RETURNS tsquery\nLANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT AS $$\nDECLARE term text; alts text[]; parts text[] := ARRAY[]::text[];\nBEGIN\n  FOREACH term IN ARRAY regexp_split_to_array(trim(q), \'[\\s]+\') LOOP\n    CONTINUE WHEN term = \'\';\n    IF term = lab.unaccent_i(term) THEN\n      SELECT array_agg(DISTINCT a ORDER BY a) INTO alts\n        FROM unnest(coalesce(lab.surface_of(term),\'{}\')) a;\n    ELSE\n      SELECT array_agg(DISTINCT a ORDER BY a) INTO alts\n        FROM unnest(coalesce(lab.lemma_of(term),\'{}\') ||\n                    coalesce(lab.surface_of(term),\'{}\')) a;\n    END IF;\n    CONTINUE WHEN alts IS NULL;\n    parts := parts || (\'(\' || array_to_string(\n               ARRAY(SELECT quote_literal(a) FROM unnest(alts) a), \' | \') || \')\');\n  END LOOP;\n  IF array_length(parts,1) IS NULL THEN RETURN \'\'::tsquery; END IF;\n  RETURN to_tsquery(\'simple\', array_to_string(parts, \' & \'));\nEND $$;\n\n-- ---------------------------------------------------------------------\n-- H3  ACCENT-FREE TERMS GO SURFACE-PREFIX. Same distrust as H2, but recover\n--     the inflection tolerance the lemma was providing by matching the\n--     surface form as a PREFIX instead - \'orban\':* reaches orbannak, orbant,\n--     orbanrol. Hungarian is suffixing, so a prefix is the right shape.\n-- ---------------------------------------------------------------------\nCREATE OR REPLACE FUNCTION lab.q_h3(q text) RETURNS tsquery\nLANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT AS $$\nDECLARE term text; alts text[]; parts text[] := ARRAY[]::text[]; piece text;\nBEGIN\n  FOREACH term IN ARRAY regexp_split_to_array(trim(q), \'[\\s]+\') LOOP\n    CONTINUE WHEN term = \'\';\n    IF term = lab.unaccent_i(term) THEN\n      SELECT array_agg(DISTINCT a ORDER BY a) INTO alts\n        FROM unnest(coalesce(lab.surface_of(term),\'{}\')) a;\n      CONTINUE WHEN alts IS NULL;\n      piece := \'(\' || array_to_string(\n                 ARRAY(SELECT quote_literal(a) || \':*\' FROM unnest(alts) a), \' | \') || \')\';\n    ELSE\n      SELECT array_agg(DISTINCT a ORDER BY a) INTO alts\n        FROM unnest(coalesce(lab.lemma_of(term),\'{}\') ||\n                    coalesce(lab.surface_of(term),\'{}\')) a;\n      CONTINUE WHEN alts IS NULL;\n      piece := \'(\' || array_to_string(\n                 ARRAY(SELECT quote_literal(a) FROM unnest(alts) a), \' | \') || \')\';\n    END IF;\n    parts := parts || piece;\n  END LOOP;\n  IF array_length(parts,1) IS NULL THEN RETURN \'\'::tsquery; END IF;\n  RETURN to_tsquery(\'simple\', array_to_string(parts, \' & \'));\nEND $$;\n',
"H45": r"""
-- H4  Drop ONLY lemmas of 1-2 characters. 'Orban' (accent-free) lemmatises to
--     'or'; 'Orban' with its accent lemmatises to 'orb'. The 3-char lemma is
--     legitimate and load-bearing - H1's threshold of 4 threw it away and took
--     Orbannak from 99% recall to 1%. Two characters is the surgical cut.
CREATE OR REPLACE FUNCTION lab.q_h4(q text) RETURNS tsquery
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT AS $$
DECLARE term text; lem text[]; sur text[]; alts text[]; parts text[] := ARRAY[]::text[];
BEGIN
  FOREACH term IN ARRAY regexp_split_to_array(trim(q), '[\s]+') LOOP
    CONTINUE WHEN term = '';
    lem := coalesce(lab.lemma_of(term), '{}');
    sur := coalesce(lab.surface_of(term), '{}');
    SELECT array_agg(DISTINCT a ORDER BY a) INTO alts FROM unnest(
      ARRAY(SELECT l FROM unnest(lem) l WHERE length(l) > 2 OR l = ANY(sur)) || sur) a;
    CONTINUE WHEN alts IS NULL;
    parts := parts || ('(' || array_to_string(
               ARRAY(SELECT quote_literal(a) FROM unnest(alts) a), ' | ') || ')');
  END LOOP;
  IF array_length(parts,1) IS NULL THEN RETURN ''::tsquery; END IF;
  RETURN to_tsquery('simple', array_to_string(parts, ' & '));
END $$;

-- H5  H4, but the guard applies ONLY to accent-free terms - the only spelling
--     where the stemmer was handed input it cannot read as Hungarian. An
--     accented term keeps every lemma it produces, however short.
CREATE OR REPLACE FUNCTION lab.q_h5(q text) RETURNS tsquery
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT AS $$
DECLARE term text; lem text[]; sur text[]; alts text[]; parts text[] := ARRAY[]::text[];
BEGIN
  FOREACH term IN ARRAY regexp_split_to_array(trim(q), '[\s]+') LOOP
    CONTINUE WHEN term = '';
    lem := coalesce(lab.lemma_of(term), '{}');
    sur := coalesce(lab.surface_of(term), '{}');
    IF term = lab.unaccent_i(term) THEN
      SELECT array_agg(DISTINCT a ORDER BY a) INTO alts FROM unnest(
        ARRAY(SELECT l FROM unnest(lem) l WHERE length(l) > 2 OR l = ANY(sur)) || sur) a;
    ELSE
      SELECT array_agg(DISTINCT a ORDER BY a) INTO alts FROM unnest(lem || sur) a;
    END IF;
    CONTINUE WHEN alts IS NULL;
    parts := parts || ('(' || array_to_string(
               ARRAY(SELECT quote_literal(a) FROM unnest(alts) a), ' | ') || ')');
  END LOOP;
  IF array_length(parts,1) IS NULL THEN RETURN ''::tsquery; END IF;
  RETURN to_tsquery('simple', array_to_string(parts, ' & '));
END $$;
""",
}

#: The H variants differ only in the QUERY function, so they share one vector.
VECTOR_SQL = {
"A": "ALTER TABLE lab.doc ADD COLUMN v_a tsvector;"
     " UPDATE lab.doc SET v_a = to_tsvector('lab.cfg_a', full_text);"
     " CREATE INDEX ON lab.doc USING gin(v_a);",
"B": "ALTER TABLE lab.doc ADD COLUMN v_b tsvector;"
     " UPDATE lab.doc SET v_b = to_tsvector('lab.cfg_b', full_text);"
     " CREATE INDEX ON lab.doc USING gin(v_b);",
"C": "ALTER TABLE lab.doc ADD COLUMN v_c tsvector;"
     " UPDATE lab.doc SET v_c = to_tsvector('lab.cfg_c', full_text);"
     " CREATE INDEX ON lab.doc USING gin(v_c);",
"D": "ALTER TABLE lab.doc ADD COLUMN v_d tsvector;"
     " UPDATE lab.doc SET v_d = to_tsvector('lab.cfg_d', full_text);"
     " CREATE INDEX ON lab.doc USING gin(v_d);",
"F": "ALTER TABLE lab.doc ADD COLUMN v_f tsvector;"
     " UPDATE lab.doc SET v_f = to_tsvector('simple',"
     " lab.unaccent_i(lab.lemma_text(full_text)));"
     " CREATE INDEX ON lab.doc USING gin(v_f);",
"H": "ALTER TABLE lab.doc ADD COLUMN v_h tsvector;"
     " UPDATE lab.doc SET v_h = lab.sv(full_text);"
     " CREATE INDEX ON lab.doc USING gin(v_h);",
}

#: Which setup chunk each scored candidate depends on.
DEPENDS = {"A": ["A"], "B": ["B"], "C": ["C"], "D": ["D"], "F": ["C", "F"],
           "H": ["H"], "H1": ["H"], "H2": ["H"], "H3": ["H"],
           "H4": ["H", "H45"], "H5": ["H", "H45"]}



#: (vector column, how to build the query) per candidate.
MATCH = {
    "A": ("v_a", "websearch_to_tsquery('lab.cfg_a', %(q)s)"),
    "B": ("v_b", "websearch_to_tsquery('lab.cfg_b', %(q)s)"),
    "C": ("v_c", "websearch_to_tsquery('lab.cfg_c', %(q)s)"),
    # D is only interesting as a PREFIX query - that is its whole proposition.
    "D": ("v_d", "to_tsquery('simple', lab.unaccent_i(%(q)s) || ':*')"),
    "F": ("v_f", "to_tsquery('simple', "
                 "replace(lab.unaccent_i(lab.lemma_text(%(q)s)), ' ', ' & '))"),
    # One vector, four query behaviours - the point of the comparison.
    "H":  ("v_h", "lab.q_h(%(q)s)"),
    "H1": ("v_h", "lab.q_h1(%(q)s)"),
    "H2": ("v_h", "lab.q_h2(%(q)s)"),
    "H3": ("v_h", "lab.q_h3(%(q)s)"),
    "H4": ("v_h", "lab.q_h4(%(q)s)"),
    "H5": ("v_h", "lab.q_h5(%(q)s)"),
}


def load_corpus(dev_cur, lab_cur) -> int:
    dev_cur.execute("""
        SELECT a.id, a.outlet, a.title, a.tags,
               concat_ws(' ', a.title, a.subtitle, a.description,
                         array_to_string(a.tags, ' ')) AS meta,
               coalesce(string_agg(b.block_text, ' '), '') AS body
        FROM corpus.article a
        LEFT JOIN corpus.content_block b
               ON b.article_id = a.id AND b.extraction_id = a.current_extraction_id
        GROUP BY a.id
        ORDER BY a.id
    """)
    rows = dev_cur.fetchall()
    for row in rows:
        full = (row["meta"] or "") + " " + (row["body"] or "")
        lab_cur.execute("""INSERT INTO lab.doc
                             (article_id, outlet, title, tags, meta, body, full_text)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (row["id"], row["outlet"], row["title"], row["tags"] or [],
                         row["meta"] or "", row["body"] or "", full))
    return len(rows)


def score(lab_cur, truth: dict[str, set[int]], keys=None) -> dict:
    out = {}
    for key, (column, query_sql) in MATCH.items():
        if keys is not None and key not in keys:
            continue
        per_query = []
        for query, kind in QUERIES:
            started = time.perf_counter()
            try:
                lab_cur.execute(
                    f"SELECT article_id FROM lab.doc WHERE {column} @@ {query_sql}",
                    {"q": query})
                returned = {r[0] for r in lab_cur.fetchall()}
                error = None
            except psycopg2.Error as exc:
                lab_cur.execute("ROLLBACK")
                returned, error = set(), str(exc).splitlines()[0]
            latency = (time.perf_counter() - started) * 1000
            want = truth[query]
            hit = returned & want
            per_query.append({
                "query": query, "kind": kind,
                "returned": len(returned), "truth": len(want), "correct": len(hit),
                "recall": (len(hit) / len(want)) if want else None,
                "precision": (len(hit) / len(returned)) if returned else None,
                "latency_ms": round(latency, 2), "error": error,
            })
        recalls = [q["recall"] for q in per_query if q["recall"] is not None]
        precisions = [q["precision"] for q in per_query if q["precision"] is not None]
        out[key] = {
            "queries": per_query,
            "mean_recall": sum(recalls) / len(recalls) if recalls else 0.0,
            "mean_precision": sum(precisions) / len(precisions) if precisions else 0.0,
        }
    return out


def trgm_score(lab_cur, truth: dict[str, set[int]], threshold: float) -> dict:
    """pg_trgm word_similarity: no dictionary, symmetric, tolerant of typos."""
    per_query = []
    lab_cur.execute("SET pg_trgm.word_similarity_threshold = %s", (threshold,))
    for query, kind in QUERIES:
        started = time.perf_counter()
        lab_cur.execute(
            "SELECT article_id FROM lab.doc WHERE %(q)s <%% full_text", {"q": query})
        returned = {r[0] for r in lab_cur.fetchall()}
        latency = (time.perf_counter() - started) * 1000
        want = truth[query]
        hit = returned & want
        per_query.append({
            "query": query, "kind": kind,
            "returned": len(returned), "truth": len(want), "correct": len(hit),
            "recall": (len(hit) / len(want)) if want else None,
            "precision": (len(hit) / len(returned)) if returned else None,
            "latency_ms": round(latency, 2), "error": None,
        })
    recalls = [q["recall"] for q in per_query if q["recall"] is not None]
    precisions = [q["precision"] for q in per_query if q["precision"] is not None]
    return {"queries": per_query,
            "mean_recall": sum(recalls) / len(recalls) if recalls else 0.0,
            "mean_precision": sum(precisions) / len(precisions) if precisions else 0.0,
            "threshold": threshold}


def build_truth(docs: list[tuple[int, str]]) -> dict[str, set[int]]:
    """An article contains a query if every term's ROOT is in its text.

    The root is approximated by the longest leading substring shared with the
    query - for a purely suffixing language that is a fair yardstick, and it is
    computed with no reference to any Postgres configuration.
    """
    truth = {}
    for query, _kind in QUERIES:
        terms = [fold(t) for t in re.split(r"[\s\-]+", query) if t]
        want = set()
        for article_id, text in docs:
            folded = fold(text)
            # A term matches if the article carries a word starting with the
            # term's first 5 characters AND sharing at least that much - which
            # catches both directions of inflection without a dictionary.
            if all(any(w.startswith(t[:5]) and (w.startswith(t) or t.startswith(w))
                       for w in re.findall(r"[\w]+", folded))
                   for t in terms):
                want.add(article_id)
        truth[query] = want
    return truth


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev-dsn", default=DEV_DSN)
    parser.add_argument("--lab-dsn", default=LAB_DSN)
    parser.add_argument("-o", "--out", type=Path, required=True)
    args = parser.parse_args(argv[1:])

    lab = psycopg2.connect(args.lab_dsn)
    lab.autocommit = True
    lab_cur = lab.cursor()
    print("== building candidate configurations")
    lab_cur.execute(BASE_SQL)
    available = set()
    for key, sql in CHUNKS.items():
        try:
            lab_cur.execute(sql)
            available.add(key)
        except psycopg2.Error as exc:
            print(f"   group {key}: UNAVAILABLE - {str(exc).splitlines()[0]}")

    dev = psycopg2.connect(args.dev_dsn)          # read-only use
    dev_cur = dev.cursor(cursor_factory=psycopg2.extras.DictCursor)
    n = load_corpus(dev_cur, lab_cur)
    dev.close()
    print(f"== loaded {n} articles from the development database (read-only)")

    print("== building vectors and indexes")
    started = time.perf_counter()
    for key in list(available):
        if key not in VECTOR_SQL:
            continue          # a chunk that only adds query functions (H45)
        try:
            lab_cur.execute(VECTOR_SQL[key])
        except psycopg2.Error as exc:
            print(f"   vector {key}: FAILED - {str(exc).splitlines()[0]}")
            available.discard(key)
    lab_cur.execute("CREATE INDEX ON lab.doc USING gin(full_text gin_trgm_ops)")
    build_s = time.perf_counter() - started
    scored = [k for k in MATCH if all(d in available for d in DEPENDS[k])]
    print(f"== scoring {len(scored)} candidate(s): {', '.join(scored)}")

    lab_cur.execute("SELECT article_id, full_text FROM lab.doc")
    docs = lab_cur.fetchall()
    truth = build_truth(docs)

    results = score(lab_cur, truth, keys=scored)
    results["E"] = trgm_score(lab_cur, truth, 0.6)

    # The lexeme chain for the worked examples, per configuration.
    lexemes = {}
    for probe in PROBES:
        row = {}
        for key, cfg in (("A", "lab.cfg_a"), ("B", "lab.cfg_b"),
                         ("C", "lab.cfg_c"), ("D", "lab.cfg_d")):
            if key not in available:
                continue
            lab_cur.execute(f"SELECT to_tsvector('{cfg}', %s)::text", (probe,))
            raw = lab_cur.fetchone()[0]
            row[key] = " ".join(re.findall(r"'([^']+)'", raw)) or "(dropped)"
        if "F" in available:
            lab_cur.execute("SELECT lab.unaccent_i(lab.lemma_text(%s))", (probe,))
            row["F"] = lab_cur.fetchone()[0] or "(dropped)"
        if "H" in available:
            # The stored vector is shared, so show what each QUERY becomes -
            # that is where the H variants differ and where the defect lives.
            for key, fn in (("H", "lab.q_h"), ("H1", "lab.q_h1"),
                            ("H2", "lab.q_h2"), ("H3", "lab.q_h3"),
                            ("H4", "lab.q_h4"), ("H5", "lab.q_h5")):
                lab_cur.execute(f"SELECT {fn}(%s)::text", (probe,))
                row[key] = lab_cur.fetchone()[0] or "(dropped)"
        lexemes[probe] = row

    # Index sizes, which is part of what each option costs.
    sizes = {}
    for key, (column, _q) in MATCH.items():
        lab_cur.execute("""SELECT pg_size_pretty(sum(pg_relation_size(i.indexrelid)))
                           FROM pg_index i JOIN pg_attribute a
                             ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                           WHERE i.indrelid = 'lab.doc'::regclass AND a.attname = %s""",
                        (column,))
        sizes[key] = lab_cur.fetchone()[0]

    lab_cur.execute("SELECT count(*) FROM lab.doc")
    corpus_size = lab_cur.fetchone()[0]
    lab.close()

    report = {
        "corpus_size": corpus_size,
        "candidates": [{"key": k, "label": lab_, "gist": g} for k, lab_, g in CANDIDATES],
        "results": results, "lexemes": lexemes, "index_sizes": sizes,
        "probes": PROBES, "build_seconds": round(build_s, 2),
        "truth": {q: sorted(v) for q, v in truth.items()},
    }
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{'cand':<5}{'mean recall':>13}{'mean precision':>16}{'index':>10}")
    for key in ["A", "B", "C", "D", "F", "E",
                "H", "H1", "H2", "H3", "H4", "H5"]:
        r = results.get(key)
        if r is None:
            continue
        print(f"{key:<5}{r['mean_recall']*100:>12.1f}%{r['mean_precision']*100:>15.1f}%"
              f"{sizes.get(key,'-'):>10}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
