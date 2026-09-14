#!/usr/bin/env python3
"""
screenshot_worker.py
====================
The screenshot backfill. Lifts one file out of each capture and records it,
without re-reading the article.

    python -m ingest.screenshot_worker --outlet mandiner.hu \
        --output /mnt/hdd/c0cshf/mandiner-extract

WHY THIS EXISTS
---------------
Screenshots are strategically required and are never removed from the
architecture. They are also 90.2% of the bytes an extraction writes and ~1.48x
of its sustained throughput, because on this hardware a long run is bound by
write bandwidth. So an urgent run can be extracted with `--stages content` and
the screenshots filled in later, from the same archives - the .wacz is the
source of truth and nothing is lost by deferring.

WHAT IT DOES NOT DO
-------------------
* It never creates a new `article_extraction`. Nothing about the READING of the
  article changed, and a new reading would supersede the one every existing
  citation was verified against.
* It never deletes a screenshot. The artifact sweep walks the allowlist, and
  `screenshot.*` is not in it - there is a test that keeps that true.
* It does not rewrite `extraction.json`. That marker describes the content
  extraction and its manifest; this pass is a separate fact, recorded in
  corpus.extraction_task.screenshot_state and corpus.article_artifact.

WHAT IT COSTS
-------------
`read_archive` is 45% of a full extraction's 908 ms, so a backfill pass is
roughly 0.56 of an extraction pass plus the bytes it writes. Deferring buys
time-to-searchable, not total machine time.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from causalia_extractor import EXTRACTOR_NAME, __version__       # noqa: E402
from causalia_extractor.pipeline import extract_screenshot       # noqa: E402
from cx_ingest import attach_screenshot                          # noqa: E402
from ingest import ledger                                        # noqa: E402
from ingest.extract_worker import HEARTBEAT_SECONDS, Heartbeat   # noqa: E402

log = logging.getLogger("screenshot_worker")

EXTRACTOR_VERSION = f"{EXTRACTOR_NAME}/{__version__}"


def run(args) -> int:
    dsn = args.dsn or os.environ.get("CX_INGEST_DSN")
    if not dsn:
        print("no DSN: pass --dsn or set CX_INGEST_DSN", file=sys.stderr)
        return 2

    worker_id = ledger.worker_identity("screenshot")
    connection = psycopg2.connect(dsn)
    beat = Heartbeat(dsn, HEARTBEAT_SECONDS)
    beat.start()

    stopping = threading.Event()

    def _stop(signum, _frame):
        log.info("signal %s: finishing the current archive, then releasing", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    output_root = Path(args.output)
    pages_root = Path(args.pages_root)
    written = absent = failed = 0
    started = time.monotonic()

    try:
        while not stopping.is_set():
            if args.limit and written + absent + failed >= args.limit:
                break
            batch = args.batch
            if args.limit:
                batch = min(batch, args.limit - written - absent - failed)
            tasks = ledger.claim_screenshot(
                connection, outlet=args.outlet,
                extractor_version=EXTRACTOR_VERSION,
                worker_id=worker_id, batch=batch)
            if not tasks:
                if args.once:
                    break
                if stopping.wait(args.idle_sleep):
                    break
                continue

            beat.keys = [t.key for t in tasks]
            for task in tasks:
                if stopping.is_set():
                    break
                wacz = task.wacz_path(pages_root)
                if not wacz.is_file():
                    ledger.set_screenshot_state(connection, task, "failed")
                    failed += 1
                    continue

                result = extract_screenshot(wacz, output_root)
                if result.state == "present":
                    directory = output_root / task.relative_dir()
                    with connection.cursor() as cur:
                        attach_screenshot(cur, task.url_hash, directory,
                                          task.relative_dir())
                    connection.commit()
                    written += 1
                elif result.state == "none_in_archive":
                    absent += 1
                else:
                    failed += 1
                    log.warning("%s: %s", task.url_hash[:12], result.error)
                ledger.set_screenshot_state(connection, task, result.state)
            beat.keys = []
    finally:
        beat.stop()
        if beat.keys:
            ledger.release(connection, beat.keys, stage="screenshot")
        with connection.cursor() as cur:
            state = ledger.summary(cur, outlet=args.outlet)
        connection.close()

    elapsed = time.monotonic() - started
    total = written + absent + failed
    print(f"\n== {written} written, {absent} with none in the archive, "
          f"{failed} failed in {elapsed:.1f}s "
          f"({total / elapsed if elapsed else 0:.2f}/s)")
    print("== ledger: " + "  ".join(f"{k}={v}" for k, v in state.items()))
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="screenshot_worker",
        description="Backfill screenshots for already-extracted articles.")
    parser.add_argument("--outlet", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pages-root", type=Path,
                        default=os.environ.get("CAUSALIA_PAGES_ROOT",
                                               "/mnt/hdd/c0cshf/causalia/pages"))
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--batch", type=int, default=32)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--idle-sleep", type=int, default=30)
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(levelname)s %(name)s: %(message)s")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
