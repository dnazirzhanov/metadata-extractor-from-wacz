#!/usr/bin/env python3
"""
reap.py
=======
Return tasks whose worker stopped beating, and sweep the temp files a killed
extraction leaves behind. Cron, every few minutes.

    python -m ingest.reap --lease 900 --output /mnt/hdd/c0cshf/mandiner-extract

Two jobs, both recovery:

* **Stale claims.** A task whose owner has not beaten within the lease goes back
  to `pending`. The attempt is not refunded, so an archive that reliably kills
  its worker reaches `quarantined` rather than cycling forever.
* **Orphaned temp files.** Every artifact is written to `.causalia-tmp-*.part`
  in its own directory and moved into place atomically; a process killed between
  those two steps leaves the temp file. Measured on the benchmark output: 25.9
  MB across 46,253 directories. They are only swept when older than the lease,
  so a live write is never touched.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ingest import ledger                                        # noqa: E402


def sweep_temp_files(output_root: Path, *, older_than_seconds: int) -> tuple[int, int]:
    """Remove .causalia-tmp-* files older than the lease. Returns (count, bytes)."""
    if not output_root.is_dir():
        return 0, 0
    cutoff = time.time() - older_than_seconds
    removed = freed = 0
    for path in output_root.rglob(".causalia-tmp-*"):
        try:
            info = path.stat()
            if info.st_mtime >= cutoff:
                continue
            path.unlink()
        except OSError:
            continue
        removed += 1
        freed += info.st_size
    return removed, freed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="reap")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--outlet", default=None)
    parser.add_argument("--lease", type=int, default=ledger.DEFAULT_LEASE_SECONDS)
    parser.add_argument("--output", type=Path, default=None,
                        help="extraction output root, to sweep temp files")
    args = parser.parse_args(argv)

    dsn = args.dsn or os.environ.get("CX_INGEST_DSN")
    if not dsn:
        print("no DSN: pass --dsn or set CX_INGEST_DSN", file=sys.stderr)
        return 2

    connection = psycopg2.connect(dsn)
    try:
        reclaimed = ledger.reclaim_stale(connection, lease_seconds=args.lease,
                                         outlet=args.outlet)
        reclaimed["screenshot"] = ledger.reclaim_stale_screenshots(
            connection, lease_seconds=args.lease, outlet=args.outlet)
        with connection.cursor() as cur:
            state = ledger.summary(cur, outlet=args.outlet)
    finally:
        connection.close()

    print(f"reclaimed: extract={reclaimed['extract']} "
          f"ingest={reclaimed['ingest']} screenshot={reclaimed['screenshot']}")
    if args.output:
        removed, freed = sweep_temp_files(args.output, older_than_seconds=args.lease)
        print(f"swept: {removed} temp file(s), {freed / 1e6:.1f} MB")
    print("ledger: " + "  ".join(f"{k}={v}" for k, v in state.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
