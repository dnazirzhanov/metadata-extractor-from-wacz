#!/usr/bin/env python3
"""Render real Postgres search results as a browsable page you can judge.

A terminal table is a bad instrument for evaluating search. This runs a set of
queries against the development database and writes a self-contained HTML page:
per query, the parsed tsquery, the ranked hits with their matching passages
highlighted, and - the part that actually matters for judging recall - the
articles that CONTAIN the term but were not returned, with the lexeme that
explains why.

Nothing is simulated. Every number and every snippet on the page came out of
Postgres; the substring counts beside them are computed in Python precisely so
the two can disagree in public.

Usage:
    scripts/search_report.py -o out.html [--links] [--dsn DSN]

    --links   emit links to articles/<outlet>-<hash8>.html, for the copy that
              lives inside the review package next to that directory
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parent))
import search as S                                              # noqa: E402

# Sentinels that cannot occur in article text, so highlighting survives escaping.
HL_START, HL_STOP = "\x02", "\x03"

#: (query, why it is here, base form to compare against or None).
#: The comparison exists for inflection probes: an inflected word is usually not
#: in the text as a literal string either, so a substring yardstick would score
#: it 0/0 and measure nothing. What matters is whether the inflection reaches
#: the SAME articles the base form does.
QUERIES = [
    ("Orbán Viktor",        "the flagship case: a name in title, tags and prose", None),
    ("Orbán",               "the surname alone - watch what it stems to", None),
    ("Orbánt",              "the accusative. Does it reach what 'Orbán' reaches?", "Orbán"),
    ("Orbánnak",            "the dative. Same question", "Orbán"),
    ("Magyarország",        "the widest match in the sample", None),
    ("magyarorszag",        "the same query typed on a keyboard without accents", "Magyarország"),
    ("Magyarországról",     "an inflection the stemmer does NOT reduce", "Magyarország"),
    ("orosz-ukrán háború",  "three terms ANDed together", None),
    ("Szijjártó Péter",     "a second politician, for comparison", None),
    ("koronavírus",         "a topic word rather than a name", None),
    ("ukrajnai fejlesztés", "two common words that co-occur in few articles", None),
    ("baloldal",            "a widely distributed political term", None),
    ("Soros György",        "appears both as a tag and in prose", None),
    ("migrációs nyomás",    "a two-word phrase lifted from a headline", None),
    ("Donald Trump",        "absent from the sample - the zero-result control", None),
]

TAG_QUERIES = ["Magyarország", "Orbán Viktor", "koronavírus", "belföld"]


def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (text or "").lower())
                   if not unicodedata.combining(c))


def corpus_index(cur) -> dict:
    cur.execute("""
        SELECT a.id, a.url_hash, a.outlet, a.title, a.subtitle, a.description,
               a.tags, a.published_at,
               string_agg(coalesce(b.block_text, ''), ' ') AS body
        FROM corpus.article a
        LEFT JOIN corpus.content_block b
               ON b.article_id = a.id AND b.extraction_id = a.current_extraction_id
        GROUP BY a.id
    """)
    out = {}
    for row in cur.fetchall():
        meta = " ".join([row["title"] or "", row["subtitle"] or "",
                         row["description"] or "", " ".join(row["tags"] or [])])
        out[row["id"]] = {
            "url_hash": row["url_hash"], "outlet": row["outlet"],
            "title": row["title"] or "(no title)", "tags": row["tags"] or [],
            "published_at": str(row["published_at"] or "")[:10],
            "meta_folded": fold(meta), "body_folded": fold(row["body"] or ""),
            "haystack": fold(meta + " " + (row["body"] or "")),
        }
    return out


def substring_hits(corpus: dict, query: str) -> set[int]:
    """Articles where every query term STARTS A WORD. The yardstick for recall.

    Word-initial, not "anywhere in the string". A bare substring test is wrong
    in Hungarian and wrong in a way that inflates exactly the number this report
    exists to show. -ban/-ben is the inessive case ending, so the accent-folded
    spelling of "Orbán" occurs inside elsősorban, műsorban, szektorban,
    táborban, korban - measured on the 1,008-article evaluation corpus, a
    substring yardstick reported 79 recall misses for the query "Orbán" and
    every single one of them was a word like "elsősorban". The search engine was
    right and the ruler was wrong.

    Matching word-initially keeps the property that makes the yardstick worth
    having - it is computed in Python, independently of anything Postgres does,
    so no text-search configuration can score well by agreeing with itself -
    while removing a systematic bias against every query ending in a sequence
    the stemmer can read as a suffix.
    """
    terms = [fold(t) for t in re.split(r"[\s\-]+", query) if t]
    return {aid for aid, doc in corpus.items()
            if all(_starts_a_word(doc["haystack"], t) for t in terms)}


def _starts_a_word(haystack: str, term: str) -> bool:
    """True when `term` occurs in `haystack` at the start of a word.

    Both are already accent-folded. A word starts at the beginning of the string
    or after any character that is not a letter, a digit or an underscore.
    """
    if not term:
        return False
    start = haystack.find(term)
    while start != -1:
        if start == 0 or not (haystack[start - 1].isalnum()
                              or haystack[start - 1] == "_"):
            return True
        start = haystack.find(term, start + 1)
    return False


def lexemes_for(cur, word: str) -> str:
    """The lexemes the LIVE search actually indexes for a word.

    corpus.search_vector, not corpus.hungarian_ci. 007 retired that
    configuration - it is still installed only because ts_headline callers may
    name it - so reporting its lexemes here explained misses in terms of an
    engine that has not been running since. Every word now contributes two
    lexemes, the unaccented lemma and the unaccented surface form, and seeing
    both is the point: it is usually the lemma that explains a surprise.
    """
    cur.execute("SELECT corpus.search_vector(%s)::text", (word,))
    raw = cur.fetchone()[0]
    return " ".join(re.findall(r"'([^']+)'", raw)) or "(none)"


def explain_miss(cur, corpus: dict, article_id: int, query: str) -> dict:
    """Why an article containing the string was not returned: find the actual
    word in it and show the lexeme it produces."""
    doc = corpus[article_id]
    term = fold(re.split(r"[\s\-]+", query)[0])
    cur.execute("""
        SELECT b.block_text FROM corpus.content_block b
        JOIN corpus.article a ON a.id = b.article_id
        WHERE b.article_id = %s AND b.extraction_id = a.current_extraction_id
          AND b.block_text IS NOT NULL
    """, (article_id,))
    texts = [r[0] for r in cur.fetchall()]
    cur.execute("""SELECT coalesce(title,'') || ' ' || coalesce(subtitle,'')
                          || ' ' || coalesce(description,'')
                   FROM corpus.article WHERE id = %s""", (article_id,))
    texts.append(cur.fetchone()[0])
    for text in texts:
        for word in re.findall(r"[\wÀ-ɏ]+", text):
            if fold(word).startswith(term):
                return {"word": word, "lexeme": lexemes_for(cur, word),
                        "query_lexeme": lexemes_for(cur, query)}
    return {"word": "(not located)", "lexeme": "", "query_lexeme": lexemes_for(cur, query)}


def collect(cur, dict_cur) -> dict:
    corpus = corpus_index(dict_cur)
    report = {"queries": [], "tags": [], "corpus_size": len(corpus)}

    for query, why, base in QUERIES:
        started = time.perf_counter()
        # DISPLAY is the top 50; RECALL is measured against the complete match
        # set. Comparing the truncated list against an unlimited yardstick
        # counts every article below the cut as a miss - on this corpus that
        # reported 546 misses where there were 13.
        rows = S.search_articles(dict_cur, query, limit=50, blocks_per_article=4)
        returned_all = S.matching_ids(dict_cur, query)
        latency = (time.perf_counter() - started) * 1000

        dict_cur.execute(
            "SELECT websearch_to_tsquery('corpus.hungarian_ci', %s)::text", (query,))
        parsed = dict_cur.fetchone()[0]

        returned = {r["id"] for r in rows}
        substrings = substring_hits(corpus, query)

        results = []
        for rank, hit in enumerate(rows, 1):
            doc = corpus[hit["id"]]
            blocks = []
            for block in hit["blocks"]:
                blocks.append({
                    "index": block["block_index"],
                    "type": block["block_type"],
                    "xpath": block["xpath"],
                    "headline": block["headline"],
                    "rank": round(float(block["rank"]), 4),
                })
            results.append({
                "rank": rank,
                "score": round(hit["score"], 4),
                "meta_rank": round(hit["meta_rank"], 4),
                "body_rank": round(hit["body_rank"], 4),
                "reason": hit["match_reason"],
                "title": hit["title"] or "(no title)",
                "outlet": hit["outlet"],
                "published": str(hit["published_at"] or "")[:10],
                "tags": hit["tags"] or [],
                "url_hash": hit["url_hash"],
                "canonical": hit["canonical_url"] or hit["source_url"],
                "status": hit["extraction_status"],
                "blocks": blocks,
                "in_substring": hit["id"] in substrings,
            })

        misses = []
        for article_id in sorted(substrings - returned_all):
            doc = corpus[article_id]
            misses.append({
                "title": doc["title"], "outlet": doc["outlet"],
                "url_hash": doc["url_hash"],
                **explain_miss(cur, corpus, article_id, query)})

        comparison = None
        if base:
            base_rows = S.search_articles(dict_cur, base, limit=50, blocks_per_article=0)
            base_ids = {r["id"] for r in base_rows}
            comparison = {
                "base": base, "base_returned": len(base_ids),
                "shared": len(base_ids & returned),
                "reaches_same": bool(base_ids) and base_ids == returned,
                "base_lexeme": lexemes_for(cur, base),
                "query_lexeme": lexemes_for(cur, query),
            }

        report["queries"].append({
            "query": query, "why": why, "parsed": parsed, "comparison": comparison,
            "latency_ms": round(latency, 2),
            "returned": len(returned), "returned_all": len(returned_all),
            "substring": len(substrings),
            "stem_only": sorted(returned_all - substrings),
            "results": results, "misses": misses,
        })

    for tag in TAG_QUERIES:
        exact = S.filter_by_tag(dict_cur, tag)
        fts = S.search_articles(dict_cur, tag, limit=50, blocks_per_article=0)
        report["tags"].append({
            "tag": tag, "exact": len(exact), "fts": len(fts),
            "exact_titles": [f"{r['outlet']} — {r['title']}" for r in exact],
        })
    return report


def render(report: dict, links: bool) -> str:
    data = json.dumps(report, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", data).replace("__LINKS__", "true" if links else "false")


TEMPLATE = r"""<title>Search Results Review</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400;6..72,600&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root{
  --paper:#f4f6f7; --surface:#fff; --surface-2:#eef1f3; --ink:#141a20;
  --ink-soft:#3d4854; --muted:#64707d; --rule:#dde3e7; --rule-soft:#e8ecef;
  --accent:#10556b; --accent-soft:#e3eef2;
  --pass:#2c6e4b; --pass-bg:#e4efe8; --note:#8a5c12; --note-bg:#f6ecdb;
  --fail:#9c3535; --fail-bg:#f6e4e4; --hl:#ffe9a8; --hl-ink:#3d2b00;
  color-scheme: light dark;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#0f1418; --surface:#161d23; --surface-2:#1d262d; --ink:#e4e9ec;
  --ink-soft:#c2ccd4; --muted:#909ca8; --rule:#26313a; --rule-soft:#1f282f;
  --accent:#63b9d6; --accent-soft:#152a33;
  --pass:#77c295; --pass-bg:#16281f; --note:#d8a552; --note-bg:#2a2113;
  --fail:#e08a8a; --fail-bg:#2c1a1a; --hl:#5c4a12; --hl-ink:#ffe9a8;
}}
:root[data-theme="dark"]{
  --paper:#0f1418; --surface:#161d23; --surface-2:#1d262d; --ink:#e4e9ec;
  --ink-soft:#c2ccd4; --muted:#909ca8; --rule:#26313a; --rule-soft:#1f282f;
  --accent:#63b9d6; --accent-soft:#152a33;
  --pass:#77c295; --pass-bg:#16281f; --note:#d8a552; --note-bg:#2a2113;
  --fail:#e08a8a; --fail-bg:#2c1a1a; --hl:#5c4a12; --hl-ink:#ffe9a8;
}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);
  font-family:"IBM Plex Sans","Segoe UI",system-ui,sans-serif;font-size:15px;line-height:1.6}
.wrap{max-width:1220px;margin:0 auto;padding:0 24px 80px}
header.top{padding:44px 0 26px;border-bottom:2px solid var(--ink);
  display:flex;flex-direction:column;gap:12px}
.eyebrow{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--accent)}
h1{font-family:Newsreader,Georgia,serif;font-weight:600;
  font-size:clamp(2rem,4.6vw,2.9rem);line-height:1.06;margin:0;letter-spacing:-.015em}
.lede{font-family:Newsreader,Georgia,serif;font-size:1.15rem;color:var(--ink-soft);
  max-width:64ch;margin:0}
.layout{display:grid;grid-template-columns:264px 1fr;gap:34px;margin-top:32px;align-items:start}
@media(max-width:900px){.layout{grid-template-columns:1fr}}

/* query rail */
.rail{position:sticky;top:14px;display:flex;flex-direction:column;gap:1px;
  background:var(--rule);border:1px solid var(--rule)}
@media(max-width:900px){.rail{position:static}}
.qbtn{background:var(--surface);border:0;text-align:left;cursor:pointer;
  padding:10px 13px;font:inherit;color:var(--ink);display:flex;
  justify-content:space-between;align-items:baseline;gap:10px;width:100%}
.qbtn:hover{background:var(--surface-2)}
.qbtn[aria-current="true"]{background:var(--accent-soft);
  box-shadow:inset 3px 0 0 var(--accent)}
.qbtn .qt{font-family:"IBM Plex Mono",monospace;font-size:12.5px}
.qbtn .qn{font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--muted);
  font-variant-numeric:tabular-nums}
.qbtn[data-zero="true"] .qn{color:var(--fail)}
.railhead{background:var(--surface-2);padding:8px 13px;
  font-family:"IBM Plex Mono",monospace;font-size:10.5px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--muted)}

/* query header */
.qhead{background:var(--surface);border:1px solid var(--rule);padding:20px 22px;margin-bottom:18px}
.qhead h2{font-family:Newsreader,Georgia,serif;font-size:1.6rem;margin:0 0 4px;font-weight:600}
.qhead .why{color:var(--muted);margin:0 0 16px;font-size:14px}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:1px;
  background:var(--rule);border:1px solid var(--rule)}
.fact{background:var(--surface);padding:11px 13px}
.fact .k{font-family:"IBM Plex Mono",monospace;font-size:10px;letter-spacing:.09em;
  text-transform:uppercase;color:var(--muted);display:block;margin-bottom:5px}
.fact .v{font-family:"IBM Plex Mono",monospace;font-size:15px;
  font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.fact.warn .v{color:var(--note)} .fact.bad .v{color:var(--fail)} .fact.ok .v{color:var(--pass)}

/* results */
.res{background:var(--surface);border:1px solid var(--rule);margin-bottom:12px}
.res .rhead{display:flex;gap:14px;align-items:flex-start;padding:14px 18px}
.rank{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--muted);
  min-width:22px;padding-top:3px;font-variant-numeric:tabular-nums}
.rmain{flex:1;min-width:0}
.rtitle{font-family:Newsreader,Georgia,serif;font-size:1.16rem;line-height:1.35;
  margin:0 0 5px;font-weight:600}
.rtitle a{color:inherit;text-decoration:none;border-bottom:1px solid var(--rule)}
.rtitle a:hover{border-bottom-color:var(--accent);color:var(--accent)}
.rmeta{display:flex;flex-wrap:wrap;gap:5px 14px;font-family:"IBM Plex Mono",monospace;
  font-size:11.5px;color:var(--muted);align-items:center}
.score{display:flex;flex-direction:column;align-items:flex-end;gap:5px;min-width:96px}
.scoreval{font-family:"IBM Plex Mono",monospace;font-size:15px;
  font-variant-numeric:tabular-nums}
.bar{height:4px;width:92px;background:var(--surface-2);overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent)}
.chip{display:inline-block;font-family:"IBM Plex Mono",monospace;font-size:10px;
  letter-spacing:.06em;text-transform:uppercase;padding:2px 6px;font-weight:500;white-space:nowrap}
.chip.both{background:var(--pass-bg);color:var(--pass)}
.chip.metadata{background:var(--accent-soft);color:var(--accent)}
.chip.body{background:var(--note-bg);color:var(--note)}
.chip.tag{background:var(--accent-soft);color:var(--accent)}
.chip.tagmatch{background:var(--hl);color:var(--hl-ink)}
.chip.warnc{background:var(--fail-bg);color:var(--fail)}
.blocks{border-top:1px solid var(--rule-soft);padding:4px 18px 12px}
.blk{padding:9px 0;border-bottom:1px dotted var(--rule-soft)}
.blk:last-child{border-bottom:0}
.blk .snip{font-family:Newsreader,Georgia,serif;font-size:1.03rem;line-height:1.55;
  color:var(--ink-soft)}
.blk .bmeta{font-family:"IBM Plex Mono",monospace;font-size:10.5px;color:var(--muted);
  margin-top:4px;display:flex;gap:14px;flex-wrap:wrap}
mark{background:var(--hl);color:var(--hl-ink);padding:.05em .12em;border-radius:1px;font-weight:500}
.nores{background:var(--surface);border:1px dashed var(--rule);padding:26px 22px;
  color:var(--muted);text-align:center}

/* base-form comparison */
.cmp{border:1px solid var(--rule);padding:14px 18px;margin:18px 0 12px;background:var(--surface)}
.cmp.bad{background:var(--fail-bg)} .cmp.ok{background:var(--pass-bg)}
.cmp h3{margin:0 0 8px;font-family:"IBM Plex Sans",sans-serif;font-size:.8rem;font-weight:600;
  letter-spacing:.09em;text-transform:uppercase}
.cmp.bad h3{color:var(--fail)} .cmp.ok h3{color:var(--pass)}
.cmp p{margin:0;font-size:13.5px;color:var(--ink-soft);max-width:72ch}
.lexrow{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:10px;
  font-family:"IBM Plex Mono",monospace;font-size:12px}
.lexrow b{color:var(--ink);font-weight:500}
.lexrow .box{background:var(--surface);border:1px solid var(--rule);padding:3px 9px}

/* misses */
.misses{background:var(--fail-bg);border:1px solid var(--rule);margin:18px 0 12px;padding:16px 18px}
.misses h3{margin:0 0 4px;font-family:"IBM Plex Sans",sans-serif;font-size:.8rem;
  font-weight:600;letter-spacing:.09em;text-transform:uppercase;color:var(--fail)}
.misses p{margin:0 0 12px;font-size:13.5px;color:var(--ink-soft);max-width:70ch}
.miss{background:var(--surface);border:1px solid var(--rule);padding:10px 13px;margin-bottom:8px}
.miss:last-child{margin-bottom:0}
.miss .mt{font-family:Newsreader,Georgia,serif;font-size:1.02rem;margin-bottom:6px}
.miss .mx{font-family:"IBM Plex Mono",monospace;font-size:11.5px;color:var(--muted);
  display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.miss .mx b{color:var(--ink);font-weight:500}
.arrow{color:var(--muted)}
.good{background:var(--pass-bg);border-color:var(--rule)}
.good h3{color:var(--pass)}

/* tag table */
section.tags{margin-top:54px}
h2.sec{font-family:Newsreader,Georgia,serif;font-size:1.7rem;font-weight:600;margin:0 0 6px}
.sublede{color:var(--muted);margin:0 0 20px;max-width:64ch}
.scroll{overflow-x:auto;border:1px solid var(--rule);background:var(--surface)}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:9px 14px;border-bottom:1px solid var(--rule-soft);vertical-align:top}
thead th{font-family:"IBM Plex Mono",monospace;font-size:10.5px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--muted);font-weight:500;background:var(--surface-2)}
tbody tr:last-child td{border-bottom:0}
td.num{text-align:right;font-variant-numeric:tabular-nums;font-family:"IBM Plex Mono",monospace}
code{font-family:"IBM Plex Mono",monospace;font-size:.875em;background:var(--surface-2);
  padding:.1em .35em;border-radius:2px}
footer{margin-top:56px;padding-top:20px;border-top:1px solid var(--rule);
  color:var(--muted);font-size:13px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>

<div class="wrap">
<header class="top">
  <span class="eyebrow">Corpus dev database · every number below came out of Postgres</span>
  <h1>Search Results Review</h1>
  <p class="lede">Twelve queries against the 36-article corpus. For each one: what
  Postgres parsed it into, what it ranked, the passages it matched — and the articles
  that contain the term but were <em>not</em> returned, with the lexeme that explains why.</p>
</header>

<div class="layout">
  <nav class="rail" id="rail"><div class="railhead">Queries</div></nav>
  <main id="panel"></main>
</div>

<section class="tags">
  <h2 class="sec">Exact tag filter vs full text</h2>
  <p class="sublede">Two different questions about the same word. The filter answers
  “tagged with this”; full text answers “mentions this”. Keeping them separate is the
  point — conflating them is how a filter starts lying.</p>
  <div class="scroll"><table>
    <thead><tr><th>Tag</th><th class="num">Exact filter</th><th class="num">Full text</th><th>Articles carrying the tag</th></tr></thead>
    <tbody id="tagbody"></tbody>
  </table></div>
</section>

<footer id="foot"></footer>
</div>

<script type="application/json" id="data">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const LINKS = __LINKS__;
const esc = s => (s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// ts_headline markers are \x02/\x03 - escape first, then turn them into <mark>.
const hl = s => esc(s).replace(/\x02/g, '<mark>').replace(/\x03/g, '</mark>');
const href = h => LINKS ? `articles/${h.outlet}-${h.url_hash.slice(0,8)}.html` : null;

function results(q){
  if(!q.results.length) return `<div class="nores">Postgres returned nothing for
    <code>${esc(q.query)}</code>.${q.substring
      ? ` But ${q.substring} article(s) contain it as text — see below.`
      : ' No article contains this text either, so the empty answer is correct.'}</div>`;
  const top = Math.max(...q.results.map(r => r.score)) || 1;
  return q.results.map(r => {
    const link = href(r);
    const title = link ? `<a href="${link}">${esc(r.title)}</a>` : esc(r.title);
    const tags = r.tags.map(t =>
      `<span class="chip ${q.query.toLowerCase().includes(t.toLowerCase())||t.toLowerCase().includes(q.query.toLowerCase())?'tagmatch':'tag'}">${esc(t)}</span>`).join(' ');
    const blocks = r.blocks.map(b => `
      <div class="blk">
        <div class="snip">…${hl(b.headline)}…</div>
        <div class="bmeta"><span>block ${b.index} · ${esc(b.type)}</span>
          <span>${esc(b.xpath)}</span><span>rank ${b.rank}</span></div>
      </div>`).join('');
    return `<article class="res">
      <div class="rhead">
        <div class="rank">${r.rank}</div>
        <div class="rmain">
          <p class="rtitle">${title}</p>
          <div class="rmeta">
            <span class="chip ${r.reason}">${r.reason}</span>
            <span>${esc(r.outlet)}</span><span>${esc(r.published)||'no date'}</span>
            ${r.status!=='success'?`<span class="chip warnc">${esc(r.status)}</span>`:''}
            ${!r.in_substring?'<span class="chip warnc">stem-only match</span>':''}
            ${tags}
          </div>
        </div>
        <div class="score">
          <span class="scoreval">${r.score.toFixed(4)}</span>
          <span class="bar"><i style="width:${Math.max(3,100*r.score/top)}%"></i></span>
          <span class="rmeta">meta ${r.meta_rank} · body ${r.body_rank}</span>
        </div>
      </div>
      ${blocks?`<div class="blocks">${blocks}</div>`:''}
    </article>`;
  }).join('');
}

function comparison(q){
  const c = q.comparison;
  if(!c) return '';
  const same = c.reaches_same;
  const cls = same ? 'ok' : 'bad';
  const verdict = same
    ? `Reaches exactly the same ${c.base_returned} article(s) as <code>${esc(c.base)}</code>.
       The stemmer connects the two forms, which is what it is for.`
    : (q.returned === 0
        ? `Returns <b>nothing</b>, while <code>${esc(c.base)}</code> returns
           <b>${c.base_returned}</b>. A reader searching this form finds none of the
           articles that are about it.`
        : `Returns ${q.returned}, <code>${esc(c.base)}</code> returns ${c.base_returned},
           and only ${c.shared} are shared. The two forms do not agree.`);
  return `<div class="cmp ${cls}">
    <h3>${same?'Reaches the base form':'Does not reach the base form'}</h3>
    <p>${verdict}</p>
    <div class="lexrow">
      <span class="box">${esc(q.query)} <span class="arrow">→</span> <b>${esc(c.query_lexeme)}</b></span>
      <span class="arrow">${same?'=':'≠'}</span>
      <span class="box">${esc(c.base)} <span class="arrow">→</span> <b>${esc(c.base_lexeme)}</b></span>
    </div></div>`;
}

function misses(q){
  if(!q.misses.length) return `<div class="misses good"><h3>No misses</h3>
    <p>Every article containing this text was returned. Recall is complete for this query.</p></div>`;
  return `<div class="misses"><h3>${q.misses.length} article(s) contain the text but were NOT returned</h3>
    <p>Search compares lexemes, not strings. Each row shows the word actually in the
    article, the lexeme it produces, and the lexeme your query produced — when those two
    differ, the article cannot match.</p>
    ${q.misses.map(m => {
      const link = href(m);
      return `<div class="miss">
        <div class="mt">${link?`<a href="${link}">${esc(m.title)}</a>`:esc(m.title)}</div>
        <div class="mx"><span>${esc(m.outlet)}</span>
          <span>found in article: <b>${esc(m.word)}</b></span>
          <span class="arrow">→</span><span>lexeme <b>${esc(m.lexeme)}</b></span>
          <span class="arrow">vs query</span><span>lexeme <b>${esc(m.query_lexeme)}</b></span>
        </div></div>`;
    }).join('')}</div>`;
}

function show(i){
  const q = DATA.queries[i];
  document.querySelectorAll('.qbtn').forEach((b,j)=>b.setAttribute('aria-current', j===i));
  const recall = q.substring ? Math.round(100*(q.substring-q.misses.length)/q.substring) : null;
  document.getElementById('panel').innerHTML = `
    <div class="qhead">
      <h2>${esc(q.query)}</h2>
      <p class="why">${esc(q.why)}</p>
      <div class="facts">
        <div class="fact"><span class="k">parsed tsquery</span><span class="v">${esc(q.parsed)||'(empty)'}</span></div>
        <div class="fact ${q.returned?'ok':'bad'}"><span class="k">returned</span><span class="v">${q.returned}</span></div>
        <div class="fact"><span class="k">contain the text</span><span class="v">${q.substring}</span></div>
        <div class="fact ${q.misses.length?'warn':'ok'}"><span class="k">recall</span><span class="v">${recall===null?'n/a':recall+'%'}</span></div>
        <div class="fact"><span class="k">latency</span><span class="v">${q.latency_ms} ms</span></div>
      </div>
    </div>
    ${comparison(q)}
    ${misses(q)}
    ${results(q)}`;
  window.scrollTo({top:0,behavior:'instant'});
}

const rail = document.getElementById('rail');
DATA.queries.forEach((q,i)=>{
  const b = document.createElement('button');
  b.className='qbtn'; b.type='button'; b.dataset.zero = q.returned===0;
  b.innerHTML = `<span class="qt">${esc(q.query)}</span><span class="qn">${q.returned}</span>`;
  b.onclick = ()=>show(i);
  rail.appendChild(b);
});
document.getElementById('tagbody').innerHTML = DATA.tags.map(t=>`
  <tr><td><code>${esc(t.tag)}</code></td><td class="num">${t.exact}</td>
      <td class="num">${t.fts}</td>
      <td>${t.exact_titles.length?t.exact_titles.map(esc).join('<br>'):'<span style="color:var(--muted)">— none —</span>'}</td></tr>`).join('');
document.getElementById('foot').innerHTML =
  `${DATA.corpus_size} articles in the development database. Ranked with ` +
  `<code>ts_rank</code> over <code>corpus.hungarian_ci</code>; score = metadata rank + ` +
  `best matching block rank. Snippets are <code>ts_headline</code> output, unedited.`;
show(0);
</script>
"""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--out", type=Path, required=True)
    parser.add_argument("--dsn", default=os.environ.get("CX_DEV_DSN", S.DEFAULT_DSN))
    parser.add_argument("--links", action="store_true",
                        help="link results to articles/<outlet>-<hash8>.html")
    args = parser.parse_args(argv[1:])

    # ts_headline must emit sentinels, not markup, so escaping happens in the page.
    S.HEADLINE_OPTS = (f"MaxFragments=2,FragmentDelimiter= … ,MinWords=6,MaxWords=26,"
                       f"StartSel={HL_START},StopSel={HL_STOP}")

    connection = psycopg2.connect(args.dsn)
    with connection.cursor() as cur, \
         connection.cursor(cursor_factory=psycopg2.extras.DictCursor) as dict_cur:
        report = collect(cur, dict_cur)
    connection.close()

    args.out.write_text(render(report, args.links), encoding="utf-8")
    total = sum(q["returned"] for q in report["queries"])
    missed = sum(len(q["misses"]) for q in report["queries"])
    print(f"wrote {args.out}  ({len(report['queries'])} queries, {total} hits, "
          f"{missed} recall misses)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
