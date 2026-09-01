-- =====================================================================
-- 006  Indexes
-- =====================================================================
-- Two kinds, and they are justified differently.
--
--   QUERY INDEXES     serve a named read. Each one below says which.
--   FK-MAINTENANCE    serve the referential action on DELETE. Postgres does NOT
--   INDEXES           create these automatically - it indexes the REFERENCED
--                     side (the parent key), never the referencing side. Without
--                     them, every parent delete is a sequential scan of the
--                     child table.
--
-- The second kind matters far more here than it looks. Re-extraction deletes a
-- superseded extraction, which cascades into content_block, article_image,
-- article_video and article_link, and fires ON DELETE SET NULL on
-- article.current_extraction_id, content_block.image_id/video_id,
-- article_link.content_block_id and passage_reference.content_block_id. That
-- happens once per re-extracted article - up to 4.2M times. An unindexed
-- referencing column would turn each one into a scan of a 58M-row table.
--
-- WHY PLAIN CREATE INDEX AND NOT CONCURRENTLY: at migration time these tables
-- are empty, so each statement is instant and CONCURRENTLY would only add
-- overhead and forbid the surrounding transaction. If you instead plan to bulk
-- backfill first and index afterwards - which is the right order for a
-- multi-day initial ingestion, since it avoids maintaining GIN on every insert
-- - then skip this file during setup and apply it later with CONCURRENTLY,
-- outside a transaction, one statement at a time.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- corpus.article
-- ---------------------------------------------------------------------
-- url_hash and the (article_id, kind) pairs already have implicit unique
-- indexes from their UNIQUE constraints. Not duplicated here.

-- Resolve a pasted URL that is the canonical rather than the crawled one.
-- Non-unique: several source URLs can share one canonical.
CREATE INDEX IF NOT EXISTS article_canonical_url_idx
    ON corpus.article (canonical_url);

-- "latest from origo.hu" - the default browse, and per-outlet corpus stats.
CREATE INDEX IF NOT EXISTS article_outlet_published_idx
    ON corpus.article (outlet, published_at DESC NULLS LAST);

-- search_articles(query).
CREATE INDEX IF NOT EXISTS article_search_tsv_idx
    ON corpus.article USING gin (search_tsv);

-- Exact tag filter: tags @> ARRAY['Tapolca']. NOT redundant with search_tsv - a
-- tsv match on 'Tapolca' also matches the word in a title or description, which
-- is wrong for a filter.
CREATE INDEX IF NOT EXISTS article_tags_idx
    ON corpus.article USING gin (tags);

-- FK maintenance: fires on every supersede, when the old extraction is deleted.
CREATE INDEX IF NOT EXISTS article_current_extraction_idx
    ON corpus.article (current_extraction_id)
    WHERE current_extraction_id IS NOT NULL;

-- ---------------------------------------------------------------------
-- corpus.article_extraction
-- ---------------------------------------------------------------------
-- Extraction history for an article; also FK maintenance for article deletes.
CREATE INDEX IF NOT EXISTS article_extraction_article_idx
    ON corpus.article_extraction (article_id);

-- "re-extract everything read from a capture we have since replaced."
CREATE INDEX IF NOT EXISTS article_extraction_wacz_idx
    ON corpus.article_extraction (wacz_sha256)
    WHERE wacz_sha256 IS NOT NULL;

-- FK maintenance only: public.archives is never deleted today, but urls
-- CASCADEs into it, so the path exists.
CREATE INDEX IF NOT EXISTS article_extraction_archive_row_idx
    ON corpus.article_extraction (archive_row_id)
    WHERE archive_row_id IS NOT NULL;

-- ---------------------------------------------------------------------
-- corpus.content_block
-- ---------------------------------------------------------------------
-- get_article_content in document order. The hot path.
CREATE INDEX IF NOT EXISTS content_block_article_order_idx
    ON corpus.content_block (article_id, block_index);

-- search_article_content(query), and article-level body search via the
-- semi-join in the search strategy. This is the primary body index; there is
-- deliberately no second vector over the whole article body.
CREATE INDEX IF NOT EXISTS content_block_text_tsv_idx
    ON corpus.content_block USING gin (text_tsv);

-- FK maintenance, and the reverse lookup "which block shows this image".
-- Partial: only ~7.6M of ~58M blocks are media blocks, so the index is small.
CREATE INDEX IF NOT EXISTS content_block_image_idx
    ON corpus.content_block (image_id) WHERE image_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS content_block_video_idx
    ON corpus.content_block (video_id) WHERE video_id IS NOT NULL;

-- (extraction_id, block_index) is already covered by the UNIQUE constraint,
-- whose leading column serves the extraction cascade.

-- ---------------------------------------------------------------------
-- corpus.article_image / corpus.article_video
-- ---------------------------------------------------------------------
-- get_article_images / get_article_videos, and FK maintenance for article
-- deletes. The extraction cascade is served by the UNIQUE (extraction_id,
-- local_ref) constraints.
CREATE INDEX IF NOT EXISTS article_image_article_idx
    ON corpus.article_image (article_id);
CREATE INDEX IF NOT EXISTS article_video_article_idx
    ON corpus.article_video (article_id);

-- "which articles embed this video?" - a real journalist question, and the
-- reason this pair is indexed rather than normalized into a dimension table.
CREATE INDEX IF NOT EXISTS article_video_platform_external_idx
    ON corpus.article_video (platform, external_id)
    WHERE external_id IS NOT NULL;

-- ---------------------------------------------------------------------
-- corpus.article_link
-- ---------------------------------------------------------------------
-- get_article_links, and FK maintenance for article deletes.
CREATE INDEX IF NOT EXISTS article_link_article_idx
    ON corpus.article_link (article_id);

-- FK maintenance: article_link has no UNIQUE on extraction_id, so without this
-- the extraction cascade scans the whole table.
CREATE INDEX IF NOT EXISTS article_link_extraction_idx
    ON corpus.article_link (extraction_id);

-- FK maintenance for the SET NULL when a content block is deleted.
CREATE INDEX IF NOT EXISTS article_link_block_idx
    ON corpus.article_link (content_block_id) WHERE content_block_id IS NOT NULL;

-- Inbound links: "what links to this article". One indexed join, and the
-- natural feed for the graph layer.
CREATE INDEX IF NOT EXISTS article_link_target_hash_idx
    ON corpus.article_link (target_url_hash) WHERE target_url_hash IS NOT NULL;

-- ---------------------------------------------------------------------
-- corpus.article_artifact
-- ---------------------------------------------------------------------
-- (article_id, kind) is covered by the UNIQUE constraint, which is also the
-- lookup for "fetch this article's readability.html path".
-- FK maintenance for the SET NULL when an extraction is deleted:
CREATE INDEX IF NOT EXISTS article_artifact_extraction_idx
    ON corpus.article_artifact (extraction_id) WHERE extraction_id IS NOT NULL;

-- ---------------------------------------------------------------------
-- corpus.passage_reference
-- ---------------------------------------------------------------------
-- Citations on an article; also FK maintenance for article deletes.
CREATE INDEX IF NOT EXISTS passage_reference_article_idx
    ON corpus.passage_reference (article_id);

-- FK maintenance: fires for every content block deleted on supersede.
CREATE INDEX IF NOT EXISTS passage_reference_block_idx
    ON corpus.passage_reference (content_block_id) WHERE content_block_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS passage_reference_verified_extraction_idx
    ON corpus.passage_reference (verified_against_extraction_id)
    WHERE verified_against_extraction_id IS NOT NULL;

-- The repair queue after a re-extraction. Partial, because the steady state is
-- that almost every reference is 'ok' and the interesting set is tiny.
CREATE INDEX IF NOT EXISTS passage_reference_needs_attention_idx
    ON corpus.passage_reference (resolution_status)
    WHERE resolution_status <> 'ok';

-- ---------------------------------------------------------------------
-- Deliberately NOT created, so the decision is recorded rather than
-- rediscovered:
--
--   article USING gin (authors)   fuzzy author search is already in search_tsv;
--                                 add when an EXACT byline filter is requested.
--   pg_trgm on title              substring/typo title search. The extension is
--                                 available but not installed; search_tsv
--                                 covers the stated need.
--   any article-level body tsvector   would duplicate 11.7 GB of text into a
--                                 second GIN index to serve a query the block
--                                 index already answers.
-- ---------------------------------------------------------------------

INSERT INTO corpus.schema_migrations (version) VALUES ('006')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
