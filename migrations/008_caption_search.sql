-- =====================================================================
-- 008  Make image captions and alt text discoverable
-- =====================================================================
-- THE GAP
--
-- `content_block.text_tsv` is built from `block_text`, and `block_text` is NULL
-- for image and video blocks by design - an image block points at a row in
-- article_image, it does not carry prose. Measured on the 50-article evaluation
-- corpus: 100 of 648 blocks (15%) therefore hold an empty vector and can never
-- match a query, and the 64 image captions and alt texts in that corpus are in
-- no search vector at all.
--
-- That text is written by a human to describe a photograph. For a journalism
-- tool it is some of the most specific text in the article, and it was
-- invisible.
--
-- WHERE IT HAD TO GO, AND WHY NOT THE TWO OBVIOUS PLACES
--
-- NOT `article.search_tsv`. A STORED generated column may only reference
-- columns of its own row - no subqueries, no aggregates - so a per-article
-- vector cannot pull text out of the per-image table. This is a hard
-- constraint, not a preference.
--
-- NOT `content_block.block_text`. That column is load-bearing for citation:
-- the whole chain rests on `block_text = normalize_text(element)`, checked
-- against readability.html every time a passage is resolved. A caption lives in
-- a <figcaption>, which is not the block's element, so writing it into
-- block_text would break the equality that makes a citation verifiable. The one
-- invariant this system cannot trade away.
--
-- So it goes on `article_image`, reached by the same semi-join that already
-- serves body search (docs/postgres-schema.md D.2). Same pattern, same shape.
--
-- DISCOVERABLE, NOT YET CITABLE
--
-- This migration deliberately stops at discovery. A caption match tells you
-- WHICH article and WHICH image, and that is all it is asked to do. Whether an
-- image caption should carry its own selector - a <figcaption> XPath with its
-- own offsets and quote - is a separate question about what a citation may
-- point at, and it should be answered on purpose rather than acquired as a
-- side effect of an index.
-- =====================================================================

BEGIN;

SET lock_timeout = '5s';

-- ---------------------------------------------------------------------
-- The caption vector
-- ---------------------------------------------------------------------
-- Built by corpus.search_vector, so a caption is found by exactly the query
-- expansion that finds body text - one set of lexeme rules for the whole
-- corpus, per 007.
--
-- Spelled with `||` and coalesce rather than concat_ws because concat_ws is
-- declared STABLE, not IMMUTABLE (it depends on the type output functions of
-- its variadic arguments), and a STORED generated column rejects it. Exactly
-- the trap array_to_string set in 001; the text `||` operator is immutable.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_attribute
                    WHERE attrelid = 'corpus.article_image'::regclass
                      AND attname = 'caption_tsv' AND NOT attisdropped) THEN
        ALTER TABLE corpus.article_image ADD COLUMN caption_tsv tsvector
            GENERATED ALWAYS AS (
                corpus.search_vector(
                    coalesce(caption, '') || ' ' ||
                    coalesce(alt, '')     || ' ' ||
                    coalesce(credit, ''))
            ) STORED;
    END IF;
END
$$;

COMMENT ON COLUMN corpus.article_image.caption_tsv IS
    'Caption + alt + credit, vectorised with corpus.search_vector. Makes the '
    'text DISCOVERABLE; it carries no selector, so a caption match is not yet '
    'a citable passage. See migrations/008.';

-- Partial: in the evaluation corpus 64 of 67 images have caption, alt or
-- credit, but an image with none of the three contributes an empty vector that
-- no query can match, and indexing those rows only makes the index bigger.
CREATE INDEX IF NOT EXISTS article_image_caption_tsv_idx
    ON corpus.article_image USING gin (caption_tsv)
    WHERE caption_tsv <> ''::tsvector;

-- ---------------------------------------------------------------------
-- What is deliberately NOT done here
-- ---------------------------------------------------------------------
-- corpus.article_video has `title` and `caption` columns with the identical
-- problem, and video blocks are equally invisible to search. They are left
-- alone because every video row in every corpus measured so far has both
-- columns NULL - 0 of 33 - so the column would ship unexercised and untested.
-- Add it the day the extractor starts filling them, with a corpus to verify it
-- against.
INSERT INTO corpus.schema_migrations (version) VALUES ('008')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
