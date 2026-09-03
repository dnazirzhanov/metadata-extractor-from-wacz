#!/usr/bin/env python3
"""Choose a stratified adversarial sample of the WACZ corpus.

The pressure test needs input that BREAKS things, and a uniform random sample of
4.8M archives will not provide it: the failure classes that matter are each a
fraction of a percent, so uniform sampling reproduces the mean and misses the
tail entirely. This picks deliberately from the tail, records which stratum each
archive came from, and writes a manifest the extractor and the harness both read.

STRICTLY READ-ONLY over the crawler database. The query is a single SELECT, it
goes through psql rather than psycopg2 so no crawler credential is needed here,
and the SQL is checked before it runs. Nothing in this file writes anywhere
except the manifest it is asked to produce.

WHY THESE STRATA, AND WHAT IS MEASURED ABOUT EACH  (crawler DB, 2026-09-03)

    stratum          selector                          available
    error_body       doc_http_status ~ '^[45]'            15,802
    redirect_stub    doc_http_status ~ '^3'               22,299
    thin_capture     wacz_size_bytes < 1 MB                  593
    giant            wacz_size_bytes > 100 MB             12,174
    agegate_hunt     outlet = 'ripost.hu'                285,607
    angular          magyarnemzet.hu / mandiner.hu     ~1,520,000
    nominal          doc_http_status = '200'           1,348,904
    unaudited        doc_http_status IS NULL           ~3,450,000

`doc_http_status` is the crawler's audit column, added 2026-09-01. NULL means
NOT AUDITED, never "fine" - a clean row is stamped '200'. At the time of writing
only origo.hu and duol.hu are fully swept and bama.hu is in progress, which is
why `unaudited` is by far the largest stratum and why it is sampled ACROSS
outlets rather than from whichever one happens to sort first.

THE 512 kB HEURISTIC IS DEAD. Earlier notes on this project treat a sub-512 kB
WACZ as the signature of an error-page capture. Measured now: exactly ONE
archive in 4.84M is under 512 kB, and the smallest is 115 kB. That detector has
been superseded by doc_http_status, so `thin_capture` uses a 1 MB threshold
(593 rows) purely to catch captures that are structurally short, and the real
error-body work is done by the `error_body` stratum.

THREE FAILURE CLASSES NO SQL COLUMN CAN FIND, and how this file approaches them:

  * AGE-GATE INTERSTITIALS. ripost.hu captures that hold the Hungarian 18+ page
    instead of the article. Status 200, ~1.36 MB, and the interstitial still
    serves the article's JSON-LD and OpenGraph - so title, date and section
    extract PERFECTLY while the body is empty. Sampled at ~3.5% of ripost, so
    `agegate_hunt` should surface roughly one in thirty; the harness detects
    them after extraction, not here.
  * ANGULAR SHORT-FRAGMENT TRUNCATION. On magyarnemzet the ng-state fallback is
    the PRIMARY extraction path, not a rescue - it fired on 59 of 60 sampled
    articles. A `success` verdict with plausible metadata there is not evidence
    of a complete body.
  * TABLE CONTENT LOSS. The extractor warns "<table> holds N characters that no
    supported block type can represent"; that text reaches no block and so no
    search vector. Outlet-independent, so the `nominal` stratum carries it.

Usage:
    scripts/sample_corpus.py --stats
    scripts/sample_corpus.py --per-stratum 625 -o /tmp/pressure_sample.json

Connection: set CX_CRAWLER_PSQL to a full psql command. The default assumes the
archiver's own container on milab2. It is never given a default host, on purpose,
in the same spirit as scripts/migrate.sh.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_PSQL = ("docker exec -i causalia-final-db-1 "
                "psql -U causalia -d causalia")

#: The seed is part of the manifest, not a hidden constant: the same seed picks
#: the same archives forever, and changing it yields a DISJOINT second sample.
#: md5 over url_hash is used rather than random() so a re-run reproduces the
#: sample without storing it.
DEFAULT_SEED = "pressure-2026-09-03"

#: Fences the result rows off from psql command tags (the read-only SET emits
#: one). Chosen so it cannot occur in a URL, an outlet or a path.
ROW_SENTINEL = "__CX_ROWS_BEGIN__"

#: Ordered MOST SPECIFIC FIRST. Every archive lands in exactly one stratum, and
#: the targeted hunts deliberately sit above `unaudited` - ripost and the
#: Angular outlets are unaudited too, and would otherwise be swallowed by it.
STRATA_SQL = """
        CASE
          WHEN a.doc_http_status ~ '^[45]'                    THEN 'error_body'
          WHEN a.doc_http_status ~ '^3'                       THEN 'redirect_stub'
          WHEN a.wacz_size_bytes < 1048576                    THEN 'thin_capture'
          WHEN a.wacz_size_bytes > 104857600                  THEN 'giant'
          WHEN a.outlet = 'ripost.hu'                         THEN 'agegate_hunt'
          WHEN a.outlet IN ('magyarnemzet.hu', 'mandiner.hu') THEN 'angular'
          WHEN a.doc_http_status = '200'                      THEN 'nominal'
          ELSE 'unaudited'
        END
"""

FIELDS = ("stratum", "url_hash", "outlet", "url", "wacz_path",
          "wacz_size_bytes", "doc_http_status")

#: Per-stratum overrides on --per-stratum, because a uniform cap is the wrong
#: unit here: strata are wildly different SIZES on disk, and the pressure test
#: has to run on a machine that is already saturated by the crawl.
#:
#: `giant` is the one that matters. Its members run from 100 MB to 1,907 MB, so
#: 625 of them is on the order of 100 GB of random reads - hours of I/O
#: competing with the fleet, to prove a point that 60 archives prove just as
#: well. What that stratum tests is behaviour under a single huge input
#: (memory, timeouts), not a distribution.
STRATUM_CAPS = {
    "giant": 60,
}


def sample_sql(per_stratum: int, seed: str) -> str:
    """One SELECT. Deterministic, and it touches nothing but archives+urls."""
    cap_cases = "\n".join(
        f"                  WHEN {sql_literal(name)} THEN {int(cap)}"
        for name, cap in sorted(STRATUM_CAPS.items()))
    return f"""
WITH ranked AS (
    SELECT {STRATA_SQL} AS stratum,
           a.url_hash, a.outlet, u.url, a.wacz_path,
           a.wacz_size_bytes, coalesce(a.doc_http_status, '') AS doc_http_status,
           row_number() OVER (
               PARTITION BY {STRATA_SQL}
               ORDER BY md5(a.url_hash || {sql_literal(seed)})
           ) AS rn
      FROM archives a
      JOIN urls u ON u.url_hash = a.url_hash
     WHERE a.status = 'success'
       AND a.wacz_path IS NOT NULL
)
SELECT stratum, url_hash, outlet, url, wacz_path, wacz_size_bytes, doc_http_status
  FROM ranked
 WHERE rn <= CASE stratum
{cap_cases}
                  ELSE {int(per_stratum)}
              END
 ORDER BY stratum, rn
"""


def stats_sql() -> str:
    return f"""
SELECT {STRATA_SQL} AS stratum, count(*) AS available,
       pg_size_pretty(min(a.wacz_size_bytes)) AS smallest,
       pg_size_pretty(max(a.wacz_size_bytes)) AS largest,
       count(DISTINCT a.outlet) AS outlets
  FROM archives a
 WHERE a.status = 'success' AND a.wacz_path IS NOT NULL
 GROUP BY 1 ORDER BY 2 DESC
"""


def sql_literal(text: str) -> str:
    """A SQL string literal. The seed is the only value interpolated here."""
    if "'" in text or "\\" in text:
        raise SystemExit(f"seed must not contain quotes or backslashes: {text!r}")
    return "'" + text + "'"


def run_psql(sql: str, *, tuples_only: bool) -> str:
    """Run one read-only statement through psql.

    The guard is not decoration. This script is pointed at the live archiver
    database, so it refuses to send anything that is not a single SELECT/WITH -
    a typo that turned into a DELETE would be unrecoverable.
    """
    stripped = sql.strip().rstrip(";").lstrip()
    if not stripped.upper().startswith(("SELECT", "WITH")):
        raise SystemExit("refusing to run a statement that is not SELECT/WITH")
    for word in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
                 "TRUNCATE", "GRANT", "COPY"):
        if f" {word} " in f" {stripped.upper()} ":
            raise SystemExit(f"refusing: statement contains {word}")

    command = shlex.split(os.environ.get("CX_CRAWLER_PSQL", DEFAULT_PSQL))
    flags = ["-v", "ON_ERROR_STOP=1", "-A", "-F", "\t", "--no-align"]
    if tuples_only:
        flags.append("-t")
    # Read-only at the session level too, so the server refuses a write even if
    # the guard above were somehow wrong. The SET emits its own command tag, so
    # the rows are fenced with a sentinel rather than assuming the first line of
    # output is data - which is how this got its first bug.
    payload = ("SET default_transaction_read_only = on;\n"
               f"\\echo {ROW_SENTINEL}\n" + stripped + ";\n")
    done = subprocess.run(command + flags, input=payload, text=True,
                          capture_output=True)
    if done.returncode != 0:
        sys.stderr.write(done.stderr)
        raise SystemExit(f"psql failed ({done.returncode})")
    _, sentinel, rows = done.stdout.partition(ROW_SENTINEL + "\n")
    if not sentinel:
        sys.stderr.write(done.stdout)
        raise SystemExit("psql produced no row section")
    return rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Stratified adversarial sample of the WACZ corpus.")
    parser.add_argument("--per-stratum", type=int, default=625,
                        help="cap per stratum; small strata return fewer "
                             "(default 625, so 8 strata ~ 5,000 archives)")
    parser.add_argument("--seed", default=DEFAULT_SEED,
                        help="change it for a DISJOINT second sample")
    parser.add_argument("--stats", action="store_true",
                        help="report stratum availability and exit")
    parser.add_argument("-o", "--out", type=Path,
                        help="manifest to write (required unless --stats)")
    args = parser.parse_args(argv[1:])

    if args.stats:
        print(run_psql(stats_sql(), tuples_only=False))
        return 0
    if args.out is None:
        parser.error("-o/--out is required unless --stats is given")

    started = time.monotonic()
    raw = run_psql(sample_sql(args.per_stratum, args.seed), tuples_only=True)
    records = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != len(FIELDS):
            raise SystemExit(f"unexpected row shape ({len(parts)}): {line[:120]}")
        row = dict(zip(FIELDS, parts))
        row["wacz_size_bytes"] = int(row["wacz_size_bytes"])
        row["doc_http_status"] = row["doc_http_status"] or None
        records.append(row)

    counts: dict[str, int] = {}
    for row in records:
        counts[row["stratum"]] = counts.get(row["stratum"], 0) + 1

    manifest = {
        "seed": args.seed,
        "per_stratum": args.per_stratum,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "total": len(records),
        "by_stratum": dict(sorted(counts.items())),
        "total_bytes": sum(r["wacz_size_bytes"] for r in records),
        "archives": records,
    }
    args.out.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    bytes_by: dict[str, int] = {}
    for row in records:
        bytes_by[row["stratum"]] = (bytes_by.get(row["stratum"], 0)
                                    + row["wacz_size_bytes"])
    manifest["bytes_by_stratum"] = dict(sorted(bytes_by.items()))

    print(f"== {len(records)} archives, "
          f"{manifest['total_bytes'] / 2**30:.1f} GiB of WACZ to read")
    for stratum, n in manifest["by_stratum"].items():
        cap = STRATUM_CAPS.get(stratum, args.per_stratum)
        note = ""
        if stratum in STRATUM_CAPS:
            note = f"  (capped at {cap}: see STRATUM_CAPS)"
        elif n < cap:
            note = "  (all there is)"
        print(f"   {stratum:<14} {n:>5}  "
              f"{bytes_by[stratum] / 2**30:>6.1f} GiB{note}")
    print(f"== wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
