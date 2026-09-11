"""
cli.py
======
The command line.

    causalia-extractor extract --input /path/to/page.wacz --output /path/to/out/

``--input`` may be a single .wacz, one article directory, a shard, an outlet, or
a whole PAGES_ROOT; every ``page.wacz`` beneath it is processed in a
deterministic order. Nothing is hardcoded to one machine: the default input root
comes from ``CAUSALIA_PAGES_ROOT`` and the output path is always explicit.

The extractor never crawls, never opens a database connection, never writes into
the corpus, and never deletes an archive. ``--output`` is a separate tree by
design.

Exit codes:
    0   every article extracted (success or partial)
    1   at least one article failed
    2   an archive changed underneath us, or an unsafe artifact was refused
    130 interrupted
"""

from __future__ import annotations

import argparse
import itertools
import logging
import sys
from collections import Counter
from pathlib import Path

from . import __version__
from .identity import PAGES_ROOT, iter_wacz_files
from .output import ArchiveMutated, UnsafeArtifact
from .pipeline import (ALL_STAGES, STAGE_CONTENT, STAGE_SCREENSHOT,
                       STATUS_FAILED, extract, extract_screenshot)

log = logging.getLogger("causalia_extractor")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="causalia-extractor",
        description="Extract evidence-anchored article artifacts from Browsertrix "
                    "WACZ archives. Reads the archive; never crawls, never writes "
                    "to the database, never modifies the archive.")
    parser.add_argument("--version", action="version",
                        version=f"causalia-extractor {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser(
        "extract", help="extract one archive or a tree of them")
    run.add_argument("--input", type=Path, default=PAGES_ROOT, metavar="PATH",
                     help="a page.wacz, an article dir, a shard, an outlet, or "
                          f"a pages root (default: {PAGES_ROOT})")
    run.add_argument("--output", type=Path, required=True, metavar="DIR",
                     help="where to write article directories")
    run.add_argument("--outlet", metavar="HOST",
                     help="restrict a tree walk to one outlet, e.g. ripost.hu")
    run.add_argument("--limit", type=int, default=0,
                     help="process at most N archives (0 = no limit)")
    run.add_argument("--copy-wacz", action="store_true",
                     help="copy page.wacz into the output dir (off by default: "
                          "the corpus is ~30 TB and the source path is recorded)")
    run.add_argument("--stages", default=",".join(ALL_STAGES), metavar="LIST",
                     help="comma-separated stages to run (default: %(default)s). "
                          "`content` alone writes no screenshot and is ~1.48x "
                          "faster; `screenshot` alone backfills one without "
                          "re-reading the article")
    run.add_argument("--force", action="store_true",
                     help="sweep this extractor's own prior artifacts before "
                          "re-extracting (never touches a screenshot or anything "
                          "outside the artifact allowlist)")
    run.add_argument("--dry-run", action="store_true",
                     help="run the full pipeline and every safety check, write nothing")
    run.add_argument("--log-level", default="INFO",
                     choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def _print_summary(results) -> None:
    statuses = Counter(result.status for result in results)
    print("\n%d archive(s) processed" % len(results))
    for status, count in sorted(statuses.items()):
        print("  %-8s %d" % (status, count))

    if not results:
        return

    totals = Counter()
    for result in results:
        totals.update(result.counts)
    if totals:
        print("  totals   " + "  ".join(
            "%s=%d" % (key, totals[key]) for key in sorted(totals)))

    durations = [result.duration_ms for result in results]
    print("  time     mean=%dms max=%dms" % (
        sum(durations) // len(durations), max(durations)))

    reasons = Counter()
    for result in results:
        for warning in result.warnings:
            # Collapse the URL/detail after the first colon so the histogram
            # counts causes rather than instances.
            reasons[warning.split(":")[0].strip()] += 1
    if reasons:
        print("  warnings")
        for reason, count in reasons.most_common(12):
            print("    %4d  %s" % (count, reason))

    failed = [result for result in results if result.status == STATUS_FAILED]
    for result in failed[:25]:
        print("  FAILED %s\n         %s" % (result.wacz_path, result.error))


def _screenshot_only(args, archives) -> int:
    """--stages screenshot: the backfill pass, reported on its own terms."""
    counts = Counter()
    for wacz_path in archives:
        result = extract_screenshot(wacz_path, args.output, dry_run=args.dry_run)
        counts[result.state] += 1
        log.info("%-16s %s", result.state, result.output_dir or result.wacz_path)
    print("\n%d archive(s) processed" % sum(counts.values()))
    for state, count in sorted(counts.items()):
        print("  %-16s %d" % (state, count))
    return 1 if counts.get("failed") else 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s")

    # Consumed lazily: enumerating the whole corpus first would mean walking
    # 4.1M article directories before extracting anything, and over a network
    # mount that alone takes minutes. --limit must short-circuit the walk.
    archives = iter_wacz_files(args.input, outlet=args.outlet)
    if args.limit:
        archives = itertools.islice(archives, args.limit)

    stages = tuple(part.strip() for part in args.stages.split(",") if part.strip())
    unknown = [stage for stage in stages if stage not in ALL_STAGES]
    if unknown:
        print("unknown stage(s): %s (known: %s)" % (", ".join(unknown),
                                                    ", ".join(ALL_STAGES)),
              file=sys.stderr)
        return 2
    if not stages:
        print("no stages selected", file=sys.stderr)
        return 2

    # SCREENSHOT ALONE IS A DIFFERENT JOB, not a narrower extraction: it reads
    # the archive without buffering media, writes one file, and deliberately
    # leaves extraction.json alone so the content extraction's manifest keeps
    # describing the content extraction.
    if stages == (STAGE_SCREENSHOT,):
        return _screenshot_only(args, archives)

    log.info("extracting into %s", args.output)
    results = []
    try:
        for wacz_path in archives:
            try:
                result = extract(wacz_path, args.output, dry_run=args.dry_run,
                                 copy_wacz=args.copy_wacz, force=args.force,
                                 stages=stages)
            except (ArchiveMutated, UnsafeArtifact) as exc:
                # Both mean the run cannot be trusted to continue.
                print("FATAL %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
                _print_summary(results)
                return 2
            results.append(result)
            log.info("%-8s %s", result.status,
                     result.output_dir or result.wacz_path)
    except KeyboardInterrupt:
        _print_summary(results)
        return 130

    if not results:
        print("no .wacz found under %s" % args.input, file=sys.stderr)
        return 1

    _print_summary(results)
    return 1 if any(r.status == STATUS_FAILED for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
