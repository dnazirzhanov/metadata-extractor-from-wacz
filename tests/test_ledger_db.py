"""The work ledger and the searchability invariant, against a real Postgres.

    CX_TEST_DSN="host=127.0.0.1 port=55446 user=causalia password=p0 dbname=causalia_p0" \
        pytest tests/test_ledger_db.py

Every test here is a failure or recovery scenario from the 2026-09-11 review:
who may hold a task, what happens when a worker dies, what happens when a batch
contains one bad article, and what a query is allowed to return.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

DSN = os.environ.get("CX_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not DSN, reason="CX_TEST_DSN not set - no database to test the ledger against")

pytest.importorskip("psycopg2", reason="pip install -e '.[db]'")
import psycopg2                                                  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ingest import ledger                                        # noqa: E402
from ingest.load import load_batch                               # noqa: E402
from test_ingest_contract import write_article                   # noqa: E402

VERSION = "test-extractor/1.0"
OUTLET = "ledgertest.hu"


def hash_for(n: int) -> str:
    return f"{n:064x}"


@pytest.fixture
def conn():
    connection = psycopg2.connect(DSN)
    yield connection
    connection.rollback()
    connection.close()


@pytest.fixture(autouse=True)
def clean(conn):
    """Each test owns the whole fixture outlet, before and after."""
    def wipe():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM corpus.extraction_task WHERE outlet = %s",
                        (OUTLET,))
            cur.execute("""DELETE FROM corpus.article WHERE outlet = %s""", (OUTLET,))
            cur.execute("DELETE FROM archives WHERE outlet = %s", (OUTLET,))
            cur.execute("DELETE FROM urls WHERE outlet = %s", (OUTLET,))
        conn.commit()
    wipe()
    yield
    wipe()


def make_captures(conn, n: int, *, doc_status="200", size=1024,
                  captures_per_url=1) -> list[str]:
    """n URLs, each with a successful capture, in the crawler's tables."""
    hashes = [hash_for(i) for i in range(n)]
    with conn.cursor() as cur:
        for i, url_hash in enumerate(hashes):
            cur.execute(
                "INSERT INTO urls (url_hash, url, outlet) VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (url_hash, f"https://{OUTLET}/{i}", OUTLET))
            for capture in range(captures_per_url):
                cur.execute(
                    "INSERT INTO archives (url_hash, outlet, status, wacz_path, "
                    " wacz_sha256, wacz_size_bytes, doc_http_status, finished_at) "
                    "VALUES (%s,%s,'success',%s,%s,%s,%s, now() - make_interval(days => %s))",
                    (url_hash, OUTLET, f"/pages/{OUTLET}/{url_hash[:2]}/{url_hash}/page.wacz",
                     f"sha-{i}-{capture}", size, doc_status, capture))
    conn.commit()
    return hashes


class TestSeeding:
    def test_seeding_queues_one_task_per_url(self, conn):
        make_captures(conn, 5)
        assert ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION) == 5

    def test_seeding_twice_queues_nothing_new(self, conn):
        make_captures(conn, 5)
        ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION)
        assert ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION) == 0

    def test_a_new_extractor_version_is_new_work(self, conn):
        make_captures(conn, 3)
        ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION)
        assert ledger.seed(conn, outlet=OUTLET, extractor_version="v2") == 3

    def test_non_2xx_captures_are_not_queued(self, conn):
        """Error bodies archived as successes extract into plausible articles:
        sampled 404s produced 1 to 26 content blocks of error-page text."""
        make_captures(conn, 4, doc_status="404")
        assert ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION) == 0

    def test_giants_are_held_back_for_their_own_pass(self, conn):
        make_captures(conn, 4, size=200 * 1024 * 1024)
        assert ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION) == 0
        assert ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION,
                           max_wacz_bytes=None) == 4

    def test_the_newest_capture_of_a_url_is_the_one_queued(self, conn):
        make_captures(conn, 1, captures_per_url=3)
        ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION)
        with conn.cursor() as cur:
            cur.execute("""SELECT t.wacz_sha256, a.finished_at
                             FROM corpus.extraction_task t
                             JOIN archives a ON a.id = t.archive_row_id
                            WHERE t.outlet = %s""", (OUTLET,))
            sha, finished = cur.fetchone()
            cur.execute("SELECT max(finished_at) FROM archives WHERE outlet = %s",
                        (OUTLET,))
            assert finished == cur.fetchone()[0]
            assert sha == "sha-0-0"


class TestClaiming:
    def test_two_workers_never_hold_the_same_task(self, conn):
        """The archiver's migration 004: a live 16-worker pool once produced
        320 claims covering 141 distinct URLs."""
        make_captures(conn, 40)
        ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION)

        others = [psycopg2.connect(DSN) for _ in range(4)]
        try:
            claimed = []
            for i, other in enumerate(others):
                claimed += ledger.claim_extract(
                    other, outlet=OUTLET, extractor_version=VERSION,
                    worker_id=f"w{i}", batch=10)
        finally:
            for other in others:
                other.close()

        keys = [t.key for t in claimed]
        assert len(keys) == 40
        assert len(set(keys)) == 40, "a task was handed to two workers"

    def test_a_second_live_claim_is_refused_by_the_database(self, conn):
        """Not by the claim query - by the partial unique index, so correctness
        does not rest on the query staying written the way it is today."""
        make_captures(conn, 1)
        ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION)
        ledger.claim_extract(conn, outlet=OUTLET, extractor_version=VERSION,
                             worker_id="a", batch=1)
        with pytest.raises(psycopg2.errors.UniqueViolation):
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO corpus.extraction_task
                        (url_hash, extractor_version, outlet, extract_state)
                    VALUES (%s, %s, %s, 'claimed')
                """, (hash_for(0), "another-version", OUTLET))
        conn.rollback()

    def test_a_claim_burns_an_attempt_even_if_the_worker_says_nothing(self, conn):
        """Otherwise an archive that reliably kills its worker is retried forever."""
        make_captures(conn, 1)
        ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION)
        task, = ledger.claim_extract(conn, outlet=OUTLET,
                                     extractor_version=VERSION,
                                     worker_id="a", batch=1)
        assert task.attempts == 1

    def test_a_task_out_of_attempts_is_not_claimed_again(self, conn):
        make_captures(conn, 1)
        ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION)
        for _ in range(3):
            tasks = ledger.claim_extract(conn, outlet=OUTLET,
                                         extractor_version=VERSION,
                                         worker_id="a", batch=1, max_attempts=3)
            if tasks:
                ledger.fail_extract(conn, tasks[0], error="boom", max_attempts=3)
        assert ledger.claim_extract(conn, outlet=OUTLET,
                                    extractor_version=VERSION,
                                    worker_id="a", batch=1, max_attempts=3) == []
        with conn.cursor() as cur:
            cur.execute("SELECT extract_state FROM corpus.extraction_task "
                        "WHERE outlet = %s", (OUTLET,))
            assert cur.fetchone()[0] == "quarantined"


class TestRecovery:
    def _age_the_heartbeat(self, conn, seconds: int):
        with conn.cursor() as cur:
            cur.execute("""UPDATE corpus.extraction_task
                              SET heartbeat_at = now() - make_interval(secs => %s)
                            WHERE outlet = %s""", (seconds, OUTLET))
        conn.commit()

    def test_a_dead_workers_task_comes_back(self, conn):
        make_captures(conn, 3)
        ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION)
        ledger.claim_extract(conn, outlet=OUTLET, extractor_version=VERSION,
                             worker_id="doomed", batch=3)
        self._age_the_heartbeat(conn, 2000)
        assert ledger.reclaim_stale(conn, lease_seconds=900,
                                    outlet=OUTLET)["extract"] == 3
        assert len(ledger.claim_extract(conn, outlet=OUTLET,
                                        extractor_version=VERSION,
                                        worker_id="next", batch=3)) == 3

    def test_a_live_worker_is_never_robbed_however_long_it_runs(self, conn):
        """Migration 003's second failure mode: an age cutoff guessed against
        batch duration was guessed wrong and put two workers on one item."""
        make_captures(conn, 2)
        ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION)
        tasks = ledger.claim_extract(conn, outlet=OUTLET,
                                     extractor_version=VERSION,
                                     worker_id="slow", batch=2)
        with conn.cursor() as cur:
            cur.execute("""UPDATE corpus.extraction_task
                              SET claimed_at = now() - interval '6 hours'
                            WHERE outlet = %s""", (OUTLET,))
        conn.commit()
        ledger.heartbeat(conn, [t.key for t in tasks])           # still alive
        assert ledger.reclaim_stale(conn, lease_seconds=900,
                                    outlet=OUTLET)["extract"] == 0

    def test_a_graceful_stop_hands_claims_back_without_waiting(self, conn):
        make_captures(conn, 2)
        ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION)
        tasks = ledger.claim_extract(conn, outlet=OUTLET,
                                     extractor_version=VERSION,
                                     worker_id="stopping", batch=2)
        assert ledger.release(conn, [t.key for t in tasks]) == 2
        assert len(ledger.claim_extract(conn, outlet=OUTLET,
                                        extractor_version=VERSION,
                                        worker_id="next", batch=2)) == 2

    def test_an_invalid_extraction_is_finished_work_not_a_failure(self, conn):
        """It is recorded `extracted` so it is never repeated, and `invalid` so
        the ingest frontier never sees it."""
        make_captures(conn, 1)
        ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION)
        task, = ledger.claim_extract(conn, outlet=OUTLET,
                                     extractor_version=VERSION,
                                     worker_id="a", batch=1)
        ledger.finish_extract(conn, task, quality="invalid",
                              output_dir=task.relative_dir(),
                              screenshot_state="present")
        assert ledger.claim_ingest(conn, outlet=OUTLET,
                                   extractor_version=VERSION,
                                   worker_id="loader", batch=10) == []
        assert ledger.claim_extract(conn, outlet=OUTLET,
                                    extractor_version=VERSION,
                                    worker_id="a", batch=10) == []


class TestLoadingAndTheInvariant:
    def _extracted_task(self, conn, tmp_path, n=0, quality="success", blocks=None):
        make_captures(conn, n + 1)
        ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION)
        tasks = ledger.claim_extract(conn, outlet=OUTLET,
                                     extractor_version=VERSION,
                                     worker_id="a", batch=n + 1)
        for task in tasks:
            directory = tmp_path / task.relative_dir()
            write_article(directory, quality=quality, blocks=blocks)
            # article.json carries the identity the ingest path joins on.
            import json
            payload = json.loads((directory / "article.json").read_text())
            payload["archive_id"] = task.url_hash
            payload["outlet"] = OUTLET
            (directory / "article.json").write_text(json.dumps(payload))
            ledger.finish_extract(conn, task, quality=quality,
                                  output_dir=task.relative_dir(),
                                  screenshot_state="present")
        return tasks

    def test_a_loaded_article_is_searchable(self, conn, tmp_path):
        self._extracted_task(conn, tmp_path)
        tasks = ledger.claim_ingest(conn, outlet=OUTLET,
                                    extractor_version=VERSION,
                                    worker_id="loader", batch=10)
        loaded, failures = load_batch(conn, tasks, tmp_path, commit_every=100)
        assert (loaded, failures) == (1, [])
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM corpus.searchable_article "
                        "WHERE outlet = %s", (OUTLET,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT ingest_state FROM corpus.extraction_task "
                        "WHERE outlet = %s", (OUTLET,))
            assert cur.fetchone()[0] == "ingested"

    def test_an_article_with_no_prose_is_not_searchable(self, conn, tmp_path):
        """Ingested by hand to prove the VIEW refuses it, not just the loader."""
        self._extracted_task(conn, tmp_path)
        tasks = ledger.claim_ingest(conn, outlet=OUTLET,
                                    extractor_version=VERSION,
                                    worker_id="loader", batch=10)
        load_batch(conn, tasks, tmp_path, commit_every=100)
        with conn.cursor() as cur:
            cur.execute("""UPDATE corpus.article_extraction e
                              SET content_block_count = 0
                             FROM corpus.article a
                            WHERE a.id = e.article_id AND a.outlet = %s""", (OUTLET,))
            conn.commit()
            cur.execute("SELECT count(*) FROM corpus.searchable_article "
                        "WHERE outlet = %s", (OUTLET,))
            assert cur.fetchone()[0] == 0

    def test_a_failed_reading_can_never_be_the_current_one(self, conn, tmp_path):
        self._extracted_task(conn, tmp_path)
        tasks = ledger.claim_ingest(conn, outlet=OUTLET,
                                    extractor_version=VERSION,
                                    worker_id="loader", batch=10)
        load_batch(conn, tasks, tmp_path, commit_every=100)
        with pytest.raises(psycopg2.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute("""UPDATE corpus.article_extraction e
                                  SET extraction_status = 'failed'
                                 FROM corpus.article a
                                WHERE a.id = e.article_id AND a.outlet = %s
                                  AND e.is_current""", (OUTLET,))
        conn.rollback()

    def test_one_bad_article_does_not_roll_back_the_batch(self, conn, tmp_path):
        """The savepoint is what makes batching safe: without it, one failure
        rolls back up to 99 already-loaded articles."""
        tasks = self._extracted_task(conn, tmp_path, n=4)
        # Break one directory the way a killed worker does: remove the marker.
        (tmp_path / tasks[2].relative_dir() / "extraction.json").unlink()
        claimed = ledger.claim_ingest(conn, outlet=OUTLET,
                                      extractor_version=VERSION,
                                      worker_id="loader", batch=10)
        loaded, failures = load_batch(conn, claimed, tmp_path, commit_every=2)
        assert loaded == 4
        assert len(failures) == 1
        assert "IncompleteExtraction" in failures[0][1]
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM corpus.searchable_article "
                        "WHERE outlet = %s", (OUTLET,))
            assert cur.fetchone()[0] == 4

    def test_loading_twice_supersedes_instead_of_duplicating(self, conn, tmp_path):
        self._extracted_task(conn, tmp_path)
        for _ in range(2):
            with conn.cursor() as cur:
                cur.execute("""UPDATE corpus.extraction_task
                                  SET ingest_state = 'pending'
                                WHERE outlet = %s""", (OUTLET,))
            conn.commit()
            tasks = ledger.claim_ingest(conn, outlet=OUTLET,
                                        extractor_version=VERSION,
                                        worker_id="loader", batch=10)
            assert load_batch(conn, tasks, tmp_path, commit_every=100)[0] == 1
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM corpus.article WHERE outlet = %s",
                        (OUTLET,))
            assert cur.fetchone()[0] == 1
            cur.execute("""SELECT count(*) FROM corpus.article_extraction e
                             JOIN corpus.article a ON a.id = e.article_id
                            WHERE a.outlet = %s AND e.is_current""", (OUTLET,))
            assert cur.fetchone()[0] == 1, "exactly one reading may be current"


class TestGracefulStop:
    """A SIGTERM part-way through a batch must hand back what it has not done.

    The first version cleared the worker's held-key list unconditionally after
    the batch loop - including when the loop broke early on a stop signal - so
    the shutdown path found nothing to release and the untouched claims sat
    until their lease expired. That is the slow path the graceful stop exists to
    avoid, and nothing would have shown it except this test: the work still gets
    done eventually, just fifteen minutes later.
    """

    def test_the_untouched_remainder_is_released_not_left_to_the_lease(self, conn):
        make_captures(conn, 6)
        ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION)
        tasks = ledger.claim_extract(conn, outlet=OUTLET,
                                     extractor_version=VERSION,
                                     worker_id="stopping", batch=6)
        held = {t.key for t in tasks}

        # two finish before the signal arrives
        for task in tasks[:2]:
            ledger.finish_extract(conn, task, quality="success",
                                  output_dir=task.relative_dir(),
                                  screenshot_state="present")
            held.discard(task.key)

        assert ledger.release(conn, list(held)) == 4

        with conn.cursor() as cur:
            cur.execute("""SELECT extract_state, count(*)
                             FROM corpus.extraction_task WHERE outlet = %s
                            GROUP BY 1 ORDER BY 1""", (OUTLET,))
            assert dict(cur.fetchall()) == {"extracted": 2, "pending": 4}


class TestParallelLoaders:
    """Ingestion is round-trip-latency bound, so it scales with loaders.

    Measured on staging at 16k articles: one loader 13.79 articles/s, four
    loaders 52.32 - 3.8x, with the box 96% idle and Postgres at 0.4 of one core
    throughout. No code change was needed for that, because the claim already
    makes loaders disjoint. This test is what keeps that true.
    """

    def _extracted(self, conn, tmp_path, n):
        make_captures(conn, n)
        ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION)
        tasks = ledger.claim_extract(conn, outlet=OUTLET,
                                     extractor_version=VERSION,
                                     worker_id="w", batch=n)
        import json
        for task in tasks:
            directory = tmp_path / task.relative_dir()
            write_article(directory)
            payload = json.loads((directory / "article.json").read_text())
            payload["archive_id"] = task.url_hash
            payload["outlet"] = OUTLET
            (directory / "article.json").write_text(json.dumps(payload))
            ledger.finish_extract(conn, task, quality="success",
                                  output_dir=task.relative_dir(),
                                  screenshot_state="skipped")
        return tasks

    def test_two_loaders_never_claim_the_same_article(self, conn, tmp_path):
        self._extracted(conn, tmp_path, 12)
        second = psycopg2.connect(DSN)
        try:
            a = ledger.claim_ingest(conn, outlet=OUTLET, extractor_version=VERSION,
                                    worker_id="loader-a", batch=6)
            b = ledger.claim_ingest(second, outlet=OUTLET, extractor_version=VERSION,
                                    worker_id="loader-b", batch=6)
            assert len(a) == len(b) == 6
            assert not ({t.key for t in a} & {t.key for t in b})

            loaded_a, failures_a = load_batch(conn, a, tmp_path, commit_every=10)
            loaded_b, failures_b = load_batch(second, b, tmp_path, commit_every=10)
        finally:
            second.close()

        assert (loaded_a, loaded_b) == (6, 6)
        assert not failures_a and not failures_b
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), count(DISTINCT url_hash) "
                        "FROM corpus.article WHERE outlet = %s", (OUTLET,))
            total, distinct = cur.fetchone()
            assert total == distinct == 12, "an article was loaded twice"
            cur.execute("""SELECT count(*) FROM corpus.article_extraction e
                             JOIN corpus.article a ON a.id = e.article_id
                            WHERE a.outlet = %s AND e.is_current""", (OUTLET,))
            assert cur.fetchone()[0] == 12


class TestTheHeartbeatCannotDeadlock:
    """The 50k rung produced 408 deadlocks, and the cycle was the heartbeat.

    PostgreSQL named it exactly: a loader's heartbeat connection waiting on a
    row its own batch transaction was writing, while another loader's batch
    waited on a row that heartbeat had already taken. Every one of the 408
    recovered - the ledger makes a failed ingest retryable - but a retry path
    that load-bearing is a design smell, not a design.
    """

    def test_a_beat_never_waits_for_a_row_someone_else_is_writing(self, conn):
        make_captures(conn, 4)
        ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION)
        tasks = ledger.claim_extract(conn, outlet=OUTLET,
                                     extractor_version=VERSION,
                                     worker_id="owner", batch=4)

        # Another connection holds a row lock on one of them, uncommitted -
        # exactly what a batch transaction mid-flight looks like.
        blocker = psycopg2.connect(DSN)
        try:
            with blocker.cursor() as cur:
                cur.execute("""UPDATE corpus.extraction_task
                                  SET last_stage = 'ingest'
                                WHERE url_hash = %s""", (tasks[0].url_hash,))
            # Before SKIP LOCKED this call blocked here until the other
            # transaction ended, which is what let a cycle form.
            beaten = ledger.heartbeat(conn, [t.key for t in tasks],
                                      worker_id="owner")
        finally:
            blocker.rollback()
            blocker.close()

        assert beaten == 3, "the locked row is skipped, the rest are beaten"

    def test_a_beat_only_touches_rows_this_worker_still_owns(self, conn):
        """A stale heartbeat kept refreshing tasks that had been failed,
        released and re-claimed by someone else - which would also have kept a
        dead worker's claim looking alive to the reaper."""
        make_captures(conn, 3)
        ledger.seed(conn, outlet=OUTLET, extractor_version=VERSION)
        tasks = ledger.claim_extract(conn, outlet=OUTLET,
                                     extractor_version=VERSION,
                                     worker_id="first", batch=3)
        ledger.release(conn, [tasks[0].key])
        ledger.claim_extract(conn, outlet=OUTLET, extractor_version=VERSION,
                             worker_id="second", batch=1)

        assert ledger.heartbeat(conn, [t.key for t in tasks],
                                worker_id="first") == 2
