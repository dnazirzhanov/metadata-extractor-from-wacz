#!/usr/bin/env python3
"""Measure whether a journalist can search naturally AND get the evidence.

Two questions, not one:

    RECALL     does an inflected or accent-free query reach the same articles
               as the base form the writer actually used?
    EVIDENCE   for every article it returns, can the exact passage that caused
               the match be located, stored, resolved and highlighted?

The second is the one that matters and the one nothing measured before. A search
layer that finds the right article and cannot point at the sentence is not a
citation tool.

METHOD

Query set: content words taken from the corpus itself, each expanded into four
spellings a real user might type -

    exact                 kormány
    unaccented            kormany
    inflected             kormánynak      (crude vowel harmony, see SUFFIXES)
    inflected+unaccented  kormanynak

All four share ONE ground truth - the articles containing the base word - so the
question "does an inflection reach what the base form reaches" is asked
directly. Truth is an accent-folded prefix match computed in Python, deliberately
independent of anything Postgres does, so no configuration can score well by
agreeing with itself. It is a crude yardstick and it is meant to be: it cannot
be gamed by the thing under test.

Failures are RECORDED, never repaired. The report is the deliverable.

Usage:
    scripts/search_eval.py --root DIR [--dsn DSN] [--words N] [-o report.json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

import psycopg2
import psycopg2.extras
from lxml import html as lxml_html

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import search as S                                              # noqa: E402
from causalia_extractor.normalize import normalize_text         # noqa: E402
from causalia_extractor.selectors import find_selector, SelectorError  # noqa: E402

BACK_VOWELS, FRONT_VOWELS = set("aáoóuú"), set("eéiíöőüű")

#: (back-vowel form, front-vowel form). Crude but enough to generate the shapes
#: a Hungarian speaker actually types; correctness of the suffix is irrelevant
#: because the QUERY is the probe, not the answer.
SUFFIXES = [("nak", "nek"), ("ban", "ben"), ("ról", "ről"), ("val", "vel")]

WORD_RE = re.compile(r"[^\W\d_]{5,}", re.UNICODE)


def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (text or "").lower())
                   if not unicodedata.combining(c))


def inflect(word: str, which: int) -> str:
    """Attach a case suffix, choosing the harmonic variant."""
    back, front = SUFFIXES[which % len(SUFFIXES)]
    vowels = [c for c in word.lower() if c in BACK_VOWELS or c in FRONT_VOWELS]
    harmonic = back if (vowels and vowels[-1] in BACK_VOWELS) else front
    return word + harmonic


# ---------------------------------------------------------------------
# Corpus, ground truth, query set
# ---------------------------------------------------------------------

def load_corpus(cur) -> dict[int, dict]:
    cur.execute("""
        SELECT a.id, a.outlet, a.title,
               -- Must cover exactly the fields the search vectors cover, or
               -- the yardstick reports honest matches as false positives:
               -- authors are weight C in article.search_tsv, and leaving them
               -- out made `magyar` -> magyarnemzet.hu and `Péter` -> feol.hu
               -- look like precision failures when both are real author hits.
               concat_ws(' ', a.title, a.subtitle, a.description,
                         corpus.text_array_to_string(a.authors),
                         corpus.text_array_to_string(a.tags),
                         string_agg(b.block_text, ' ')) AS full_text
          FROM corpus.article a
          LEFT JOIN corpus.content_block b
                 ON b.article_id = a.id
                AND b.extraction_id = a.current_extraction_id
         GROUP BY a.id
    """)
    return {r["id"]: {"outlet": r["outlet"], "title": r["title"],
                      "text": r["full_text"] or "",
                      "folded_words": set(WORD_RE.findall(fold(r["full_text"] or "")))}
            for r in cur.fetchall()}


def pick_words(cur, corpus: dict[int, dict], want: int) -> list[str]:
    """Content words that appear in at least two articles, most frequent first.

    Two articles is the minimum that makes recall meaningful: a word in exactly
    one article scores 1/1 for any configuration that finds anything at all.

    Stopwords are excluded, and by the system's OWN definition rather than a
    list kept here: a word whose corpus.search_vector is empty is not indexed,
    so it is not searchable and scoring it measures nothing. Without this the
    probe set fills with `szerint`, `pedig`, `akkor` - the most frequent words
    in any Hungarian text and precisely the ones both sides of the vector drop
    on purpose - and every one of them scores 0% recall for being correct.
    """
    spread: Counter = Counter()
    surface: dict[str, Counter] = defaultdict(Counter)
    for doc in corpus.values():
        seen = set()
        for match in WORD_RE.finditer(doc["text"]):
            word = match.group(0)
            folded = fold(word)
            surface[folded][word] += 1
            if folded not in seen:
                seen.add(folded)
                spread[folded] += 1
    chosen: list[str] = []
    for folded, n_articles in sorted(spread.items(), key=lambda kv: (-kv[1], kv[0])):
        if n_articles < 2:
            continue
        # the commonest spelling, so the probe is a word the corpus really uses
        word = surface[folded].most_common(1)[0][0]
        cur.execute("SELECT corpus.search_vector(%s) = ''::tsvector", (word,))
        if cur.fetchone()[0]:
            continue                      # stopword: indexed nowhere, so unsearchable
        chosen.append(word)
        if len(chosen) >= want:
            break
    return chosen


#: How many characters a corpus word may differ from the probe and still count
#: as "the same word". Hungarian case endings are 2-4 characters (-ig, -ban,
#: -ról, -ként), so 4 admits inflection. Without a cap the yardstick counts
#: `magyarország` as containing `magyar`, which measures nothing but the fact
#: that full-text search is not substring search - by design. The cap still
#: admits short derivations (`kormány`/`kormányzat`) that no stemmer unifies, so
#: recall measured this way is a LOWER bound on lexical recall.
INFLECTION_SLACK = 4


def truth_for(corpus: dict[int, dict], base: str) -> set[int]:
    """Articles containing the base word or an inflection of it.

    Accent-folded, prefix-agreeing, and bounded by INFLECTION_SLACK so that
    compounding is excluded. Computed in Python with no reference to any
    Postgres configuration, so nothing under test can agree with itself.
    """
    t = fold(base)
    return {aid for aid, doc in corpus.items()
            if any((w.startswith(t) or t.startswith(w))
                   and abs(len(w) - len(t)) <= INFLECTION_SLACK
                   for w in doc["folded_words"])}


def build_queries(words: list[str]) -> list[dict]:
    out = []
    for i, base in enumerate(words):
        inflected = inflect(base, i)
        out.append({"q": base, "base": base, "kind": "exact"})
        if fold(base) != base:
            out.append({"q": fold(base), "base": base, "kind": "unaccented"})
        out.append({"q": inflected, "base": base, "kind": "inflected"})
        if fold(inflected) != inflected:
            out.append({"q": fold(inflected), "base": base,
                        "kind": "inflected+unaccented"})
    return out


# ---------------------------------------------------------------------
# The citation chain, per (query, article) hit
# ---------------------------------------------------------------------

class Citations:
    """Resolve the passage that caused a match, and say why when it cannot."""

    def __init__(self, cur, root: Path):
        self.cur = cur
        self.root = root
        self.trees: dict[int, object] = {}
        self.paths: dict[int, Path] = {}

    def tree_for(self, article_id: int):
        if article_id in self.trees:
            return self.trees[article_id]
        self.cur.execute("""
            SELECT f.file_path FROM corpus.article_artifact f
              JOIN corpus.article a ON a.id = f.article_id
             WHERE f.article_id = %s AND f.extraction_id = a.current_extraction_id
               AND f.kind = 'readability_html'
        """, (article_id,))
        row = self.cur.fetchone()
        tree = None
        if row:
            path = self.root / row[0]
            self.paths[article_id] = path
            if path.is_file():
                tree = lxml_html.fromstring(path.read_bytes())
        self.trees[article_id] = tree
        return tree

    def resolve(self, article_id: int, query: str) -> dict:
        """Return {ok, reason, block_type, token, xpath}."""
        self.cur.execute("""
            SELECT b.id, b.block_type, b.xpath, b.block_text
              FROM corpus.content_block b
              JOIN corpus.article a ON a.id = b.article_id
             WHERE b.article_id = %s
               AND b.extraction_id = a.current_extraction_id
               AND b.text_tsv @@ corpus.search_query(%s)
             ORDER BY ts_rank(b.text_tsv, corpus.search_query(%s)) DESC,
                      b.block_index
             LIMIT 5
        """, (article_id, query, query))
        blocks = self.cur.fetchall()
        if not blocks:
            return {"ok": False, "reason": "metadata_only_no_block"}

        tree = self.tree_for(article_id)
        if tree is None:
            return {"ok": False, "reason": "readability_html_missing"}

        last = "no_block_resolved"
        for block in blocks:
            found = tree.xpath(block["xpath"])
            if len(found) != 1:
                last = "xpath_selects_%d" % len(found)
                continue
            element = found[0]
            element_text = normalize_text(element)
            if element_text != (block["block_text"] or ""):
                last = "block_text_disagrees_with_dom"
                continue
            # Which token of this block did the search rules match?
            self.cur.execute("""
                SELECT t.tok
                  FROM unnest(regexp_split_to_array(%(text)s, '[^[:alnum:]]+'))
                       WITH ORDINALITY AS t(tok, ord)
                 WHERE t.tok <> ''
                   AND corpus.search_vector(t.tok) @@ corpus.search_query(%(q)s)
                 ORDER BY t.ord LIMIT 1
            """, {"text": element_text, "q": query})
            row = self.cur.fetchone()
            if not row:
                last = "no_token_matches_in_block"
                continue
            token = row[0]
            try:
                selector = find_selector(tree, element, token)
            except SelectorError as exc:
                last = f"selector_error:{type(exc).__name__}"
                continue
            if selector is None:
                last = "token_not_locatable"
                continue
            # Resolve independently: xpath -> normalise -> slice -> compare.
            target = tree.xpath(selector.value)
            if len(target) != 1:
                last = "stored_xpath_unresolvable"
                continue
            text = normalize_text(target[0])
            if text[selector.start:selector.end] != selector.exact:
                last = "quote_mismatch"
                continue
            return {"ok": True, "reason": "resolved", "token": token,
                    "block_type": block["block_type"], "xpath": selector.value}
        return {"ok": False, "reason": last}


# ---------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True,
                    help="extractor output root that article_artifact paths are relative to")
    ap.add_argument("--dsn", default=os.environ.get("CX_EVAL_DSN"))
    ap.add_argument("--words", type=int, default=60)
    ap.add_argument("--max-hits", type=int, default=25,
                    help="cap citation attempts per query")
    ap.add_argument("-o", "--out", type=Path)
    args = ap.parse_args(argv[1:])

    conn = S.connect(args.dsn)
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    corpus = load_corpus(cur)
    words = pick_words(cur, corpus, args.words)
    queries = build_queries(words)
    citations = Citations(cur, args.root)
    print(f"== {len(corpus)} articles, {len(words)} base words, "
          f"{len(queries)} query variants")

    per_kind = defaultdict(lambda: {"n": 0, "recall": [], "precision": [],
                                    "agreement": [], "hits": 0, "cited": 0,
                                    "uncitable_metadata": 0})
    base_results: dict[str, set[int]] = {}
    failures: Counter = Counter()
    failure_examples: dict[str, list] = defaultdict(list)
    fp_examples: list = []
    by_block_type: Counter = Counter()
    started = time.perf_counter()

    for item in queries:
        q, base, kind = item["q"], item["base"], item["kind"]
        want = truth_for(corpus, base)
        cur.execute("""
            SELECT a.id FROM corpus.article a
             WHERE a.search_tsv @@ corpus.search_query(%(q)s)
                OR EXISTS (SELECT 1 FROM corpus.content_block b
                            WHERE b.article_id = a.id
                              AND b.extraction_id = a.current_extraction_id
                              AND b.text_tsv @@ corpus.search_query(%(q)s))
        """, {"q": q})
        got = {r[0] for r in cur.fetchall()}
        hit = got & want
        # The journalist-facing question, and the only fully objective one:
        # does typing it differently reach what the base spelling reaches?
        if kind == "exact":
            base_results[base] = got
        elif base in base_results and base_results[base]:
            bucket_base = base_results[base]
            per_kind[kind]["agreement"].append(
                len(got & bucket_base) / len(bucket_base))
        bucket = per_kind[kind]
        bucket["n"] += 1
        if want:
            bucket["recall"].append(len(hit) / len(want))
        if got:
            bucket["precision"].append(len(hit) / len(got))

        for aid in sorted(got - want)[:3]:
            fp_examples.append({"query": q, "base": base, "kind": kind,
                                "article": aid,
                                "outlet": corpus[aid]["outlet"],
                                "title": (corpus[aid]["title"] or "")[:60]})

        # EVIDENCE: only over true positives - pointing at a false positive is
        # a different defect and is counted separately above.
        for aid in sorted(hit)[:args.max_hits]:
            bucket["hits"] += 1
            outcome = citations.resolve(aid, q)
            if outcome["ok"]:
                bucket["cited"] += 1
                by_block_type[outcome["block_type"]] += 1
            elif outcome["reason"] == "metadata_only_no_block":
                # Not a defect of the citation chain: the match is real but it
                # is in the title, tags or description, and the schema has no
                # citable unit for metadata. Counted apart from failures.
                bucket["uncitable_metadata"] += 1
                failures[outcome["reason"]] += 1
            else:
                failures[outcome["reason"]] += 1
                if len(failure_examples[outcome["reason"]]) < 4:
                    failure_examples[outcome["reason"]].append(
                        {"query": q, "base": base, "kind": kind, "article": aid,
                         "outlet": corpus[aid]["outlet"]})

    elapsed = time.perf_counter() - started

    def mean(xs): return (sum(xs) / len(xs)) if xs else 0.0

    print(f"\n{'variant':<24}{'n':>5}{'recall':>9}{'prec':>8}"
          f"{'agrees w/base':>15}{'hits':>7}{'evidence':>10}{'meta-only':>11}")
    total_hits = total_cited = total_meta = 0
    for kind in ("exact", "unaccented", "inflected", "inflected+unaccented"):
        b = per_kind.get(kind)
        if not b:
            continue
        total_hits += b["hits"]; total_cited += b["cited"]
        total_meta += b["uncitable_metadata"]
        rate = (b["cited"] / b["hits"] * 100) if b["hits"] else 0.0
        agree = (f"{mean(b['agreement'])*100:>14.1f}%" if b["agreement"]
                 else f"{'(baseline)':>15}")
        print(f"{kind:<24}{b['n']:>5}{mean(b['recall'])*100:>8.1f}%"
              f"{mean(b['precision'])*100:>7.1f}%{agree}"
              f"{b['hits']:>7}{rate:>9.1f}%{b['uncitable_metadata']:>11}")
    joint = (total_cited / total_hits * 100) if total_hits else 0.0
    citable = total_hits - total_meta
    strict = (total_cited / citable * 100) if citable else 0.0
    print(f"\n  {total_cited}/{total_hits} of all true-positive hits produced a "
          f"resolvable citation ({joint:.1f}%)")
    print(f"  {total_cited}/{citable} of hits that matched a CONTENT BLOCK did "
          f"({strict:.1f}%); the other {total_meta} matched metadata only, "
          f"which has no citable unit")
    print(f"  citations landed on: "
          f"{', '.join(f'{k} {v}' for k, v in by_block_type.most_common())}")
    print(f"  {elapsed:.1f}s")

    if failures:
        print("\n  evidence failures, recorded not repaired:")
        for reason, n in failures.most_common():
            print(f"    {n:>5}  {reason}")
            for ex in failure_examples[reason][:2]:
                print(f"           e.g. {ex['query']!r} ({ex['kind']}) "
                      f"-> article {ex['article']} {ex['outlet']}")

    if args.out:
        args.out.write_text(json.dumps({
            "articles": len(corpus), "base_words": words,
            "per_kind": {k: {kk: (vv if not isinstance(vv, list) else
                                  {"mean": mean(vv), "n": len(vv)})
                             for kk, vv in v.items()} for k, v in per_kind.items()},
            "joint_evidence_rate": joint,
            "failures": dict(failures),
            "failure_examples": {k: v for k, v in failure_examples.items()},
            "false_positive_examples": fp_examples[:60],
            "citations_by_block_type": dict(by_block_type),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
