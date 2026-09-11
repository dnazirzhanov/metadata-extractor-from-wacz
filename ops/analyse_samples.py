#!/usr/bin/env python3
"""Summarise a rung's samples.jsonl.

Reports the box metrics that actually mean something on this hardware:

  * aqu-sz and r_await/w_await, NOT %util. %util pegs high on a queued
    rotational device long before capacity.
  * CPU as user+nice. The archiver's workers run under nice and reading %user
    alone shows a saturated box as idle.
  * Dirty pages, because sustained throughput here is bound by writeback: once
    Dirty crosses 10% of 62 GB the kernel throttles the writers.
  * WAL growth and database growth per article, which is what a rung is for -
    the absolute rate matters less than whether cost per article is flat.

The FIRST sample of each run is kept: sample.sh already discards iostat's and
vmstat's own first report, which is the cumulative-since-boot one.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path


def percentile(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[index]


def main(path: str, articles: int | None = None) -> int:
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    if not rows:
        print("no samples")
        return 1

    def col(name):
        return [float(r.get(name, 0) or 0) for r in rows]

    print(f"samples            {len(rows)}  ({rows[0]['ts']} -> {rows[-1]['ts']})")
    for name, unit in (("cpu_busy", "%"), ("cpu_iowait", "%"), ("cpu_idle", "%"),
                       ("aqu_sz", ""), ("r_await", "ms"), ("w_await", "ms"),
                       ("r_kb_s", "kB/s"), ("w_kb_s", "kB/s"),
                       ("dirty_kb", "kB"), ("pg_cpu", "%"), ("pg_conns", "")):
        values = col(name)
        print(f"  {name:<12} median {statistics.median(values):>12,.1f}{unit:<5} "
              f"p90 {percentile(values, 0.90):>12,.1f}  max {max(values):>12,.1f}")

    wal = col("wal_bytes")
    db = col("db_bytes")
    out = col("out_bytes")
    wal_growth = wal[-1] - wal[0]
    db_growth = db[-1] - db[0]
    out_growth = out[-1] - out[0]
    print(f"\n  WAL growth   {wal_growth / 1048576:>10,.1f} MB")
    print(f"  DB growth    {db_growth / 1048576:>10,.1f} MB")
    print(f"  output grew  {out_growth / 1048576:>10,.1f} MB")
    if articles:
        print(f"\n  per article: WAL {wal_growth / articles / 1024:,.1f} kB   "
              f"DB {db_growth / articles / 1024:,.1f} kB   "
              f"output {out_growth / articles / 1048576:,.2f} MB")

    trouble = rows[-1]
    print(f"\n  ledger at the end: extracted {trouble['extracted']:,}  "
          f"ingested {trouble['ingested']:,}  failed {trouble['failed']}  "
          f"quarantined {trouble['quarantined']}  retried {trouble['retried']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1],
                          int(sys.argv[2]) if len(sys.argv) > 2 else None))
