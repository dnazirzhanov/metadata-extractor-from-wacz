-- =====================================================================
-- 004  Article artifacts: paths, never bytes
-- =====================================================================
-- No binaries in Postgres. The corpus is ~36 TB on /mnt/hdd, the database lives
-- on the same disk and shares it with the WAL, and a full disk on this host is a
-- CORRECTNESS risk rather than merely an availability one. Moving 11.7 GB of
-- HTML plus terabytes of media into rows would multiply WAL volume and break
-- pg_dump for no gain. bytea and large objects are out.
--
-- Image and video FILES are not artifact rows. They already live in tables with
-- the domain metadata they need (width, platform), and a generic artifact row
-- would add a second place where a path can disagree with itself. This table is
-- for the document-level singletons.
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS corpus.article_artifact (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    article_id      bigint NOT NULL REFERENCES corpus.article (id) ON DELETE CASCADE,
    -- NULL for kind='wacz': the capture belongs to the crawler, not to any
    -- reading of it.
    extraction_id   bigint REFERENCES corpus.article_extraction (id) ON DELETE SET NULL,

    kind            text NOT NULL CHECK (kind IN
                        ('readability_html', 'original_html', 'screenshot', 'wacz')),

    -- RELATIVE to the configured storage root, never absolute. The same corpus
    -- is mounted at /mnt/hdd/... on milab2, over sshfs on milab4, and at a third
    -- path when read from a laptop; an absolute path would encode one machine's
    -- view and break on the others.
    file_path       text NOT NULL,

    -- A viewer needs it, and it is how "prefer a raster screenshot over the
    -- webp fallback" is expressed as queryable data rather than as a filename
    -- convention.
    media_type      text NOT NULL,

    byte_size       bigint,     -- storage accounting without stat-ing 4M files
    -- Only meaningful for the .wacz, where it is the evidence anchor and
    -- mirrors archives.wacz_sha256. Nullable elsewhere rather than hashing every
    -- HTML file for no consumer.
    sha256          text,

    -- The extractor writes exactly one of each kind per article, so the
    -- constraint makes that an enforced fact instead of an assumption.
    UNIQUE (article_id, kind)
);

COMMENT ON TABLE corpus.article_artifact IS
    'Document-level files by path. Because paths are relative and the .wacz is '
    'an ordinary row, retiring the .wacz layer later is a data-retention '
    'decision (DELETE WHERE kind = ''wacz''), not a schema change.';

COMMENT ON COLUMN corpus.article_artifact.media_type IS
    'image/png or image/jpeg for a Browsertrix capture; image/webp identifies '
    'the 2026-08-07 Playwright backfill fallback.';

INSERT INTO corpus.schema_migrations (version) VALUES ('004')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
