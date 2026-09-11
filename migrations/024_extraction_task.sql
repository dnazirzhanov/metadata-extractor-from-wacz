-- =====================================================================
-- 024  The work ledger: what has been attempted, by whom, and how it went
-- =====================================================================
-- Until now nothing recorded that an article had been processed. The frontier
-- was the filesystem: the extractor CLI walked a tree, and the loader did
-- `sorted(root.rglob("content.json"))`, which materialises every article
-- directory before the first INSERT. An interrupted run therefore restarted
-- from zero, and a 418k run WILL be interrupted. The precedent is not
-- hypothetical - cx-pg-d1 holds an arbitrary 16,008-article prefix of a
-- 50,000-article run whose ingest was killed and could not be resumed.
--
-- This table is modelled directly on the archiver's own claim/lease design in
-- causalia-final (migrations 003 and 004), which has driven 6.3M captures
-- across two machines. Both of its recorded mistakes are avoided here on
-- purpose; each is marked below.
--
-- STATE, DELIBERATELY SPLIT IN THREE
-- ----------------------------------
-- extract_state     owned by the extraction worker
-- ingest_state      owned by the loader
-- screenshot_state  owned by the screenshot stage, which is independently
--                   rerunnable (the .wacz is always the source of truth, and
--                   an existing screenshot is never deleted)
--
-- `quality` is the verdict from the extractor's quality contract and is NOT a
-- state - it does not transition, it is recorded once when the extraction
-- completes, and it is what the loader consults before ingesting anything.
--
-- There is deliberately no `extracting` or `validating` state. Neither is
-- recoverable information: a worker holding a lease is `claimed` whatever it is
-- doing inside, and validation happens in the same process, in memory, before
-- the commit marker is written. Persisting them would cost two UPDATE
-- round-trips per article across 418,343 articles and buy a distinction no
-- recovery path can act on.
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS corpus.extraction_task (
    -- Identity is the article URL plus the extractor that read it. Same
    -- extractor version = the same work item, so a re-run is a no-op; a new
    -- version is a NEW row, which is what makes re-extraction expressible
    -- without deleting the record of the previous reading.
    url_hash            text NOT NULL
                        REFERENCES public.urls (url_hash) ON DELETE RESTRICT,
    extractor_version   text NOT NULL,

    -- Which capture this task is about. Copied from public.archives at seed
    -- time so the ledger stays answerable even if a capture row is later
    -- superseded; wacz_sha256 makes "re-extract everything read from a capture
    -- we have since replaced" a query rather than a filesystem walk.
    archive_row_id      bigint REFERENCES public.archives (id) ON DELETE SET NULL,
    wacz_sha256         text,
    outlet              text NOT NULL,

    extract_state       text NOT NULL DEFAULT 'pending'
                        CHECK (extract_state IN
                            ('pending','claimed','extracted','failed','quarantined')),
    quality             text
                        CHECK (quality IN ('success','partial_valid','invalid')),
    ingest_state        text NOT NULL DEFAULT 'pending'
                        CHECK (ingest_state IN
                            ('pending','claimed','ingested','failed')),
    -- `skipped` means the stage was not run and there is work to do later;
    -- `none_in_archive` means we looked and the capture holds none, which is
    -- normal for everything crawled before 2026-08-12 and is NOT work. A
    -- backfill that cannot tell those apart re-reads the whole corpus.
    screenshot_state    text NOT NULL DEFAULT 'pending'
                        CHECK (screenshot_state IN
                            ('pending','claimed','present','none_in_archive',
                             'failed','skipped')),

    -- LEASE, NOT AGE. The archiver learned this the hard way (its migration
    -- 003): "reset anything in_progress older than an hour" both missed rows
    -- stranded by a worker that restarted within seconds, and stole rows from
    -- healthy long-running batches, producing two live workers on one item.
    -- A worker stamps heartbeat_at on the rows it holds; recovery asks whether
    -- anyone has touched the row recently, never how old it is.
    worker_id           text,
    claimed_at          timestamptz,
    heartbeat_at        timestamptz,

    -- Incremented AT CLAIM TIME, not at failure: a worker that dies without
    -- reporting anything must still burn an attempt, or an archive that
    -- reliably kills its worker is retried forever.
    attempts            smallint NOT NULL DEFAULT 0,
    last_stage          text,
    last_error          text,

    -- Relative to the extraction output root, never absolute: the same corpus
    -- is mounted at three different paths on three machines.
    output_dir          text,
    extracted_at        timestamptz,
    ingested_at         timestamptz,
    row_updated_at      timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (url_hash, extractor_version)
);

COMMENT ON TABLE corpus.extraction_task IS
    'The work ledger: one row per (article, extractor version). The frontier is '
    'a query against this table, never a filesystem walk.';

-- ---------------------------------------------------------------------
-- At most one live claim per URL
-- ---------------------------------------------------------------------
-- The archiver's migration 004 exists because SKIP LOCKED alone was not
-- enough there: it locked rows in `urls` while the thing that excluded a URL
-- was a row in `archives`, read under a READ COMMITTED statement snapshot. A
-- loser's snapshot could predate the winner's commit, and a live 16-worker
-- pool produced 320 claims covering 141 distinct URLs.
--
-- The claim here locks and tests the SAME rows of the SAME table, which is the
-- case SKIP LOCKED actually covers. This index is the structural backstop
-- anyway: correctness should not rest on that argument staying true as the
-- claim query is edited.
CREATE UNIQUE INDEX IF NOT EXISTS extraction_task_one_live_claim
    ON corpus.extraction_task (url_hash)
    WHERE extract_state = 'claimed' OR ingest_state = 'claimed'
       OR screenshot_state = 'claimed';

-- Recovery scans only live claims, so it stays small as the table reaches
-- millions of finished rows.
CREATE INDEX IF NOT EXISTS extraction_task_liveness
    ON corpus.extraction_task (heartbeat_at)
    WHERE extract_state = 'claimed' OR ingest_state = 'claimed'
       OR screenshot_state = 'claimed';

-- The extraction frontier: claimable work for one outlet. 'failed' is in the
-- predicate because a failure is RETRYABLE - `attempts` bounds the retries and
-- 'quarantined' is the terminal state. Leaving it out made every failure
-- permanent, which a test caught before any of this ran for real.
CREATE INDEX IF NOT EXISTS extraction_task_extract_frontier
    ON corpus.extraction_task (outlet, url_hash)
    WHERE extract_state IN ('pending', 'failed');

-- The ingest frontier: extracted, usable, not yet loaded.
CREATE INDEX IF NOT EXISTS extraction_task_ingest_frontier
    ON corpus.extraction_task (outlet, url_hash)
    WHERE extract_state = 'extracted' AND ingest_state IN ('pending', 'failed');

-- The screenshot backfill frontier: extracted articles whose screenshot stage
-- was switched off for the urgent run.
CREATE INDEX IF NOT EXISTS extraction_task_screenshot_frontier
    ON corpus.extraction_task (outlet, url_hash)
    WHERE screenshot_state IN ('pending', 'skipped', 'failed');

-- "Why did these fail?" answered in SQL rather than by walking directories.
CREATE INDEX IF NOT EXISTS extraction_task_trouble
    ON corpus.extraction_task (outlet, extract_state, quality)
    WHERE extract_state IN ('failed','quarantined') OR quality = 'invalid';

DO $verify$
DECLARE
    n integer;
BEGIN
    -- 1. The live-claim index must actually forbid a second claim.
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                    WHERE schemaname = 'corpus'
                      AND indexname = 'extraction_task_one_live_claim') THEN
        RAISE EXCEPTION '024: the live-claim index was not created';
    END IF;

    -- 2. The state vocabularies are constrained, not free text.
    SELECT count(*) INTO n FROM pg_constraint
     WHERE conrelid = 'corpus.extraction_task'::regclass AND contype = 'c';
    IF n < 4 THEN
        RAISE EXCEPTION '024: expected CHECK constraints on every state column, found %', n;
    END IF;

    -- 3. Identity is (url_hash, extractor_version), so the same version cannot
    --    be queued twice while a new version still can.
    IF NOT EXISTS (
        SELECT 1 FROM pg_index i
          JOIN pg_class c ON c.oid = i.indexrelid
         WHERE i.indrelid = 'corpus.extraction_task'::regclass
           AND i.indisprimary
           AND array_length(i.indkey::int2[], 1) = 2) THEN
        RAISE EXCEPTION '024: the primary key is not (url_hash, extractor_version)';
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('024')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
