#!/usr/bin/env python3
"""Pressure-test the pipeline: .wacz -> article directory -> Postgres -> search.

SECTION A - CHAIN OF CUSTODY, the link nothing else checks.

    .wacz  --A-->  article directory  --dev_validate-->  Postgres  --?-->  search

scripts/dev_validate.py and scripts/validate_ingestion.py verify the middle link
exhaustively - 64,022 checks over 1,008 articles - but NEITHER OPENS A .wacz.
The only archive access anywhere in scripts/ is cx_ingest.py hashing one for a
dev stand-in row. So nothing has ever checked that readability.html came from
the archive, or that every archive that should have produced an article did.

That gap is where a whole class of defect lives, and this corpus has the
canonical example. ripost.hu captures that hold the Hungarian 18+ interstitial
instead of the article return HTTP 200, weigh ~1.36 MB, and STILL SERVE THE
ARTICLE'S JSON-LD AND OPENGRAPH - so title, date, section and author all extract
perfectly while the body is empty. Every metadata assertion passes. Roughly 3.5%
of ripost, about 10,000 articles. No check that reads only the extractor's own
output can see it, because the output is internally consistent; the only witness
is the archive.

WHAT THIS ASSERTS, AND WHAT IT ONLY MEASURES

Two of these are hard assertions and the rest are measurements, because the
difference is not decoration - a harness that reports a defect where none exists
teaches people to ignore it, which this project has already learned once (both
search harnesses compared a LIMIT-truncated list against an unlimited yardstick
and reported 546 recall misses where there were 13).

  ASSERTED  the archive opens, and holds an HTML document
  ASSERTED  every extracted block's text is really in the archive - FIDELITY.
            A block whose words are not in the capture was invented downstream.
  MEASURED  prose coverage: what fraction of the archive's article-like
            paragraphs reached a block. NOT a pass/fail number - see below.
  MEASURED  extraction coverage: did every archive produce an output directory.
  FLAGGED   an article whose metadata extracted cleanly while its prose coverage
            collapsed. That is the age-gate/truncation signature.

WHY PROSE COVERAGE IS A COMPARATIVE SIGNAL AND NOT A VERDICT

The archive's <p> elements include navigation, footers, related-article teasers,
cookie notices and comment forms. Perfect extraction therefore does NOT score
100%, and a raw coverage figure read as "we lost 40% of the article" would be
wrong. Two things make the number useful anyway:

  * paragraphs are filtered by a heuristic INDEPENDENT of readability - long
    enough to be prose, ending in sentence punctuation, and not inside
    nav/footer/aside/header/form. Independent matters: scoring against
    readability's own idea of the article would only prove the extractor agrees
    with itself.
  * it is read as a DISTRIBUTION per outlet and per stratum. A single article at
    30% means little; an outlet whose median is 30% while another's is 85% is a
    finding, and an article at 2% with a perfect title is the age-gate class.

HANDOFF records that 20.1% of prose never reaches content.json and that 90% of
ripost captures lose at least one paragraph. This is the instrument for that
number, and it should be quoted as "coverage by this heuristic", never as truth.

COVERAGE IS NOT COMPARABLE ON THE ANGULAR OUTLETS, AND THE REPORT SAYS SO

On magyarnemzet and mandiner the article body is server-rendered into an
ng-state JSON blob rather than into DOM <p> elements, and the extractor's
ng-state fallback is the primary path there. archive_paragraphs() reads the DOM,
so on those outlets it finds the standfirst, the about-blurb and the related
teasers - furniture - and none of the body. Coverage then reads 0.00 for an
article whose ten blocks are perfectly good prose.

The block count disambiguates it, so the report prints both:

    coverage low  + blocks healthy (>=5)  ->  not comparable, body came from
                                              ng-state and this measure is blind
    coverage low  + blocks <= 2           ->  TRUNCATION, and it is real

Measured over the 1,008-article eval set: magyarnemzet has a median of ONE text
block against seven archive paragraphs, and both readings are present in the
same outlet - some articles yield ten sound blocks, others exactly one. So the
outlet cannot be summarised by a single number, and the per-article rows are
where the answer is.

MEMORY

Archives are opened with html_only=True. read_archive otherwise buffers every
image and video body into ArchiveContents.payloads at once, and the largest
captures in this corpus reach 1.9 GB of mostly video. Section A wants the
document only.

Usage:
    # audit an existing extraction tree (the 1,008-article eval set)
    scripts/pressure_test.py --section A \\
        --root /mnt/hdd/c0cshf/causalia-eval-20260902/out \\
        --pages-root /mnt/hdd/c0cshf/causalia/pages \\
        -o /tmp/pressure_A.json

    # restrict to a stratified sample, and label findings by stratum
    scripts/pressure_test.py --section A --root $OUT \\
        --manifest /mnt/hdd/c0cshf/pressure_sample.json -o /tmp/pressure_A.json

Read-only throughout: it opens archives and extractor output, and writes only
the report it is asked for.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from causalia_extractor import wacz                              # noqa: E402
from causalia_extractor.dom import reparse                       # noqa: E402
from causalia_extractor.normalize import (                       # noqa: E402
    collapse, element_raw_text)

#: Elements whose text a reader never sees as prose. Stripped when collecting
#: PARAGRAPHS for the coverage measure - and deliberately NOT when collecting
#: tokens for the fidelity check. See WORD_RE.
DEAD_TAGS = ("script", "style", "noscript", "template", "svg")

#: Fidelity tokens are taken over the WHOLE decoded capture, scripts included,
#: and split on non-word characters. Both halves of that are corrections to an
#: earlier version of this file that got the answer wrong:
#:
#:  * SCRIPTS MUST BE INCLUDED. On the Angular outlets the article body lives in
#:    a server-rendered ng-state JSON blob, and the extractor's ng-state
#:    fallback is the PRIMARY extraction path there - it fired on 59 of 60
#:    sampled magyarnemzet articles. Stripping <script> made the visible-DOM
#:    text 3,511 chars out of 794,557 and would have reported the entire
#:    `angular` stratum as fabricated text. The question fidelity asks is "are
#:    these words in the capture at all", and a script blob is in the capture.
#:  * SPLITTING ON WHITESPACE IS NOT ENOUGH. normalize.element_raw_text
#:    concatenates text nodes with NO separator, exactly as textContent does -
#:    correct within one paragraph, and wrong across a document, where it fuses
#:    the last word of one <li> onto the first word of the next. A word-boundary
#:    split is immune to that, and to JSON escaping in the ng-state blob.
WORD_RE = re.compile(r"\w+", re.UNICODE)

#: Ancestors that mark a paragraph as furniture rather than article prose. This
#: is the independent half of the coverage heuristic - it must NOT consult
#: readability, or coverage would only measure the extractor agreeing with
#: itself.
FURNITURE_ANCESTORS = ("nav", "footer", "aside", "header", "form")

#: Tags that can hold a prose paragraph.
PROSE_TAGS = ("p", "h2", "h3", "h4", "h5", "h6", "blockquote", "li")

#: A paragraph shorter than this is a caption, a byline, a menu item or a
#: button, and counting those would swamp the signal.
MIN_PARA_CHARS = 80

#: Hungarian and English sentence enders, plus the closing quotes that follow
#: them. Prose ends in punctuation; furniture usually does not.
SENTENCE_END = re.compile(r'[.!?:][\s"\'”„»«)\]]*$')

#: A paragraph appearing in at least this many articles OF THE SAME OUTLET is
#: boilerplate, whatever it looks like. The second independent half of the
#: coverage heuristic, and it replaces a hand-written phrase list that would
#: need maintaining per outlet and would still miss the next one.
#:
#: Added because the first run's six flagged articles included four carrying the
#: same advertising blurb - "Portfóliónk minőségi tartalmat jelent minden olvasó
#: számára. Egyedülálló elérést, országos lefedettséget..." - a media-kit pitch
#: that is long, ends in sentence punctuation and sits outside
#: nav/footer/aside, so every structural test passes it. Repetition gives it
#: away where structure cannot.
#:
#: PER OUTLET, not corpus-wide, and that distinction is load-bearing. The county
#: titles - bama, baon, beol, boon, duol, delmagyar and the rest - are one
#: publisher network that SYNDICATES the same story across sister sites, so a
#: genuine article paragraph legitimately appears in twenty articles. Counting
#: corpus-wide classified real prose as boilerplate: "Emmanuel Macron kétoldalú
#: megbeszélést folytat majd Áder János köztársasági elnökkel" is an article,
#: not furniture, and it showed up in exactly 20. Within ONE outlet the ad blurb
#: still repeats on every article while a syndicated story repeats once or
#: twice, so the two separate cleanly.
BOILERPLATE_MIN_ARTICLES = 5

#: Below this, the prose did not survive extraction in any meaningful sense.
#: Reported as SUSPECT, never as a failure - see the module docstring.
COVERAGE_FLOOR = 0.25

#: A block whose token overlap with the archive falls below this was not merely
#: reshaped by readability (which drops inline furniture, so an exact substring
#: match legitimately fails) - its words are not in the capture at all.
#:
#: Calibrated, not guessed. Over 300 real articles the WEAKEST block per article
#: sits at 0.965 (p01) and 1.0 (median), while fabricated text scores 0.000 to
#: 0.300 in a negative control. The floor sits in a wide empty band between the
#: two, with 0.14 of headroom below the worst real content.
FIDELITY_FLOOR = 0.80

#: ...but a ratio is meaningless on a two-word block, which can only score 0,
#: 0.5 or 1.0. Real examples that tripped the floor on nothing:
#: "dolgozók munkahelyeinek" (2 tokens, one absent -> 0.50) and
#: "és Miskolc Szenpéteri kapui" (4 tokens -> 0.75). So a block is reported only
#: when MORE THAN ONE of its words is missing, which leaves wholesale
#: fabrication caught - invented prose is missing every word, not one.
FIDELITY_MIN_MISSING = 2


def archive_tokens(html: str) -> set[str]:
    """Every word in the capture, scripts included. The fidelity haystack."""
    return set(WORD_RE.findall(html.lower()))


def archive_paragraphs(html: str) -> list[str]:
    """The capture's article-like paragraphs, for the coverage measure.

    Normalised with the SAME functions the extractor uses for block text, so
    comparing the two compares like with like - the point of importing
    normalize rather than writing a second collapse here.
    """
    tree = reparse(html)
    root = tree.getroot()

    for tag in DEAD_TAGS:
        for node in root.iter(tag):
            node.text = None
            for child in list(node):
                node.remove(child)

    paragraphs = []
    for tag in PROSE_TAGS:
        for node in root.iter(tag):
            text = collapse(element_raw_text(node))
            if len(text) < MIN_PARA_CHARS or not SENTENCE_END.search(text):
                continue
            ancestors = {a.tag for a in node.iterancestors()
                         if isinstance(a.tag, str)}
            if ancestors & set(FURNITURE_ANCESTORS):
                continue
            paragraphs.append(text)
    return paragraphs


def token_overlap(needle: str, haystack_tokens: set[str]) -> float:
    """Fraction of the needle's words present in the haystack.

    Word-boundary tokenised on both sides, so it is unaffected by
    element_raw_text fusing words across element boundaries, and by readability
    legitimately dropping inline elements - a share button, a related-link span
    - which makes a paragraph a SUBSEQUENCE of the capture rather than a
    substring.
    """
    tokens = WORD_RE.findall(needle.lower())
    if not tokens:
        return 1.0
    return sum(1 for t in tokens if t in haystack_tokens) / len(tokens)


def load_manifest(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {row["url_hash"]: row for row in data["archives"]}


def wacz_for(directory: Path, pages_root: Path, outlet: str | None,
             manifest_row: dict | None) -> Path | None:
    """Where the archive for this output directory lives.

    The manifest carries wacz_path outright. Without one, the corpus layout is
    <pages_root>/<outlet>/<first2 of hash>/<hash>/page.wacz, and the output
    directory is named by the same hash.
    """
    if manifest_row and manifest_row.get("wacz_path"):
        return Path(manifest_row["wacz_path"])
    url_hash = directory.name
    if outlet is None:
        return None
    candidate = pages_root / outlet / url_hash[:2] / url_hash / "page.wacz"
    return candidate


def audit_one(directory: Path, wacz_path: Path | None) -> dict:
    """Section A for one article. Never raises; every failure becomes a field."""
    result: dict = {
        "url_hash": directory.name,
        "outlet": directory.parent.parent.name,
        "wacz": str(wacz_path) if wacz_path else None,
        "status": "ok",
        "errors": [],
    }

    content_path = directory / "content.json"
    if not content_path.is_file():
        result["status"] = "no_output"
        result["errors"].append("content.json missing")
        return result

    try:
        document = json.loads(content_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["status"] = "unreadable_output"
        result["errors"].append(f"content.json: {exc}")
        return result

    # content.json is {"blocks": [...]} - see docs/data-contract.md. Textual
    # types carry "text"; a list ALSO carries per-item text, and those items are
    # prose that would otherwise look lost.
    blocks = document.get("blocks", []) if isinstance(document, dict) else []
    block_texts = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("text"):
            block_texts.append(block["text"])
        for item in block.get("items") or []:
            if isinstance(item, dict) and item.get("text"):
                block_texts.append(item["text"])
    result["blocks"] = len(blocks)
    result["blocks_with_text"] = len(block_texts)

    article = {}
    article_path = directory / "article.json"
    if article_path.is_file():
        try:
            article = json.loads(article_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    result["has_title"] = bool(article.get("title"))
    result["has_published"] = bool(article.get("published_at"))

    if wacz_path is None or not wacz_path.is_file():
        result["status"] = "no_archive"
        result["errors"].append(f"archive not found: {wacz_path}")
        return result

    # ---- ASSERTED: the archive opens and holds a document -----------------
    try:
        contents = wacz.read_archive_for_page(wacz_path, html_only=True)
    except wacz.ArchiveUnreadable as exc:
        result["status"] = "archive_unreadable"
        result["errors"].append(str(exc))
        return result
    except Exception as exc:                       # noqa: BLE001 - report, never abort
        result["status"] = "archive_error"
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        return result

    result["html_records"] = contents.html_record_count
    result["has_screenshot"] = contents.screenshot is not None
    if not contents.main_html:
        result["status"] = "no_html_record"
        result["errors"].append("archive holds no HTML document")
        return result

    try:
        html = wacz.decode_html(contents)
        haystack_tokens = archive_tokens(html)
        paragraphs = archive_paragraphs(html)
    except Exception as exc:                       # noqa: BLE001
        result["status"] = "archive_unparseable"
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        return result

    result["archive_chars"] = len(html)
    result["archive_words"] = len(haystack_tokens)
    result["archive_paragraphs"] = len(paragraphs)

    # ---- ASSERTED: fidelity - the blocks came from this archive ------------
    full = 0
    weak: list[dict] = []
    overlaps: list[float] = []
    near_misses = 0
    for text in block_texts:
        tokens = WORD_RE.findall(text.lower())
        present = sum(1 for t in tokens if t in haystack_tokens)
        missing = len(tokens) - present
        overlap = 1.0 if not tokens else present / len(tokens)
        overlaps.append(overlap)
        if overlap >= 1.0:
            full += 1
            continue
        if overlap < FIDELITY_FLOOR:
            if missing < FIDELITY_MIN_MISSING:
                # Below the floor on arithmetic alone - a short block.
                near_misses += 1
                continue
            weak.append({"overlap": round(overlap, 3), "missing": missing,
                         "tokens": len(tokens), "text": text[:180]})
    result["blocks_short_near_miss"] = near_misses
    result["blocks_fully_in_archive"] = full
    result["blocks_not_in_archive"] = len(weak)
    # The WEAKEST block, recorded whether or not it failed. This is what
    # calibrates FIDELITY_FLOOR: if real content routinely sits just above the
    # floor, the floor is guesswork and the next extractor change will make it
    # fire spuriously. Reported as a distribution by report_section_a.
    result["min_block_overlap"] = round(min(overlaps), 3) if overlaps else None
    result["weak_blocks"] = weak[:5]
    if weak:
        result["status"] = "fidelity"
        result["errors"].append(
            f"{len(weak)} block(s) whose words are not in the archive")

    # ---- MEASURED: prose coverage -----------------------------------------
    if paragraphs:
        # A separator that cannot occur in article text, so a paragraph cannot
        # appear to match by straddling two blocks.
        joined = " \u241f ".join(block_texts)
        block_tokens = set(t for t in joined.split(" ") if t)
        # Reached = present verbatim in the extracted text, OR nearly all of its
        # words are, which is what readability splitting one archive paragraph
        # into two blocks looks like.
        matched = sum(1 for para in paragraphs
                      if para in joined
                      or token_overlap(para, block_tokens) >= 0.9)
        result["prose_coverage"] = round(matched / len(paragraphs), 4)
        result["paragraphs_matched"] = matched
        reached = {para for para in paragraphs
                   if para in joined
                   or token_overlap(para, block_tokens) >= 0.9}
        lost = sorted((p for p in paragraphs if p not in reached),
                      key=len, reverse=True)
        result["longest_lost"] = [p[:200] for p in lost[:3]]
        # Hashes, so summarise() can find which paragraphs RECUR across articles
        # and reclassify them as boilerplate. Texts are not stored per article;
        # only a sample per hash, once, in the summary.
        result["para_hashes"] = [para_hash(p) for p in paragraphs]
        result["lost_hashes"] = [para_hash(p) for p in lost]
        result["lost_samples"] = {para_hash(p): p[:160] for p in lost[:6]}
    else:
        result["prose_coverage"] = None
        result["paragraphs_matched"] = 0
        result["longest_lost"] = []
        result["para_hashes"] = []
        result["lost_hashes"] = []
        result["lost_samples"] = {}

    # ---- FLAGGED: metadata fine, prose gone -------------------------------
    coverage = result["prose_coverage"]
    if (coverage is not None and coverage < COVERAGE_FLOOR
            and result["has_title"] and result["archive_paragraphs"] >= 3):
        if result["status"] == "ok":
            result["status"] = "suspect"
        result["errors"].append(
            f"prose coverage {coverage:.0%} with a clean title - "
            f"the age-gate/truncation signature")

    return result


def para_hash(text: str) -> str:
    """A short stable id for a paragraph, for cross-article recurrence counting."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return round(ordered[index], 4)


def find_boilerplate(results: list[dict]) -> dict[tuple[str, str], int]:
    """Paragraphs recurring WITHIN one outlet, counted once per article.

    Keyed (outlet, paragraph hash) - see BOILERPLATE_MIN_ARTICLES for why the
    outlet is part of the key.
    """
    seen: dict[tuple[str, str], int] = {}
    for row in results:
        outlet = row["outlet"]
        for digest in set(row.get("para_hashes") or []):
            key = (outlet, digest)
            seen[key] = seen.get(key, 0) + 1
    return {k: n for k, n in seen.items() if n >= BOILERPLATE_MIN_ARTICLES}


def adjusted_coverage(row: dict, boilerplate: set[tuple[str, str]]) -> float | None:
    """Coverage with recurring boilerplate removed from BOTH sides.

    Removed from the denominator as well as the numerator: an article whose only
    unreached paragraphs are the site's advertising blurb has lost no prose, and
    should score 1.0 rather than 0.8.
    """
    outlet = row["outlet"]
    paras = [d for d in (row.get("para_hashes") or [])
             if (outlet, d) not in boilerplate]
    if not paras:
        return None
    lost = [d for d in (row.get("lost_hashes") or [])
            if (outlet, d) not in boilerplate]
    return round((len(paras) - len(lost)) / len(paras), 4)


def summarise(results: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    for row in results:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1

    boilerplate = find_boilerplate(results)
    samples: dict[tuple[str, str], str] = {}
    for row in results:
        outlet = row["outlet"]
        for digest, text in (row.get("lost_samples") or {}).items():
            key = (outlet, digest)
            if key in boilerplate and key not in samples:
                samples[key] = text
    # Re-decide SUSPECT now that boilerplate is known. audit_one flags on RAW
    # coverage because it sees one article at a time and cannot know what
    # recurs; recurrence is only visible across the corpus. Both directions
    # matter: an article whose only losses were the ad blurb is not suspect,
    # and an article that looked fine on raw coverage can become suspect once
    # the boilerplate is taken out of its denominator.
    boiler_set = set(boilerplate)
    for row in results:
        row["prose_coverage_adjusted"] = adjusted_coverage(row, boiler_set)
        coverage = row.get("prose_coverage_adjusted")
        if row["status"] not in ("ok", "suspect") or coverage is None:
            continue
        row["errors"] = [e for e in row.get("errors", [])
                         if "age-gate/truncation signature" not in e]
        suspect = (coverage < COVERAGE_FLOOR and row.get("has_title")
                   and row.get("archive_paragraphs", 0) >= 3)
        row["status"] = "suspect" if suspect else "ok"
        if suspect:
            row["errors"].append(
                f"prose coverage {coverage:.0%} (boilerplate excluded) with a "
                f"clean title - the age-gate/truncation signature")

    by_status = {}
    for row in results:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1

    # Per outlet on the ADJUSTED figure. Reporting raw here while reporting
    # adjusted globally was a real defect in an earlier version of this file:
    # per-outlet is exactly where the comparison is made, and raw coverage is
    # dominated by each site's own furniture.
    grouped: dict[str, list[float]] = {}
    raw_grouped: dict[str, list[float]] = {}
    blocks_grouped: dict[str, list[float]] = {}
    for row in results:
        if row.get("prose_coverage_adjusted") is not None:
            grouped.setdefault(row["outlet"], []).append(
                row["prose_coverage_adjusted"])
        if row.get("prose_coverage") is not None:
            raw_grouped.setdefault(row["outlet"], []).append(
                row["prose_coverage"])
        if row.get("blocks_with_text") is not None:
            blocks_grouped.setdefault(row["outlet"], []).append(
                float(row["blocks_with_text"]))
    per_outlet = {
        outlet: {
            "articles": len(values),
            "median": percentile(values, 0.5),
            "p10": percentile(values, 0.10),
            "worst": percentile(values, 0.0),
            "median_raw": percentile(raw_grouped.get(outlet, []), 0.5),
            # The disambiguator. Coverage near zero WITH a healthy block count
            # means the body was extracted from somewhere this measure cannot
            # see - the ng-state JSON blob on the Angular outlets - and the
            # figure is not comparable. Coverage near zero WITH one block is
            # truncation, and it is real.
            "median_blocks": percentile(blocks_grouped.get(outlet, []), 0.5),
        }
        for outlet, values in sorted(grouped.items())
    }
    # From raw_grouped, NOT grouped: `grouped` holds the ADJUSTED values, and
    # drawing the raw figure from it made both lines of the report print the
    # same number while claiming to contrast them.
    everything = [v for values in raw_grouped.values() for v in values]
    adj = [r["prose_coverage_adjusted"] for r in results
           if r.get("prose_coverage_adjusted") is not None]
    mins = [r["min_block_overlap"] for r in results
            if r.get("min_block_overlap") is not None]
    return {
        "articles": len(results),
        # Coverage is undefined for an article whose every archive paragraph was
        # boilerplate - there is nothing left to have lost - so it drops out of
        # the coverage statistics. Reported, so the shrinking n is not a mystery.
        "articles_measurable": len(adj),
        "by_status": dict(sorted(by_status.items())),
        "prose_coverage": {
            "median": percentile(everything, 0.5),
            "p10": percentile(everything, 0.10),
            "p01": percentile(everything, 0.01),
        },
        "fidelity_headroom": {
            "articles": len(mins),
            "floor": FIDELITY_FLOOR,
            "min": percentile(mins, 0.0),
            "p01": percentile(mins, 0.01),
            "p10": percentile(mins, 0.10),
            "median": percentile(mins, 0.5),
        },
        "boilerplate": {
            "min_articles": BOILERPLATE_MIN_ARTICLES,
            "paragraph_types": len(boilerplate),
            "scope": "per outlet",
            "paragraph_types_by_outlet": len(boilerplate),
            "worst_offenders": [
                {"outlet": key[0], "articles": n,
                 "text": samples.get(key, "(no sample)")}
                for key, n in sorted(boilerplate.items(),
                                     key=lambda kv: -kv[1])[:6]
            ],
        },
        "prose_coverage_adjusted": {
            "median": percentile(adj, 0.5),
            "p10": percentile(adj, 0.10),
            "p01": percentile(adj, 0.01),
        },
        "per_outlet": per_outlet,
    }


def run_section_a(args: argparse.Namespace) -> dict:
    manifest = load_manifest(args.manifest) if args.manifest else {}
    root = args.root

    directories = sorted(p.parent for p in root.rglob("content.json"))
    if manifest:
        directories = [d for d in directories if d.name in manifest]
    if args.limit:
        directories = directories[:args.limit]

    print(f"== section A: chain of custody over {len(directories)} article(s)")
    if manifest:
        print(f"   manifest {args.manifest} "
              f"({len(manifest)} archives, {len(directories)} extracted)")

    stream = args.out.with_suffix(".jsonl") if args.out else None
    handle = stream.open("w", encoding="utf-8") if stream else None
    results = []
    started = time.monotonic()
    try:
        for index, directory in enumerate(directories, 1):
            outlet = directory.parent.parent.name
            row = manifest.get(directory.name)
            archive = wacz_for(directory, args.pages_root, outlet, row)
            result = audit_one(directory, archive)
            if row:
                result["stratum"] = row.get("stratum")
            results.append(result)
            if handle:
                handle.write(json.dumps(result, ensure_ascii=False) + "\n")
                handle.flush()
            if index % 50 == 0 or index == len(directories):
                rate = index / max(time.monotonic() - started, 1e-9)
                print(f"   {index}/{len(directories)}  {rate:.1f}/s", flush=True)
    finally:
        if handle:
            handle.close()

    summary = summarise(results)
    summary["elapsed_seconds"] = round(time.monotonic() - started, 1)
    summary["manifest"] = str(args.manifest) if args.manifest else None
    summary["root"] = str(root)
    if manifest:
        summary["extraction_coverage"] = {
            "in_manifest": len(manifest),
            "extracted": len(directories),
            "missing": len(manifest) - len(directories),
        }
    return {"section_a": summary, "results": results}


def report_section_a(summary: dict) -> int:
    print("\n== STATUS")
    for status, count in summary["by_status"].items():
        print(f"   {status:<20} {count:>6}")

    head = summary["fidelity_headroom"]
    print("\n== FIDELITY HEADROOM  (weakest block per article, vs the floor)")
    print(f"   floor {head['floor']}   worst {head['min']}   p01 {head['p01']}"
          f"   p10 {head['p10']}   median {head['median']}")
    if head["p01"] is not None and head["p01"] < head["floor"] + 0.05:
        print("   NOTE  real content sits within 0.05 of the floor: the floor is"
              " guesswork, not a measurement")

    boiler = summary["boilerplate"]
    print(f"\n== RECURRING BOILERPLATE  {boiler['paragraph_types']} "
          f"(outlet, paragraph) pair(s) seen in >= {boiler['min_articles']} "
          f"articles of the SAME outlet, excluded from coverage")
    for row in boiler["worst_offenders"]:
        print(f"   {row['outlet']:<16} {row['articles']:>4} articles  "
              f"{row['text'][:70]}")

    raw = summary["prose_coverage"]
    adj = summary["prose_coverage_adjusted"]
    print(f"\n   {summary['articles_measurable']} of {summary['articles']} "
          f"articles have measurable coverage; the rest had only boilerplate "
          f"paragraphs in the DOM")
    print("\n== PROSE COVERAGE  (a comparative signal, not a verdict -"
          " see the module docstring)")
    print(f"   raw       median {raw['median']}   p10 {raw['p10']}"
          f"   p01 {raw['p01']}")
    print(f"   adjusted  median {adj['median']}   p10 {adj['p10']}"
          f"   p01 {adj['p01']}   <- boilerplate removed from both sides")

    print("\n== PER OUTLET, adjusted coverage, worst median first")
    print("   blocks= median text blocks extracted. Low coverage WITH a healthy"
          " block count means the")
    print("   body came from the ng-state blob, which this measure cannot see -"
          " not lost prose.")
    ranked = sorted(summary["per_outlet"].items(),
                    key=lambda kv: (kv[1]["median"] is None, kv[1]["median"]))
    for outlet, stats in ranked:
        flag = ""
        if (stats["median"] is not None and stats["median"] < 0.5
                and (stats["median_blocks"] or 0) >= 5):
            flag = "   <- not comparable (ng-state)"
        elif (stats["median"] is not None and stats["median"] < 0.5
                and (stats["median_blocks"] or 0) <= 2):
            flag = "   <- TRUNCATION"
        print(f"   {outlet:<18} n={stats['articles']:<4} "
              f"median={stats['median']:<7} p10={stats['p10']:<7} "
              f"raw={stats['median_raw']:<7} blocks={stats['median_blocks']}"
              f"{flag}")

    if "extraction_coverage" in summary:
        cov = summary["extraction_coverage"]
        print(f"\n== EXTRACTION COVERAGE  {cov['extracted']}/{cov['in_manifest']}"
              f" archives produced output, {cov['missing']} missing")

    hard = sum(summary["by_status"].get(s, 0) for s in
               ("fidelity", "archive_unreadable", "archive_error",
                "no_html_record", "archive_unparseable", "no_archive",
                "unreadable_output"))
    return 1 if hard else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--section", default="A", choices=["A"],
                        help="only A exists so far")
    parser.add_argument("--root", type=Path, required=True,
                        help="extractor output tree to audit")
    parser.add_argument("--pages-root", type=Path,
                        default=Path("/mnt/hdd/c0cshf/causalia/pages"),
                        help="corpus root, to locate each page.wacz")
    parser.add_argument("--manifest", type=Path,
                        help="sample_corpus.py manifest; restricts and labels")
    parser.add_argument("--limit", type=int, help="audit at most N articles")
    parser.add_argument("-o", "--out", type=Path,
                        help="JSON report (a .jsonl stream is written beside it)")
    args = parser.parse_args(argv[1:])

    if not args.root.is_dir():
        raise SystemExit(f"--root is not a directory: {args.root}")

    report = run_section_a(args)
    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        print(f"\n== wrote {args.out}")
    return report_section_a(report["section_a"])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
