#!/usr/bin/env python3
"""Ingest extractor output into the DEVELOPMENT database.

Read-only over the extractor output: it opens the JSON and HTML the extractor
wrote and nothing else. No .wacz is touched, the crawler is not run.

One transaction per article (cx_ingest.ingest), so a failure leaves the database
holding whole articles rather than half of one. Running this twice on the same
directory is the re-ingestion test - the schema's insert-then-flip makes the
second pass supersede the first rather than duplicate it.

Usage:  scripts/ingest_dev.py <output-dir> [--dsn DSN] [--limit N]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cx_ingest import ingest                                   # noqa: E402

DEFAULT_DSN = ("host=127.0.0.1 port=55433 user=causalia password=dev "
               "dbname=causalia_dev")

TABLES = ("article", "article_extraction", "content_block", "article_image",
          "article_video", "article_link", "article_artifact",
          "passage_reference")


def counts(cur) -> dict[str, int]:
    out = {}
    for table in TABLES:
        cur.execute(f"SELECT count(*) FROM corpus.{table}")
        out[table] = cur.fetchone()[0]
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--dsn", default=os.environ.get("CX_DEV_DSN", DEFAULT_DSN))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv[1:])

    root = args.output_dir
    if not root.is_dir():
        print(f"no such output directory: {root}", file=sys.stderr)
        return 1

    # An article directory is one holding content.json. rglob so an outlet, a
    # shard or a whole output root all work as input.
    directories = sorted(p.parent for p in root.rglob("content.json"))
    if args.limit:
        directories = directories[:args.limit]
    if not directories:
        print(f"no article directories under {root}", file=sys.stderr)
        return 1

    connection = psycopg2.connect(args.dsn)
    with connection.cursor() as cur:
        before = counts(cur)
    pass_number = 1 if before["article_extraction"] == 0 else \
        before["article_extraction"] // max(before["article"], 1) + 1

    print(f"== ingesting {len(directories)} article directories from {root}")
    print(f"== pass {pass_number} (database already holds {before['article']} articles)")

    skipped = failures = 0
    started = time.monotonic()
    per_article = []
    for directory in directories:
        t0 = time.monotonic()
        try:
            with connection:                       # one transaction per article
                with connection.cursor() as cur:
                    _article_id, _extraction_id, synthetic = ingest(cur, directory)
                    skipped += synthetic
        except Exception as exc:                   # noqa: BLE001 - report, continue
            failures += 1
            print(f"   FAILED {directory.name[:12]}: "
                  f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}")
            continue
        elapsed = time.monotonic() - t0
        per_article.append(elapsed)
        if not args.quiet:
            print(f"   {elapsed*1000:7.1f} ms  {directory.parent.parent.name}/"
                  f"{directory.name[:12]}")
    total = time.monotonic() - started

    with connection.cursor() as cur:
        after = counts(cur)
    connection.close()

    print(f"\n== rows ({len(directories)} directories, pass {pass_number})")
    for table in TABLES:
        delta = after[table] - before[table]
        print("   %-20s %6d  (%+d)" % (table, after[table], delta))

    print(f"\n   {'links skipped':<20} {skipped}", end="")
    print(" (none - links.py filters them at the source)" if not skipped
          else " (embed fallback anchors: PRE-FIX output)")
    if failures:
        print(f"   {'FAILED articles':<20} {failures}")
    if per_article:
        per_article.sort()
        print(f"\n== timing")
        print(f"   total            {total:.2f} s")
        print(f"   mean per article {sum(per_article)/len(per_article)*1000:.1f} ms")
        print(f"   median           {per_article[len(per_article)//2]*1000:.1f} ms")
        print(f"   max              {per_article[-1]*1000:.1f} ms")
        print(f"   rate             {len(per_article)/total:.1f} articles/s")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
