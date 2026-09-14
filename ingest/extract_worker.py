#!/usr/bin/env python3
"""
extract_worker.py
=================
A resumable extraction worker. Claims work from the ledger, extracts it, records
the verdict, and hands back anything it is still holding when told to stop.

    python -m ingest.extract_worker --outlet mandiner.hu \
        --output /mnt/hdd/c0cshf/mandiner-extract --limit 1000

ONE PROCESS IS ONE WORKER. Concurrency is N processes against the same ledger,
which is how the archiver runs and how the 2026-09-10 benchmark was measured -
the claim is what makes them disjoint, not a thread pool. That also means a
crash takes down one worker's lease, not a pool's.

WHAT MAKES IT RESUMABLE
-----------------------
The frontier is `corpus.extraction_task`, not a filesystem walk. Restarting
after any interruption picks up where the ledger says the work stopped; nothing
re-walks 418,343 directories, and nothing is extracted twice because a claim is
exclusive.

WHAT HAPPENS WHEN IT DIES
-------------------------
* SIGTERM/SIGINT: the current article finishes, remaining claims are released
  immediately, the process exits 0. Nothing waits for a lease.
* Killed outright: the heartbeat stops, and `reclaim_stale` returns the claims
  to `pending` once the lease expires. The attempt is not given back.
* Either way the output directory is left as it is. It is never accepted by the
  loader without a complete commit marker, so a half-written article cannot be
  ingested - it is simply re-extracted over.
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

from causalia_extractor import EXTRACTOR_NAME, __version__      # noqa: E402
from causalia_extractor.output import ArchiveMutated, UnsafeArtifact  # noqa: E402
from causalia_extractor.pipeline import ALL_STAGES, extract     # noqa: E402
from ingest import ledger                                       # noqa: E402

log = logging.getLogger("extract_worker")

EXTRACTOR_VERSION = f"{EXTRACTOR_NAME}/{__version__}"

#: Beat well inside the lease. Five beats' grace before a live worker could be
#: declared dead.
HEARTBEAT_SECONDS = 30


class Heartbeat:
    """Stamps heartbeat_at on the tasks this worker holds, on its own connection.

    Its own, because the main connection is busy inside a claim or an ingest
    transaction and a beat must not have to wait for it - the archiver learned
    the same thing (causalia-final/causalia/worker.py).
    """

    def __init__(self, dsn: str, interval: int = HEARTBEAT_SECONDS,
                 worker_id: str | None = None):
        self.dsn = dsn
        self.interval = interval
        # Beat only rows this worker still owns - see ledger.heartbeat.
        self.worker_id = worker_id
        self.keys: list[tuple[str, str]] = []
        self.missed = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="heartbeat",
                                        daemon=True)
        self._thread.start()

    def _loop(self):
        connection = None
        while not self._stop.wait(self.interval):
            if not self.keys:
                continue
            try:
                if connection is None or connection.closed:
                    connection = psycopg2.connect(self.dsn)
                ledger.heartbeat(connection, list(self.keys),
                                 worker_id=self.worker_id)
                self.missed = 0
            except Exception as exc:                    # noqa: BLE001
                self.missed += 1
                log.warning("heartbeat failed (%d in a row): %s", self.missed, exc)
                connection = None
        if connection is not None and not connection.closed:
            connection.close()

    def stop(self):
        self._stop.set()


def resolve_wacz(task: ledger.Task, pages_root: Path) -> Path:
    """Where this task's capture actually is on THIS machine.

    `archives.wacz_path` is absolute and encodes milab2's mount point, while the
    same corpus is at a different path over sshfs on milab4 and somewhere else
    again on a laptop. The trailing `<outlet>/<h2>/<hash>/page.wacz` is stable,
    so the path is rebuilt from the identity rather than trusted verbatim.
    """
    return task.wacz_path(pages_root)


# A missing screenshot is never a reason to reject an article, so it is recorded
# on its own column. `present`, `none_in_archive` (the capture genuinely holds
# none - normal for the pre-2026-08-12 cohort) and `skipped` (the stage was not
# run) are three different facts, and a backfill needs to tell them apart:
# `skipped` is work to do, `none_in_archive` is not.


def run(args) -> int:
    dsn = args.dsn or os.environ.get("CX_INGEST_DSN")
    if not dsn:
        print("no DSN: pass --dsn or set CX_INGEST_DSN", file=sys.stderr)
        return 2

    worker_id = ledger.worker_identity("extract")
    connection = psycopg2.connect(dsn)
    beat = Heartbeat(dsn, worker_id=worker_id)
    beat.start()

    stopping = threading.Event()

    def _stop(signum, _frame):
        log.info("signal %s: finishing the current article, then releasing",
                 signum)
        stopping.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    output_root = Path(args.output)
    pages_root = Path(args.pages_root)
    done = failed = invalid = 0
    started = time.monotonic()

    try:
        while not stopping.is_set():
            if args.limit and done + failed >= args.limit:
                break
            batch = args.batch
            if args.limit:
                batch = min(batch, args.limit - done - failed)
            tasks = ledger.claim_extract(
                connection, outlet=args.outlet,
                extractor_version=EXTRACTOR_VERSION,
                worker_id=worker_id, batch=batch,
                max_attempts=args.max_attempts)
            if not tasks:
                if args.once:
                    break
                log.info("nothing to claim; sleeping %ss", args.idle_sleep)
                if stopping.wait(args.idle_sleep):
                    break
                continue

            # `held` is what this worker still owes an answer for. It shrinks as
            # each task is resolved, so a SIGTERM part-way through a batch
            # releases exactly the untouched remainder.
            #
            # THIS USED TO BE A BUG: beat.keys was cleared unconditionally after
            # the loop, including when the loop broke early on a stop signal, so
            # the finally block found nothing to release and the untouched
            # claims sat until their lease expired - which is the slow path the
            # graceful stop exists to avoid.
            held = {t.key for t in tasks}
            beat.keys = list(held)
            for task in tasks:
                if stopping.is_set():
                    break
                wacz = resolve_wacz(task, pages_root)
                if not wacz.is_file():
                    ledger.fail_extract(connection, task,
                                        error=f"no capture at {wacz}",
                                        max_attempts=args.max_attempts)
                    held.discard(task.key)
                    failed += 1
                    continue
                try:
                    # A retry re-extracts over a directory a previous attempt
                    # may have half-written, so sweep this extractor's own prior
                    # artifacts first. attempts is post-claim, so >1 means "this
                    # is not the first time".
                    result = extract(wacz, output_root, force=task.attempts > 1,
                                     stages=args.stages)
                except (ArchiveMutated, UnsafeArtifact) as exc:
                    # Both mean the run cannot be trusted to continue at all.
                    ledger.fail_extract(connection, task,
                                        error=f"{type(exc).__name__}: {exc}",
                                        max_attempts=args.max_attempts)
                    held.discard(task.key)
                    log.error("FATAL %s: %s", type(exc).__name__, exc)
                    return 2

                if result.error and result.verdict is None:
                    ledger.fail_extract(connection, task, error=result.error,
                                        max_attempts=args.max_attempts)
                    held.discard(task.key)
                    failed += 1
                    continue

                # An `invalid` verdict is a COMPLETED extraction whose answer was
                # no. Recording it as extracted is what stops it being retried
                # forever; `quality` is what keeps it out of the ingest frontier.
                ledger.finish_extract(
                    connection, task, quality=result.quality,
                    output_dir=task.relative_dir(),
                    screenshot_state=result.screenshot_state,
                    last_error=result.error)
                held.discard(task.key)
                done += 1
                if result.quality == "invalid":
                    invalid += 1
                    log.info("invalid  %s %s", task.url_hash[:12],
                             result.verdict.reasons if result.verdict else "")
                else:
                    log.debug("%-13s %s", result.quality, task.url_hash[:12])
            beat.keys = list(held)
    finally:
        beat.stop()
        if beat.keys:
            released = ledger.release(connection, beat.keys, stage="extract")
            if released:
                log.info("released %d unfinished claim(s)", released)
        with connection.cursor() as cur:
            state = ledger.summary(cur, outlet=args.outlet)
        connection.close()

    elapsed = time.monotonic() - started
    rate = done / elapsed if elapsed else 0
    print(f"\n== extracted {done} ({invalid} invalid), {failed} failed "
          f"in {elapsed:.1f}s ({rate:.2f}/s)")
    print("== ledger: " + "  ".join(f"{k}={v}" for k, v in state.items()))
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract_worker",
        description="Claim, extract and record one outlet's captures.")
    parser.add_argument("--outlet", required=True)
    parser.add_argument("--output", required=True, type=Path,
                        help="where article directories are written")
    parser.add_argument("--pages-root", type=Path,
                        default=os.environ.get("CAUSALIA_PAGES_ROOT",
                                               "/mnt/hdd/c0cshf/causalia/pages"))
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--stages", default=",".join(ALL_STAGES), metavar="LIST",
                        type=lambda v: tuple(p.strip() for p in v.split(",") if p.strip()),
                        help="stages to run (default: %(default)s). `content` "
                             "alone writes no screenshot: 4.8 MB less per "
                             "article and ~1.48x faster, backfillable later")
    parser.add_argument("--batch", type=int, default=8,
                        help="tasks per claim (default 8)")
    parser.add_argument("--limit", type=int, default=0,
                        help="stop after N articles (0 = run until drained)")
    parser.add_argument("--once", action="store_true",
                        help="exit when the frontier is empty instead of waiting")
    parser.add_argument("--idle-sleep", type=int, default=30)
    parser.add_argument("--max-attempts", type=int,
                        default=ledger.DEFAULT_MAX_ATTEMPTS)
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
