-- =====================================================================
-- 026  SEARCHABLE is a property of the database, not of the query someone wrote
-- =====================================================================
-- The invariant:
--
--     SEARCHABLE(article) <=> the current reading is not `failed`
--                         AND that reading produced at least one prose block
--
-- Before this, nothing enforced it. scripts/search.py's candidate set is a
-- union of three branches, and only two of them - content blocks and image
-- captions - are scoped to the current extraction. The third reads
-- corpus.article.search_tsv directly, with no join to article_extraction at
-- all, so an article whose reading was partial or failed still matched on its
-- title, subtitle, description, authors and tags.
--
-- THREE LAYERS, AND THIS IS THE THIRD
-- -----------------------------------
--   1. By construction: the loader never ingests an `invalid` extraction, so
--      no row exists for a query to reach. Cheapest and strongest.
--   2. By constraint: 025's CHECK forbids a failed reading from being current.
--   3. By query shape: this view, which every user-facing query joins.
--
-- Layer 1 alone would be enough for data ingested from today. It is not enough
-- for data already in cx-pg-d1 and cx-pg-bench, and it is not enough against a
-- future loader that forgets. A rule enforced in one place is a rule that
-- drifts.
--
-- WHY THIS VIEW IS A GATE LIST AND NOT `SELECT a.*`
-- -------------------------------------------------
-- The obvious version selects every column of corpus.article, which reads
-- better and is WRONG here: it would make the view depend on the generated
-- column `article.search_tsv`, and migrations 007 and 016 both DROP and rebuild
-- that column. A dependent view turns those into
--
--     ERROR: cannot drop column search_tsv ... view corpus.searchable_article
--            depends on it
--
-- which breaks the property every migration in this directory is written for -
-- `psql -f` twice is a no-op - and which CI enforces by applying all of them
-- twice. Found exactly that way, by scripts/validate_migrations.sh, before this
-- view ever reached a database that mattered.
--
-- So the view carries identity only. Queries join it and read their columns
-- from the table, which costs one primary-key join and keeps every migration
-- re-runnable.
--
-- PERFORMANCE: both tests are on the extraction row the article already points
-- at - a primary-key lookup plus an integer comparison. No EXISTS over
-- content_block, no second index scan, and the article's own GIN index is still
-- what serves the tsvector match.
-- =====================================================================

BEGIN;

CREATE OR REPLACE VIEW corpus.searchable_article AS
SELECT a.id,
       a.url_hash,
       a.outlet,
       a.current_extraction_id
  FROM corpus.article a
  JOIN corpus.article_extraction e ON e.id = a.current_extraction_id
 WHERE e.extraction_status <> 'failed'
   AND e.content_block_count > 0;

COMMENT ON VIEW corpus.searchable_article IS
    'Which articles a query may return: a current reading that is not failed '
    'and that produced at least one prose block. Identity columns only - it '
    'must not depend on a generated tsvector column, because 007 and 016 drop '
    'and rebuild those. scripts/search.py joins this view; it never filters on '
    'corpus.article alone.';

DO $verify$
BEGIN
    -- 1. Identity only. A tsvector column here would break re-applying 007/016.
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'corpus'
                  AND table_name = 'searchable_article'
                  AND data_type = 'tsvector') THEN
        RAISE EXCEPTION
            '026: the view exposes a tsvector column; 007 and 016 can no '
            'longer drop and rebuild the generated columns';
    END IF;

    -- 2. An article with no current reading is not searchable. This is the hole
    --    the metadata branch of the candidate set used to go through.
    IF EXISTS (SELECT 1 FROM corpus.searchable_article v
                 JOIN corpus.article a ON a.id = v.id
                WHERE a.current_extraction_id IS NULL) THEN
        RAISE EXCEPTION '026: an article with no current reading is searchable';
    END IF;

    -- 3. Nothing with an unusable reading is searchable.
    IF EXISTS (SELECT 1 FROM corpus.searchable_article v
                 JOIN corpus.article_extraction e
                   ON e.id = v.current_extraction_id
                WHERE e.content_block_count = 0
                   OR e.extraction_status = 'failed') THEN
        RAISE EXCEPTION '026: an unusable reading is searchable';
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('026')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
