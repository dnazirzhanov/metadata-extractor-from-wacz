#!/usr/bin/env python3
"""
load.py
=======
The loader: extracted article directories -> the corpus schema.

    python -m ingest.load --outlet mandiner.hu \
        --output /mnt/hdd/c0cshf/mandiner-extract --limit 1000

WHAT IT FIXES ABOUT scripts/ingest_dev.py
-----------------------------------------
* **The frontier.** ingest_dev did `sorted(root.rglob("content.json"))`, which
  materialises and sorts every article directory before the first INSERT, keys
  on the wrong file, and cannot resume. Here the frontier is a ledger claim.
* **The marker.** `content.json` is written five artifacts before
  `extraction.json`, so a worker killed in between leaves a directory the walker
  accepted and the loader died on - measured at 5 of 46,253 directories on the
  benchmark output. Here every directory goes through `read_marker`, which
  checks the commit marker AND the manifest of artifacts it promises.
* **The transaction shape.** One transaction per article cost 71% of ingest
  runtime (1,438.3 s against 417.0 s over the same 20,000 articles). Here
  commits are batched with a SAVEPOINT per article, which is what makes batching
  safe: without it one bad article rolls back up to 99 already-loaded ones.
* **The quality gate.** An `invalid` extraction is never claimed and never
  loaded, so it can never become searchable.

ATOMICITY
---------
`corpus.extraction_task.ingest_state = 'ingested'` is written inside the SAME
transaction as the article rows, so the two commit or roll back together. A
crash between them is impossible; a crash before the commit leaves the task
claimed, its lease expires, and the article is loaded again - which is safe,
because ingestion is idempotent by upsert-then-flip.
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

from causalia_extractor import EXTRACTOR_NAME, __version__      # noqa: E402
from cx_ingest import (                                          # noqa: E402
    CaptureNotFound, IncompleteExtraction, UnusableExtraction, ingest,
    resolve_capture)
from ingest import ledger                                        # noqa: E402
from ingest.extract_worker import HEARTBEAT_SECONDS, Heartbeat   # noqa: E402

log = logging.getLogger("load")

EXTRACTOR_VERSION = f"{EXTRACTOR_NAME}/{__version__}"

#: Measured: commit-every-1 took 1,438.3 s over 20,000 articles, commit-every-100
#: took 417.0 s. Beyond 100 the curve is flat and the rollback blast radius grows.
DEFAULT_COMMIT_EVERY = 100


class Failure(Exception):
    """An article that could not be loaded, carrying why."""


def _capture_for(cur, task: ledger.Task):
    """(archive_row_id, wacz_sha256) for this task.

    Prefers what the ledger recorded when the task was seeded: that is the
    capture row this work item is ABOUT, and using it means no per-article
    lookup at all. Falls back to a lookup when the ledger has no copy, which is
    the case for tasks seeded before the archive row existed.
    """
    if task.archive_row_id is not None:
        return task.archive_row_id, task.wacz_sha256
    return resolve_capture(cur, task.url_hash)


def load_batch(connection, tasks, output_root: Path, *, commit_every: int,
               beat=None):
    """Load a claimed batch. Returns (loaded, [(task, reason), ...]).

    ``beat`` is the heartbeat, and it is told what is left as the batch goes.
    Without that the loader kept beating articles it had already finished - and
    in the 50k rung one of those had been failed, released and re-claimed by
    another loader in the meantime, so two workers' heartbeats and batches were
    touching the same ledger rows. That is half of what produced 408 deadlocks.
    """
    remaining = {t.key for t in tasks}
    loaded = 0
    failures: list[tuple[ledger.Task, str]] = []
    pending_in_tx = 0

    with connection.cursor() as cur:
        for task in tasks:
            directory = output_root / (task.output_dir or task.relative_dir())
            cur.execute("SAVEPOINT article")
            try:
                capture = _capture_for(cur, task)
                ingest(cur, directory, capture=capture)
                ledger.mark_ingested(cur, task)
                cur.execute("RELEASE SAVEPOINT article")
                loaded += 1
                pending_in_tx += 1
                remaining.discard(task.key)
                if beat is not None:
                    beat.keys = list(remaining)
            except (IncompleteExtraction, UnusableExtraction, CaptureNotFound) as exc:
                # A refusal, not a crash: the directory is not loadable, and
                # saying so is the correct outcome.
                cur.execute("ROLLBACK TO SAVEPOINT article")
                failures.append((task, f"{type(exc).__name__}: {exc}"))
                remaining.discard(task.key)
            except Exception as exc:                            # noqa: BLE001
                cur.execute("ROLLBACK TO SAVEPOINT article")
                failures.append((task, f"{type(exc).__name__}: {exc}"))
                remaining.discard(task.key)

            if pending_in_tx >= commit_every:
                connection.commit()
                pending_in_tx = 0

    connection.commit()
    return loaded, failures


def run(args) -> int:
    dsn = args.dsn or os.environ.get("CX_INGEST_DSN")
    if not dsn:
        print("no DSN: pass --dsn or set CX_INGEST_DSN", file=sys.stderr)
        return 2

    worker_id = ledger.worker_identity("load")
    connection = psycopg2.connect(dsn)
    beat = Heartbeat(dsn, HEARTBEAT_SECONDS, worker_id=worker_id)
    beat.start()

    stopping = threading.Event()

    def _stop(signum, _frame):
        log.info("signal %s: finishing this batch, then releasing", signum)
        stopping.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    output_root = Path(args.output)
    loaded = failed = 0
    started = time.monotonic()

    try:
        while not stopping.is_set():
            if args.limit and loaded + failed >= args.limit:
                break
            batch = args.batch
            if args.limit:
                batch = min(batch, args.limit - loaded - failed)
            tasks = ledger.claim_ingest(
                connection, outlet=args.outlet,
                extractor_version=EXTRACTOR_VERSION,
                worker_id=worker_id, batch=batch)
            if not tasks:
                if args.once:
                    break
                log.info("nothing to claim; sleeping %ss", args.idle_sleep)
                if stopping.wait(args.idle_sleep):
                    break
                continue

            beat.keys = [t.key for t in tasks]
            ok, failures = load_batch(connection, tasks, output_root,
                                      commit_every=args.commit_every, beat=beat)
            loaded += ok
            beat.keys = []

            # AFTER the batch transaction ended: a failure has to survive the
            # rollback that produced it, so it cannot be written inside it.
            for task, reason in failures:
                ledger.fail_ingest(connection, task, error=reason)
                failed += 1
                log.warning("FAILED %s %s", task.url_hash[:12], reason[:160])
    finally:
        beat.stop()
        if beat.keys:
            released = ledger.release(connection, beat.keys, stage="ingest")
            log.info("released %d claim(s)", released)
        with connection.cursor() as cur:
            state = ledger.summary(cur, outlet=args.outlet)
        connection.close()

    elapsed = time.monotonic() - started
    rate = loaded / elapsed if elapsed else 0
    print(f"\n== loaded {loaded}, {failed} failed in {elapsed:.1f}s "
          f"({rate:.1f} articles/s)")
    print("== ledger: " + "  ".join(f"{k}={v}" for k, v in state.items()))
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="load", description="Load extracted articles into the corpus schema.")
    parser.add_argument("--outlet", required=True)
    parser.add_argument("--output", required=True, type=Path,
                        help="the extraction output root")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--batch", type=int, default=500,
                        help="tasks per claim (default 500)")
    parser.add_argument("--commit-every", type=int, default=DEFAULT_COMMIT_EVERY)
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
