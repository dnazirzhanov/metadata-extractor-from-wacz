-- =====================================================================
-- 003  The content of a reading: blocks, images, videos, links
-- =====================================================================
-- All four tables are owned by an extraction and are wholly regenerated when
-- one supersedes another. Nothing durable may point at these rows - see 005.
--
-- Order matters: images and videos are created before content_block, because a
-- block references them.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- corpus.article_image
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corpus.article_image (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    article_id      bigint NOT NULL REFERENCES corpus.article (id) ON DELETE CASCADE,
    extraction_id   bigint NOT NULL
                    REFERENCES corpus.article_extraction (id) ON DELETE CASCADE,

    -- 'image_001' - the extractor's positional handle, what content.json refers
    -- to, and the join key during ingestion.
    local_ref       text NOT NULL,

    file_path       text,           -- relative to the storage root; NULL when unavailable
    original_url    text,
    media_type      text,
    width           int,
    height          int,
    alt             text,
    caption         text,
    -- Kept although it is 100% NULL across the validated sample: it is one
    -- nullable column, and the alternative is discovering later that the
    -- extractor learned to fill it and having nowhere to put it.
    credit          text,
    is_available    boolean NOT NULL,

    UNIQUE (extraction_id, local_ref)
);

COMMENT ON COLUMN corpus.article_image.file_path IS
    'Relative path under the configured storage root, never absolute: the same '
    'corpus is mounted at different paths on milab2, milab4 and over sshfs.';

-- ---------------------------------------------------------------------
-- corpus.article_video
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corpus.article_video (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    article_id      bigint NOT NULL REFERENCES corpus.article (id) ON DELETE CASCADE,
    extraction_id   bigint NOT NULL
                    REFERENCES corpus.article_extraction (id) ON DELETE CASCADE,

    local_ref       text NOT NULL,          -- 'video_001'

    -- platform + external_id are named to match the columns public.videos
    -- already has. NOT unique here: the same video legitimately appears in many
    -- articles, and that is the point of the index in 006.
    platform        text,
    external_id     text,
    source_type     text NOT NULL,          -- extractor `type`; differs for html5/iframe
    canonical_url   text,                   -- the watch URL where derivable
    embed_url       text NOT NULL,          -- what the page actually embedded
    thumbnail_url   text,                   -- recorded, never fetched
    title           text,
    caption         text,

    file_path       text,                   -- NULL when nothing playable was written
    -- is_archived = true with file_path = NULL is a valid, meaningful state: an
    -- adaptive-stream (DASH/HLS) ladder is held in the capture but its rungs
    -- cannot be reassembled without ffmpeg, which the extractor does not have.
    is_archived     boolean NOT NULL,

    UNIQUE (extraction_id, local_ref)
);

COMMENT ON COLUMN corpus.article_video.is_archived IS
    'Are the bytes in the capture. TRUE with file_path NULL means held but not '
    'playable - an adaptive bitrate ladder needing reassembly.';

-- ---------------------------------------------------------------------
-- corpus.content_block
-- ---------------------------------------------------------------------
-- ~58M rows projected at 4.2M articles (13.8 blocks/article measured).
CREATE TABLE IF NOT EXISTS corpus.content_block (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    extraction_id   bigint NOT NULL
                    REFERENCES corpus.article_extraction (id) ON DELETE CASCADE,
    -- Denormalized from extraction: every content query filters by article, and
    -- routing the hot path through extraction would be a two-hop join for no
    -- benefit. Cannot drift - both are set once, together, by one insert.
    article_id      bigint NOT NULL REFERENCES corpus.article (id) ON DELETE CASCADE,

    -- The article's reading order. KEPT, for reasons that are not "the JSON has
    -- it": ordering by xpath is wrong (p[10] sorts before p[2] as text, and
    -- media blocks interleave), and UNIQUE + a contiguity assertion at ingestion
    -- catches a truncated extraction that would otherwise look like a short
    -- article. It is 4 bytes.
    --
    -- It is explicitly NOT identity. An extractor improvement shifts it -
    -- measured twice on this corpus (the lead-paragraph fix, and the link-strip
    -- fix which moved p[9] to p[8]). Living on an extraction-owned row is what
    -- stops it being mistaken for a durable article-level coordinate.
    block_index     int NOT NULL,

    block_type      text NOT NULL CHECK (block_type IN
                        ('paragraph', 'heading', 'image', 'video', 'quote', 'list')),
    xpath           text NOT NULL,          -- the address in readability.html

    -- Named block_text rather than "text" so nothing in a generated expression
    -- or a join has to disambiguate a column from a type name.
    block_text      text,                   -- NULL for image/video blocks
    heading_level   smallint CHECK (heading_level BETWEEN 2 AND 6),

    image_id        bigint REFERENCES corpus.article_image (id) ON DELETE SET NULL,
    video_id        bigint REFERENCES corpus.article_video (id) ON DELETE SET NULL,

    text_tsv        tsvector GENERATED ALWAYS AS (
                        to_tsvector('corpus.hungarian_ci', coalesce(block_text, ''))
                    ) STORED,

    UNIQUE (extraction_id, block_index),

    -- Shape rules the extractor already guarantees, restated so a future
    -- ingestion bug is caught at the boundary rather than discovered in a query.
    CONSTRAINT content_block_heading_level
        CHECK ((block_type = 'heading') = (heading_level IS NOT NULL)),
    CONSTRAINT content_block_media_ref
        CHECK ((block_type = 'image' AND video_id IS NULL)
            OR (block_type = 'video' AND image_id IS NULL)
            OR (block_type NOT IN ('image', 'video')
                AND image_id IS NULL AND video_id IS NULL)),
    CONSTRAINT content_block_text_presence
        CHECK ((block_type IN ('image', 'video')) OR (block_text IS NOT NULL))
);

-- ---------------------------------------------------------------------
-- corpus.article_link
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corpus.article_link (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    article_id          bigint NOT NULL REFERENCES corpus.article (id) ON DELETE CASCADE,
    extraction_id       bigint NOT NULL
                        REFERENCES corpus.article_extraction (id) ON DELETE CASCADE,
    content_block_id    bigint REFERENCES corpus.content_block (id) ON DELETE SET NULL,

    target_url          text NOT NULL,      -- as written in the href
    -- Canonicalized with the same function as article.url_hash. This is what
    -- turns links into a research capability: "which archived articles link to
    -- this one" becomes one indexed join, and it is the natural feed for the
    -- graph layer. NULL when the href cannot be canonicalized.
    target_url_hash     text,
    anchor_text         text NOT NULL,
    context             text,               -- the owning block's text
    is_internal         boolean NOT NULL,

    -- The selector. Five columns, identical to passage_reference (005).
    -- Nullable as a set: the extractor emits selector = null when the anchor
    -- text is not locatable in its own block's normalized text.
    selector_xpath      text,
    quote_start         int,
    quote_end           int,
    quote_exact         text,
    quote_prefix        text,
    quote_suffix        text,

    CONSTRAINT article_link_selector_complete CHECK (
        (selector_xpath IS NULL AND quote_start IS NULL
         AND quote_end IS NULL AND quote_exact IS NULL)
     OR (selector_xpath IS NOT NULL AND quote_start IS NOT NULL
         AND quote_end IS NOT NULL AND quote_exact IS NOT NULL
         AND quote_start >= 0 AND quote_end > quote_start)
    )
);

-- Deliberately NO unique constraint on (article_id, target_url): the same URL
-- legitimately appears more than once in an article with different anchor text.
-- The extractor dedupes on (href, text), not on href.

INSERT INTO corpus.schema_migrations (version) VALUES ('003')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
