"""
ledger.py
=========
corpus.extraction_task: what has been attempted, by whom, and how it went.

    seed          -> pending
    claim_*       -> claimed        (stamped with worker_id and a heartbeat)
    finish_*      -> extracted / ingested
    fail_*        -> failed, or quarantined once attempts run out
    reclaim_stale -> claimed rows whose worker stopped beating -> pending

MODELLED ON THE ARCHIVER, INCLUDING ITS MISTAKES
------------------------------------------------
causalia-final's db.py has driven 6.3M captures. Two things it had to learn are
built in here from the start:

* **A lease, not an age.** Its migration 003: "reset anything in_progress older
  than an hour" missed rows stranded by a worker that restarted within seconds,
  and stole rows from healthy long-running batches, putting two live workers on
  one item. Liveness is measured by a heartbeat the owner stamps, never by how
  old a row is.
* **SKIP LOCKED is not enough on its own.** Its migration 004: the claim locked
  rows in `urls` while the thing that excluded a URL was a row in `archives`,
  read under a READ COMMITTED snapshot - so a loser's snapshot could predate the
  winner's commit. A live 16-worker pool produced 320 claims over 141 distinct
  URLs. The claim here locks and tests the same rows of the same table, which is
  the case SKIP LOCKED actually covers, and migration 024's partial unique index
  is the structural backstop so correctness does not rest on that staying true.

TRANSACTIONS: every function here takes a CONNECTION and commits, because a
claim that is not committed is not a claim. The loader is the exception - it
marks a task ingested inside the same transaction as the rows it ingested, so
that `ingested` can never be true for an article that was rolled back.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: How long a worker may hold a task without beating. Generous against the
#: measured worst case: the slowest capture in the 2026-09-10 soak took 10.5 s,
#: and the largest archives in this corpus reach 1.4 GB.
DEFAULT_LEASE_SECONDS = 900

#: A task that has burned this many attempts is quarantined rather than retried
#: forever. An archive that reliably kills its worker must not be able to burn
#: the fleet - the archiver retries indefinitely, which is right for a network
#: fetch and wrong for a corrupt file.
DEFAULT_MAX_ATTEMPTS = 3

TASK_COLUMNS = ("url_hash, extractor_version, archive_row_id, wacz_sha256, "
                "outlet, output_dir, attempts")


@dataclass(frozen=True)
class Task:
    url_hash: str
    extractor_version: str
    archive_row_id: int | None
    wacz_sha256: str | None
    outlet: str
    output_dir: str | None
    attempts: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.url_hash, self.extractor_version)

    @property
    def shard(self) -> str:
        return self.url_hash[:2]

    def relative_dir(self) -> str:
        """``<outlet>/<h2>/<url_hash>`` - the corpus layout, kept in the output."""
        return f"{self.outlet}/{self.shard}/{self.url_hash}"

    def wacz_path(self, pages_root: Path) -> Path:
        return Path(pages_root) / self.relative_dir() / "page.wacz"


def _rows_to_tasks(rows) -> list[Task]:
    return [Task(*row) for row in rows]


# ---------------------------------------------------------------------
# Seeding the frontier
# ---------------------------------------------------------------------

def seed(conn, *, outlet: str, extractor_version: str,
         max_wacz_bytes: int = 100 * 1024 * 1024,
         require_2xx: bool = True, limit: int | None = None) -> int:
    """Queue one outlet's captures. Idempotent: ON CONFLICT DO NOTHING.

    TWO EXCLUSIONS, BOTH MEASURED, BOTH DELIBERATE:

    * ``doc_http_status !~ '^2'`` - error bodies archived as successes extract
      into plausible articles. Sampled 404s produced `partial` status with 1 to
      26 content blocks of the error page's own text, and one 301 produced a
      full `success`. On mandiner that is 61 rows; corpus-wide it is 15,802
      4xx/5xx plus 22,299 3xx.
    * ``wacz_size_bytes > max_wacz_bytes`` - the 686 captures over 100 MB reach
      1,440 MB of buffered payload each. They belong in their own low-concurrency
      pass, and their size is known before the file is opened.

    Both excluded sets stay in `archives`; they are simply not queued here. Run
    a second seed with different bounds to give them their own pass.
    """
    status_filter = "AND a.doc_http_status ~ '^2'" if require_2xx else ""
    size_filter = "AND a.wacz_size_bytes <= %(max_bytes)s" if max_wacz_bytes else ""
    limit_clause = "LIMIT %(limit)s" if limit else ""
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO corpus.extraction_task
                (url_hash, extractor_version, archive_row_id, wacz_sha256, outlet)
            SELECT DISTINCT ON (a.url_hash)
                   a.url_hash, %(version)s, a.id, a.wacz_sha256, a.outlet
              FROM public.archives a
             WHERE a.outlet = %(outlet)s
               AND a.status = 'success'
               {status_filter}
               {size_filter}
             -- The newest successful capture per URL. `archives` is append-only
             -- history and a URL can carry several.
             ORDER BY a.url_hash, a.finished_at DESC NULLS LAST, a.id DESC
             {limit_clause}
            ON CONFLICT (url_hash, extractor_version) DO NOTHING
        """, {"version": extractor_version, "outlet": outlet,
              "max_bytes": max_wacz_bytes, "limit": limit})
        queued = cur.rowcount
    conn.commit()
    return queued


# ---------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------

def claim_extract(conn, *, outlet: str, extractor_version: str, worker_id: str,
                  batch: int = 8,
                  max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> list[Task]:
    """Claim up to ``batch`` pending extraction tasks. Commits immediately."""
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE corpus.extraction_task t
               SET extract_state = 'claimed', worker_id = %(worker)s,
                   claimed_at = now(), heartbeat_at = now(),
                   -- Incremented at CLAIM time: a worker that dies without
                   -- reporting must still burn an attempt.
                   attempts = t.attempts + 1, row_updated_at = now()
             WHERE (t.url_hash, t.extractor_version) IN (
                     SELECT url_hash, extractor_version
                       FROM corpus.extraction_task
                      WHERE outlet = %(outlet)s
                        AND extractor_version = %(version)s
                        -- 'failed' is RETRYABLE, not terminal: it records that
                        -- the last attempt failed, and `attempts` is what bounds
                        -- the retries. Only 'quarantined' is the end of the line.
                        -- Found by a test: with 'pending' alone, fail_extract
                        -- made every failure permanent and the retry policy
                        -- never ran at all.
                        AND extract_state IN ('pending', 'failed')
                        AND attempts < %(max_attempts)s
                      ORDER BY url_hash
                      FOR UPDATE SKIP LOCKED
                      LIMIT %(batch)s)
            RETURNING {TASK_COLUMNS}
        """, {"worker": worker_id, "outlet": outlet, "batch": batch,
              "version": extractor_version, "max_attempts": max_attempts})
        tasks = _rows_to_tasks(cur.fetchall())
    conn.commit()
    return tasks


def claim_ingest(conn, *, outlet: str, extractor_version: str, worker_id: str,
                 batch: int = 100,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> list[Task]:
    """Claim extracted, USABLE tasks that are not yet loaded.

    The quality filter here is enforcement layer one: an `invalid` extraction is
    never claimed, so it is never ingested, so no row exists for a query to
    reach. That is stronger than any WHERE clause in the search path.
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE corpus.extraction_task t
               SET ingest_state = 'claimed', worker_id = %(worker)s,
                   claimed_at = now(), heartbeat_at = now(),
                   row_updated_at = now()
             WHERE (t.url_hash, t.extractor_version) IN (
                     SELECT url_hash, extractor_version
                       FROM corpus.extraction_task
                      WHERE outlet = %(outlet)s
                        AND extractor_version = %(version)s
                        AND extract_state = 'extracted'
                        AND quality IN ('success', 'partial_valid')
                        AND ingest_state IN ('pending', 'failed')
                        AND attempts < %(max_attempts)s
                      ORDER BY url_hash
                      FOR UPDATE SKIP LOCKED
                      LIMIT %(batch)s)
            RETURNING {TASK_COLUMNS}
        """, {"worker": worker_id, "outlet": outlet, "batch": batch,
              "version": extractor_version, "max_attempts": max_attempts})
        tasks = _rows_to_tasks(cur.fetchall())
    conn.commit()
    return tasks


def claim_screenshot(conn, *, outlet: str, extractor_version: str,
                     worker_id: str, batch: int = 32) -> list[Task]:
    """Claim articles whose screenshot stage still has work to do.

    `skipped` (the stage was switched off) and `failed` are work. `present` and
    `none_in_archive` are not: the first already has one, and the second is a
    capture that genuinely holds none, which is normal for everything crawled
    before 2026-08-12 and must not be re-read on every backfill.

    Only ALREADY-EXTRACTED articles are claimed. A screenshot for an article
    whose content extraction has not run yet is not a backfill - it is the
    ordinary `content,screenshot` path, done in one read of the archive.
    """
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE corpus.extraction_task t
               SET screenshot_state = 'claimed', worker_id = %(worker)s,
                   claimed_at = now(), heartbeat_at = now(),
                   row_updated_at = now()
             WHERE (t.url_hash, t.extractor_version) IN (
                     SELECT url_hash, extractor_version
                       FROM corpus.extraction_task
                      WHERE outlet = %(outlet)s
                        AND extractor_version = %(version)s
                        AND extract_state = 'extracted'
                        AND screenshot_state IN ('pending', 'skipped', 'failed')
                      ORDER BY url_hash
                      FOR UPDATE SKIP LOCKED
                      LIMIT %(batch)s)
            RETURNING {TASK_COLUMNS}
        """, {"worker": worker_id, "outlet": outlet, "batch": batch,
              "version": extractor_version})
        tasks = _rows_to_tasks(cur.fetchall())
    conn.commit()
    return tasks


# ---------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------

def heartbeat(conn, keys, *, worker_id: str | None = None) -> int:
    """Prove the owner of these tasks is still alive. Called on a timer.

    TWO GUARDS, BOTH ADDED AFTER THE 50k RUNG PRODUCED 408 DEADLOCKS.
    PostgreSQL named the cycle precisely: a loader's heartbeat connection
    waiting on a row its OWN batch transaction was writing, while another
    loader's batch waited on a row this heartbeat had already taken.

    * ``FOR UPDATE SKIP LOCKED`` - a beat NEVER WAITS. A row currently being
      written by the worker that owns it is alive by definition, so skipping it
      loses nothing: the lease is 900 s against a 30 s beat, so a row has thirty
      chances to be beaten before anyone would call it stale. Waiting, on the
      other hand, is what closes a deadlock cycle.
    * ``worker_id`` - beat only rows THIS worker still owns. Without it a
      heartbeat kept refreshing tasks that had been failed, released and
      re-claimed by a different worker, which both fed the deadlock cascade and
      would have kept a dead worker's claim looking alive.
    """
    keys = list(keys)
    if not keys:
        return 0
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE corpus.extraction_task t
               SET heartbeat_at = now()
             WHERE (t.url_hash, t.extractor_version) IN (
                     SELECT url_hash, extractor_version
                       FROM corpus.extraction_task
                      WHERE (url_hash, extractor_version) IN %(keys)s
                        AND (%(worker)s IS NULL OR worker_id = %(worker)s)
                      ORDER BY url_hash
                      FOR UPDATE SKIP LOCKED)
        """, {"keys": tuple(keys), "worker": worker_id})
        beaten = cur.rowcount
    conn.commit()
    return beaten


def reclaim_stale(conn, *, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                  outlet: str | None = None) -> dict:
    """Return tasks whose owner stopped beating to the frontier.

    Asks the only question that matters - has anyone touched this row recently -
    rather than how old the row is. A crash is detected within a couple of
    missed beats however soon the worker restarts, and a healthy slow batch is
    never stolen however long it runs.
    """
    out = {}
    with conn.cursor() as cur:
        for column, state in (("extract_state", "extract"),
                              ("ingest_state", "ingest")):
            cur.execute(f"""
                UPDATE corpus.extraction_task
                   SET {column} = 'pending', worker_id = NULL,
                       last_error = 'reclaimed: worker stopped heartbeating',
                       row_updated_at = now()
                 WHERE {column} = 'claimed'
                   AND (%(outlet)s IS NULL OR outlet = %(outlet)s)
                   AND coalesce(heartbeat_at, claimed_at)
                       < now() - make_interval(secs => %(lease)s)
            """, {"lease": lease_seconds, "outlet": outlet})
            out[state] = cur.rowcount
    conn.commit()
    return out


STAGE_COLUMNS = {"extract": "extract_state", "ingest": "ingest_state",
                 "screenshot": "screenshot_state"}


def release(conn, keys, *, stage: str = "extract") -> int:
    """Hand claims back on a graceful stop (SIGTERM), without waiting for a lease.

    The archiver's worker does the same thing for live captures. The attempt is
    NOT given back: it was spent, and pretending otherwise is how a poisonous
    archive gets retried forever.
    """
    keys = list(keys)
    if not keys:
        return 0
    column = STAGE_COLUMNS[stage]
    with conn.cursor() as cur:
        cur.execute(f"""
            UPDATE corpus.extraction_task
               SET {column} = 'pending', worker_id = NULL,
                   last_error = 'released: worker stopped on request',
                   row_updated_at = now()
             WHERE (url_hash, extractor_version) IN %s
               AND {column} = 'claimed'
        """, (tuple(keys),))
        released = cur.rowcount
    conn.commit()
    return released


# ---------------------------------------------------------------------
# Recording outcomes
# ---------------------------------------------------------------------

def finish_extract(conn, task: Task, *, quality: str, output_dir: str,
                   screenshot_state: str, last_error: str | None = None) -> None:
    """Record a completed extraction, whatever the verdict.

    An `invalid` verdict is a COMPLETED extraction, not a failure: the archive
    was read, the article was judged, and the answer was no. It is recorded as
    extracted so the work is never repeated, and the quality column is what
    keeps it out of the ingest frontier.
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE corpus.extraction_task
               SET extract_state = 'extracted', quality = %(quality)s,
                   output_dir = %(output_dir)s, extracted_at = now(),
                   screenshot_state = %(screenshot)s,
                   last_stage = 'extract', last_error = %(error)s,
                   worker_id = NULL, row_updated_at = now()
             WHERE url_hash = %(url_hash)s
               AND extractor_version = %(version)s
        """, {"quality": quality, "output_dir": output_dir,
              "screenshot": screenshot_state, "error": last_error,
              "url_hash": task.url_hash, "version": task.extractor_version})
    conn.commit()


def fail_extract(conn, task: Task, *, error: str,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> str:
    """Record an extraction that could not complete. Returns the new state.

    `failed` is retryable - claim_extract picks it up again while attempts
    remain - and `quarantined` is terminal. `task.attempts` is the post-claim
    value, so a task claimed for the third time with max_attempts=3 quarantines
    rather than going round again.
    """
    state = "quarantined" if task.attempts >= max_attempts else "failed"
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE corpus.extraction_task
               SET extract_state = %(state)s, last_stage = 'extract',
                   last_error = %(error)s, worker_id = NULL,
                   row_updated_at = now()
             WHERE url_hash = %(url_hash)s AND extractor_version = %(version)s
        """, {"state": state, "error": error[:2000],
              "url_hash": task.url_hash, "version": task.extractor_version})
    conn.commit()
    return state


def mark_ingested(cur, task: Task) -> None:
    """Mark a task loaded. Takes a CURSOR, not a connection, ON PURPOSE.

    This runs inside the loader's transaction, alongside the article rows it
    describes, so `ingested` and the rows commit or roll back together. Marking
    it afterwards in its own transaction would allow a crash in between to leave
    a task that says ingested and a database that never saw the article.
    """
    cur.execute("""
        UPDATE corpus.extraction_task
           SET ingest_state = 'ingested', ingested_at = now(),
               last_stage = 'ingest', last_error = NULL, worker_id = NULL,
               row_updated_at = now()
         WHERE url_hash = %s AND extractor_version = %s
    """, (task.url_hash, task.extractor_version))


def fail_ingest(conn, task: Task, *, error: str) -> None:
    """Record a load that could not complete, in its own transaction.

    Called after the batch transaction has ended, because the failure has to
    survive the rollback that produced it.
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE corpus.extraction_task
               SET ingest_state = 'failed', last_stage = 'ingest',
                   last_error = %(error)s, worker_id = NULL,
                   row_updated_at = now()
             WHERE url_hash = %(url_hash)s AND extractor_version = %(version)s
        """, {"error": error[:2000], "url_hash": task.url_hash,
              "version": task.extractor_version})
    conn.commit()


def set_screenshot_state(conn, task: Task, state: str) -> None:
    """The screenshot stage is independently rerunnable, so it owns one column."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE corpus.extraction_task
               SET screenshot_state = %s, row_updated_at = now()
             WHERE url_hash = %s AND extractor_version = %s
        """, (state, task.url_hash, task.extractor_version))
    conn.commit()


# ---------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------

def reclaim_stale_screenshots(conn, *, lease_seconds: int = DEFAULT_LEASE_SECONDS,
                              outlet: str | None = None) -> int:
    """The screenshot stage's half of reclaim_stale."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE corpus.extraction_task
               SET screenshot_state = 'pending', worker_id = NULL,
                   last_error = 'reclaimed: worker stopped heartbeating',
                   row_updated_at = now()
             WHERE screenshot_state = 'claimed'
               AND (%(outlet)s IS NULL OR outlet = %(outlet)s)
               AND coalesce(heartbeat_at, claimed_at)
                   < now() - make_interval(secs => %(lease)s)
        """, {"lease": lease_seconds, "outlet": outlet})
        reclaimed = cur.rowcount
    conn.commit()
    return reclaimed


def counts(cur, *, outlet: str | None = None) -> list[dict]:
    """Every metric the run needs, as one GROUP BY."""
    cur.execute("""
        SELECT outlet, extract_state, quality, ingest_state, screenshot_state,
               count(*) AS n, max(heartbeat_at) AS last_beat
          FROM corpus.extraction_task
         WHERE (%(outlet)s IS NULL OR outlet = %(outlet)s)
         GROUP BY 1, 2, 3, 4, 5
         ORDER BY 1, 2, 3, 4, 5
    """, {"outlet": outlet})
    return [dict(zip([c.name for c in cur.description], row))
            for row in cur.fetchall()]


def summary(cur, *, outlet: str | None = None) -> dict:
    """The headline numbers, for a one-line progress log."""
    cur.execute("""
        SELECT count(*) FILTER (WHERE extract_state = 'pending')      AS to_extract,
               count(*) FILTER (WHERE extract_state = 'claimed')      AS extracting,
               count(*) FILTER (WHERE extract_state = 'extracted')    AS extracted,
               count(*) FILTER (WHERE extract_state = 'failed')       AS extract_failed,
               count(*) FILTER (WHERE extract_state = 'quarantined')  AS quarantined,
               count(*) FILTER (WHERE quality = 'invalid')            AS invalid,
               count(*) FILTER (WHERE ingest_state = 'pending'
                                  AND extract_state = 'extracted'
                                  AND quality IN ('success','partial_valid'))
                                                                      AS to_ingest,
               count(*) FILTER (WHERE ingest_state = 'ingested')      AS ingested,
               count(*) FILTER (WHERE ingest_state = 'failed')        AS ingest_failed,
               count(*)                                               AS total
          FROM corpus.extraction_task
         WHERE (%(outlet)s IS NULL OR outlet = %(outlet)s)
    """, {"outlet": outlet})
    return dict(zip([c.name for c in cur.description], cur.fetchone()))


def worker_identity(prefix: str = "extract") -> str:
    """Who holds a claim. Not used for correctness - used for answering
    'which worker stranded these rows?' after the fact, which is exactly the
    question the archiver could not answer before its migration 003."""
    return f"{prefix}/{os.uname().nodename}/{os.getpid()}"
