-- =====================================================================
-- 025  Make "usable" a fact the database holds, not one the loader remembers
-- =====================================================================
-- Two small additions, both in service of the searchability invariant that 026
-- expresses as a view.
--
-- 1. content_block_count on article_extraction.
--    An article with no content block has no citable passage. Before the
--    quality contract landed it was recorded as `partial`, ingested, and
--    answered searches on its metadata vector alone - 1.28% of mandiner,
--    roughly 5,400 articles. The count is denormalised DELIBERATELY: the
--    alternative is an EXISTS over content_block for every candidate article in
--    the search path, and that path already spends 2.7 s of a 3.0 s query on
--    per-candidate work. It is written in the same transaction as the blocks it
--    counts, so it cannot drift.
--
-- 2. A CHECK that a failed reading can never be the current one.
--    `article_extraction_one_current` (migration 002) already guarantees
--    exactly one current reading per article. It does not say that reading has
--    to be usable. This does.
--
-- SAFETY OF THE CHECK ON EXISTING DATA: no producer could emit
-- extraction_status='failed' until 2026-09-11 - every failure path in the
-- extractor returned no output directory, so no extraction.json was written at
-- all. Any database built before then therefore holds only 'success' and
-- 'partial' rows, and the constraint validates trivially. If it does NOT
-- validate, that is a genuine finding and the migration should stop.
-- =====================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

ALTER TABLE corpus.article_extraction
    ADD COLUMN IF NOT EXISTS content_block_count integer NOT NULL DEFAULT 0;

COMMENT ON COLUMN corpus.article_extraction.content_block_count IS
    'Prose blocks this reading produced. Written with the blocks, in the same '
    'transaction. corpus.searchable_article tests it so the search path does '
    'not need a per-candidate EXISTS over content_block.';

-- Backfill for databases that already hold readings. A no-op on a fresh
-- production database (the table is empty) and idempotent everywhere: it
-- recomputes rather than incrementing.
UPDATE corpus.article_extraction e
   SET content_block_count = c.n
  FROM (SELECT extraction_id, count(*) AS n
          FROM corpus.content_block
         WHERE block_type NOT IN ('image', 'video')
           AND coalesce(btrim(block_text), '') <> ''
         GROUP BY extraction_id) c
 WHERE c.extraction_id = e.id
   AND e.content_block_count IS DISTINCT FROM c.n;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'extraction_current_is_usable') THEN
        EXECUTE 'ALTER TABLE corpus.article_extraction
                 ADD CONSTRAINT extraction_current_is_usable
                 CHECK (NOT (is_current AND extraction_status = ''failed''))';
    END IF;
END
$$;

DO $verify$
DECLARE
    article_id bigint;
    extraction_id bigint;
BEGIN
    -- 1. The column exists and is NOT NULL, so "unknown" is not a third state.
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'corpus'
                      AND table_name = 'article_extraction'
                      AND column_name = 'content_block_count'
                      AND is_nullable = 'NO') THEN
        RAISE EXCEPTION '025: content_block_count missing or nullable';
    END IF;

    -- 2. The constraint genuinely refuses a failed current reading. Proved by
    --    attempting it, not by reading the catalogue - a constraint that exists
    --    and does not fire is worse than none.
    IF EXISTS (SELECT 1 FROM public.urls LIMIT 1) THEN
        INSERT INTO corpus.article (url_hash, outlet, source_url)
        SELECT url_hash, outlet, 'https://example.invalid/025-verify'
          FROM public.urls LIMIT 1
        ON CONFLICT (url_hash) DO NOTHING
        RETURNING id INTO article_id;

        IF article_id IS NOT NULL THEN
            BEGIN
                INSERT INTO corpus.article_extraction
                    (article_id, extractor_version, extraction_status,
                     extracted_at, is_current)
                VALUES (article_id, '025-verify', 'failed', now(), true)
                RETURNING id INTO extraction_id;
                RAISE EXCEPTION
                    '025: a failed reading was allowed to be current';
            EXCEPTION WHEN check_violation THEN
                NULL;                          -- refused, which is the point
            END;
            DELETE FROM corpus.article WHERE id = article_id;
        END IF;
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('025')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
