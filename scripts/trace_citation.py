#!/usr/bin/env python3
"""Walk one query from full-text search all the way to a highlighted passage.

    search  ->  article  ->  content_block  ->  passage_reference
            ->  XPath + offsets + quote  ->  readability.html  ->  highlight

This is the join between the two halves of the system: DISCOVERY (Postgres
full-text search) and EVIDENCE (a selector that resolves to exact characters in
the canonical document). Each half is tested on its own; nothing tested that
they meet.

WHAT IS TRUSTED WHERE
    The selector is BUILT with the extractor's own selectors.py, because that
    is what a real ingestion would use.
    It is RESOLVED here by hand - xpath -> element -> normalise -> slice ->
    compare - rather than by calling selectors.verify(), so a bug in that
    function cannot make the round trip agree with itself. The one piece shared
    across the boundary is normalize.normalize_text, which is the documented
    contract every consumer must reimplement and which has its own
    JS-agreement test.

Exit codes: 0 the passage resolved and the quote matched, 1 it did not,
2 nothing to trace (no search hit, or the artifact is missing).

Usage:
    scripts/trace_citation.py "Orbán" [--root DIR] [--dsn DSN] [--keep]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from lxml import html as lxml_html

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import search as S                                              # noqa: E402
from causalia_extractor.normalize import normalize_text         # noqa: E402
from causalia_extractor.selectors import find_selector          # noqa: E402

#: Where the extractor's output tree lives; article_artifact.file_path is
#: relative to it.
DEFAULT_ROOT = Path.home() / "causalia-extraction-review" / "data"

OK, FAIL, SKIP = "\x1b[32m  ok\x1b[0m", "\x1b[31mFAIL\x1b[0m", "\x1b[33mskip\x1b[0m"


def step(n: int, what: str, status: str, detail: str = "") -> None:
    print(f"  {status}  {n}. {what}")
    for line in (detail.splitlines() if detail else []):
        print(f"           {line}")


def readability_path(cur, article_id: int, root: Path) -> Path | None:
    cur.execute("""
        SELECT f.file_path
          FROM corpus.article_artifact f
          JOIN corpus.article a ON a.id = f.article_id
         WHERE f.article_id = %s
           AND f.extraction_id = a.current_extraction_id
           AND f.kind = 'readability_html'
    """, (article_id,))
    row = cur.fetchone()
    return (root / row[0]) if row else None


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--root", type=Path, default=Path(
        os.environ.get("CX_ARTIFACT_ROOT", DEFAULT_ROOT)))
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--keep", action="store_true",
                    help="leave the passage_reference row behind")
    args = ap.parse_args(argv[1:])

    conn = S.connect(args.dsn)
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    print(f"\ntracing {args.query!r} from search to highlighted passage\n")

    # ---- 1. DISCOVERY ------------------------------------------------
    hits = S.search_articles(cur, args.query, limit=1, blocks_per_article=5)
    if not hits:
        step(1, f"search {args.query!r}", FAIL, "no article matched")
        return 2
    hit = hits[0]
    step(1, f"search {args.query!r}", OK,
         f"{hit['outlet']}  {hit['title'][:64]}\n"
         f"matched on: {hit['match_reason']}   score {hit['score']:.4f}")

    if not hit["blocks"]:
        step(2, "a matching content_block", SKIP,
             "the article matched on metadata only - nothing to cite")
        return 2

    # ---- 2. the citable unit -----------------------------------------
    block = hit["blocks"][0]
    step(2, "best matching content_block", OK,
         f"block {block['block_index']} ({block['block_type']})\n"
         f"xpath  {block['xpath']}")

    # ---- 3. the canonical document -----------------------------------
    path = readability_path(cur, hit["id"], args.root)
    if path is None or not path.is_file():
        step(3, "readability.html on disk", FAIL, f"missing: {path}")
        return 2
    tree = lxml_html.fromstring(path.read_bytes())
    step(3, "readability.html", OK, f"{path}  ({path.stat().st_size:,} bytes)")

    # ---- 4. locate the element the block describes --------------------
    found = tree.xpath(block["xpath"])
    if len(found) != 1:
        step(4, "the block's XPath selects exactly one element", FAIL,
             f"selected {len(found)}")
        return 1
    element = found[0]
    element_text = normalize_text(element)
    if element_text != (block["block_text"] or ""):
        step(4, "block_text equals the element's normalised text", FAIL,
             f"db  {block['block_text'][:70]!r}\ndom {element_text[:70]!r}")
        return 1
    step(4, "block_text == normalise(element)", OK,
         f"{len(element_text)} characters agree")

    # ---- 5. find the WORD ON THE PAGE that the query matched ----------
    # The query string is very often not the string in the document - that is
    # the whole point of migration 007, which finds `kormánynak` for a search
    # for `kormany`. So the passage to highlight cannot be found by looking for
    # the query text. It is found by asking the SAME lexeme rules that matched
    # the article which token of this block they matched, so discovery and
    # evidence agree by construction instead of by coincidence.
    needle = None
    for term in args.query.split():
        cur.execute("""
            SELECT t.tok
              FROM unnest(regexp_split_to_array(%(text)s, '[^[:alnum:]]+'))
                   WITH ORDINALITY AS t(tok, ord)
             WHERE t.tok <> ''
               AND corpus.search_vector(t.tok) @@ corpus.search_query(%(term)s)
             ORDER BY t.ord
             LIMIT 1
        """, {"text": element_text, "term": term})
        row = cur.fetchone()
        if row:
            needle = row[0]
            break
    if needle is None:
        step(5, "the token this block matched on", SKIP,
             "no token of this block matches the query's lexemes")
        return 2
    matched_note = ("" if needle.lower() == args.query.lower()
                    else f"   (query was {args.query!r})")
    selector = find_selector(tree, element, needle)
    if selector is None:
        step(5, "build a selector", FAIL, f"{needle!r} not locatable")
        return 1
    step(5, f"matched token {needle!r}{matched_note}", OK,
         f"[{selector.start}, {selector.end})  exact={selector.exact!r}")

    # ---- 6. store it as a citation ------------------------------------
    cur.execute("""
        INSERT INTO corpus.passage_reference
            (article_id, content_block_id, selector_xpath,
             quote_start, quote_end, quote_exact, quote_prefix, quote_suffix,
             resolution_status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'unverified')
        RETURNING id
    """, (hit["id"], block.get("block_id"), selector.value,
          selector.start, selector.end, selector.exact,
          selector.prefix, selector.suffix))
    reference_id = cur.fetchone()[0]
    step(6, "stored as corpus.passage_reference", OK, str(reference_id))

    # ---- 7. read it back the way a consumer would ---------------------
    cur.execute("SELECT selector FROM corpus.passage_selector WHERE id = %s",
                (reference_id,))
    payload = cur.fetchone()[0]
    step(7, "read back through corpus.passage_selector", OK,
         json.dumps(payload, ensure_ascii=False, indent=1))

    # ---- 8. resolve it INDEPENDENTLY ----------------------------------
    # Deliberately not selectors.verify(): xpath, slice, compare, by hand.
    target = tree.xpath(payload["value"])
    if len(target) != 1:
        step(8, "resolve the stored XPath", FAIL, f"selected {len(target)}")
        return 1
    text = normalize_text(target[0])
    lo, hi = payload["refinedBy"]["start"], payload["refinedBy"]["end"]
    got = text[lo:hi]
    want = payload["quote"]["exact"]
    if got != want:
        step(8, "sliced text == quote.exact", FAIL,
             f"want {want!r}\ngot  {got!r}")
        return 1
    step(8, "sliced text == quote.exact", OK, f"{got!r}")

    # ---- 9. what a reader would see -----------------------------------
    lo_ctx, hi_ctx = max(0, lo - 60), min(len(text), hi + 60)
    print("\n  the passage, as a frontend would highlight it:\n")
    print(f"      …{text[lo_ctx:lo]}\x1b[7m{got}\x1b[0m{text[hi:hi_ctx]}…\n")

    # 'ok' is the schema's vocabulary for a reference that resolved with its
    # quote intact - see migrations/005. The others are 'unverified',
    # 'repaired' (re-found by prefix+exact+suffix) and 'quote_not_found'.
    cur.execute("""UPDATE corpus.passage_reference
                      SET resolution_status = 'ok',
                          last_verified_at = now(),
                          verified_against_extraction_id =
                              (SELECT current_extraction_id FROM corpus.article
                                WHERE id = %s)
                    WHERE id = %s""", (hit["id"], reference_id))
    if not args.keep:
        cur.execute("DELETE FROM corpus.passage_reference WHERE id = %s",
                    (reference_id,))
        print("  (passage_reference removed; pass --keep to retain it)\n")
    else:
        print(f"  passage_reference {reference_id} kept, marked resolved\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
