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

Usage:  scripts/stemming_lab.py [--dev-dsn ...] [--lab-dsn ...] [-o out.json]
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

SETUP = """
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
DROP SCHEMA IF EXISTS lab CASCADE;
CREATE SCHEMA lab;

-- A: exactly what corpus.hungarian_ci is today.
DROP TEXT SEARCH CONFIGURATION IF EXISTS lab.cfg_a;
CREATE TEXT SEARCH CONFIGURATION lab.cfg_a (COPY = hungarian);
ALTER TEXT SEARCH CONFIGURATION lab.cfg_a
  ALTER MAPPING FOR hword, hword_part, word WITH unaccent, hungarian_stem;

-- B: the same stemmer with nothing in front of it.
DROP TEXT SEARCH CONFIGURATION IF EXISTS lab.cfg_b;
CREATE TEXT SEARCH CONFIGURATION lab.cfg_b (COPY = hungarian);

-- C: Hunspell first, snowball for whatever the dictionary does not know.
DROP TEXT SEARCH CONFIGURATION IF EXISTS lab.cfg_c;
CREATE TEXT SEARCH CONFIGURATION lab.cfg_c (COPY = hungarian);
ALTER TEXT SEARCH CONFIGURATION lab.cfg_c
  ALTER MAPPING FOR hword, hword_part, word WITH hunspell_hu, hungarian_stem;

-- D: no stemming at all, accent-folded. Whole words as lexemes.
DROP TEXT SEARCH CONFIGURATION IF EXISTS lab.cfg_d;
CREATE TEXT SEARCH CONFIGURATION lab.cfg_d (COPY = simple);
ALTER TEXT SEARCH CONFIGURATION lab.cfg_d
  ALTER MAPPING FOR hword, hword_part, word WITH unaccent, simple;

-- F needs to unaccent the LEMMAS rather than the input, which no dictionary
-- chain can express: lemmatise with C, flatten to text, then index that.
CREATE OR REPLACE FUNCTION lab.lemma_text(t text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $$
  SELECT array_to_string(tsvector_to_array(to_tsvector('lab.cfg_c', t)), ' ')
$$;
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

VECTORS = """
ALTER TABLE lab.doc
  ADD COLUMN v_a tsvector, ADD COLUMN v_b tsvector, ADD COLUMN v_c tsvector,
  ADD COLUMN v_d tsvector, ADD COLUMN v_f tsvector;
UPDATE lab.doc SET
  v_a = to_tsvector('lab.cfg_a', full_text),
  v_b = to_tsvector('lab.cfg_b', full_text),
  v_c = to_tsvector('lab.cfg_c', full_text),
  v_d = to_tsvector('lab.cfg_d', full_text),
  v_f = to_tsvector('simple', lab.unaccent_i(lab.lemma_text(full_text)));
CREATE INDEX ON lab.doc USING gin(v_a);
CREATE INDEX ON lab.doc USING gin(v_b);
CREATE INDEX ON lab.doc USING gin(v_c);
CREATE INDEX ON lab.doc USING gin(v_d);
CREATE INDEX ON lab.doc USING gin(v_f);
CREATE INDEX ON lab.doc USING gin(full_text gin_trgm_ops);
"""

#: (vector column, how to build the query) per candidate.
MATCH = {
    "A": ("v_a", "websearch_to_tsquery('lab.cfg_a', %(q)s)"),
    "B": ("v_b", "websearch_to_tsquery('lab.cfg_b', %(q)s)"),
    "C": ("v_c", "websearch_to_tsquery('lab.cfg_c', %(q)s)"),
    # D is only interesting as a PREFIX query - that is its whole proposition.
    "D": ("v_d", "to_tsquery('simple', lab.unaccent_i(%(q)s) || ':*')"),
    "F": ("v_f", "to_tsquery('simple', "
                 "replace(lab.unaccent_i(lab.lemma_text(%(q)s)), ' ', ' & '))"),
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


def score(lab_cur, truth: dict[str, set[int]]) -> dict:
    out = {}
    for key, (column, query_sql) in MATCH.items():
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
    lab_cur.execute(SETUP)

    dev = psycopg2.connect(args.dev_dsn)          # read-only use
    dev_cur = dev.cursor(cursor_factory=psycopg2.extras.DictCursor)
    n = load_corpus(dev_cur, lab_cur)
    dev.close()
    print(f"== loaded {n} articles from the development database (read-only)")

    print("== building vectors and indexes")
    started = time.perf_counter()
    lab_cur.execute(VECTORS)
    build_s = time.perf_counter() - started

    lab_cur.execute("SELECT article_id, full_text FROM lab.doc")
    docs = lab_cur.fetchall()
    truth = build_truth(docs)

    print("== scoring")
    results = score(lab_cur, truth)
    results["E"] = trgm_score(lab_cur, truth, 0.6)

    # The lexeme chain for the worked examples, per configuration.
    lexemes = {}
    for probe in PROBES:
        row = {}
        for key, cfg in (("A", "lab.cfg_a"), ("B", "lab.cfg_b"),
                         ("C", "lab.cfg_c"), ("D", "lab.cfg_d")):
            lab_cur.execute(f"SELECT to_tsvector('{cfg}', %s)::text", (probe,))
            raw = lab_cur.fetchone()[0]
            row[key] = " ".join(re.findall(r"'([^']+)'", raw)) or "(dropped)"
        lab_cur.execute("SELECT lab.unaccent_i(lab.lemma_text(%s))", (probe,))
        row["F"] = lab_cur.fetchone()[0] or "(dropped)"
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
    for key in ["A", "B", "C", "D", "F", "E"]:
        r = results[key]
        print(f"{key:<5}{r['mean_recall']*100:>12.1f}%{r['mean_precision']*100:>15.1f}%"
              f"{sizes.get(key,'-'):>10}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
