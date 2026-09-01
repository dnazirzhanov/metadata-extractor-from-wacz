-- =====================================================================
-- 002  The spine: article, and the extractions that read it
-- =====================================================================
-- Identity, established by measurement rather than by the field name:
-- the extractor's `archive_id` is sha256(normalize_url(url)), byte-identical to
-- public.urls.url_hash. It identifies the ARTICLE URL, not a capture. The
-- capture layer already exists in public.archives (many rows per URL, with
-- wacz_sha256 as the evidence anchor).
--
--   urls.url_hash   the ARTICLE   one per URL        <- extractor's archive_id
--   archives.id     the CAPTURE   many per URL       <- the .wacz
--   article_extraction  the READING  many per capture <- extractor version N
--
-- Article metadata is corrected in place by re-extraction (a title is not a
-- citation target). Content is owned by an extraction and wholly regenerated.
-- Citations live in corpus.passage_reference (005), hanging off the article, so
-- re-extraction cannot cascade one away.
--
-- LOCK NOTE: creating corpus.article takes a SHARE ROW EXCLUSIVE lock on
-- public.urls for the duration of the statement, and that conflicts with the
-- ROW EXCLUSIVE that INSERT/UPDATE take - so crawler writes to `urls` block
-- while this runs. There is nothing to validate (the referencing table is
-- empty), so it is sub-second. Run with a short lock_timeout anyway, so this
-- fails fast instead of queueing behind a long transaction and holding the
-- crawler off.
-- =====================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------
-- corpus.article
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corpus.article (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- The article's identity, and the external key the Neo4j layer should use:
    -- deterministic from the URL, so a graph node can be minted without a
    -- round-trip to Postgres.
    --
    -- ON DELETE RESTRICT, deliberately NOT the CASCADE that archives and
    -- videos use. An archive row is derived data - losing it with its URL is
    -- harmless. This table is the root of a citation graph, and a CASCADE would
    -- let a urls prune silently delete passage_reference rows, which is
    -- evidence loss. Verified free today: nothing in the crawler, scripts or
    -- migrations ever deletes from urls (quarantine flags, never removes), and
    -- all 13 extracted articles resolve to existing urls rows.
    url_hash            text NOT NULL UNIQUE
                        REFERENCES public.urls (url_hash) ON DELETE RESTRICT,

    outlet              text NOT NULL,
    source_url          text NOT NULL,          -- the URL that was crawled
    canonical_url       text,                   -- what the page declared; captures redirect

    title               text,                   -- nullable: the extractor warns and continues
    subtitle            text,                   -- the standfirst
    description         text,

    authors             text[] NOT NULL DEFAULT '{}',
    publisher           text,
    section             text,
    language            text,
    tags                text[] NOT NULL DEFAULT '{}',

    -- Parsed for querying...
    published_at        timestamptz,
    updated_at_source   timestamptz,
    captured_at         timestamptz,
    -- ...and kept verbatim for evidence. The extractor keeps dates as the page
    -- wrote them on purpose: "an ISO-8601 conversion that silently mangles a
    -- timezone is worse than the original string."
    published_at_raw    text,
    updated_at_raw      text,

    -- Which reading is live. Nullable to break the circular dependency at
    -- insert time; the FK is added in 002b below, after article_extraction
    -- exists.
    current_extraction_id bigint,

    first_ingested_at   timestamptz NOT NULL DEFAULT now(),
    row_updated_at      timestamptz NOT NULL DEFAULT now(),

    -- Weighted so a title hit outranks a tag hit. Authors and tags are in here
    -- for fuzzy matching, which is why there is no separate author index.
    -- 'corpus.hungarian_ci' is spelled out because the two-argument
    -- to_tsvector is IMMUTABLE (the one-argument form is only STABLE and
    -- cannot be used in a generated column). The arrays go through
    -- corpus.text_array_to_string for the same reason - see 001.
    search_tsv          tsvector GENERATED ALWAYS AS (
                            setweight(to_tsvector('corpus.hungarian_ci',
                                      coalesce(title, '')), 'A')
                         || setweight(to_tsvector('corpus.hungarian_ci',
                                      coalesce(subtitle, '')), 'B')
                         || setweight(to_tsvector('corpus.hungarian_ci',
                                      coalesce(description, '')), 'B')
                         || setweight(to_tsvector('corpus.hungarian_ci',
                                      corpus.text_array_to_string(authors)), 'C')
                         || setweight(to_tsvector('corpus.hungarian_ci',
                                      corpus.text_array_to_string(tags)), 'C')
                        ) STORED
);

COMMENT ON COLUMN corpus.article.url_hash IS
    'sha256 of the normalized article URL. Same value as public.urls.url_hash '
    'and as the extractor''s (misnamed) archive_id. The cross-system identifier.';
COMMENT ON COLUMN corpus.article.published_at_raw IS
    'The publication date exactly as the page declared it. published_at is the '
    'parsed form for querying; this one is the evidence.';

-- ---------------------------------------------------------------------
-- corpus.article_extraction
-- ---------------------------------------------------------------------
-- One row per extractor run. Extraction lifecycle lives HERE and not on
-- article, because "which version produced this" is a property of a run, and
-- because a new run must be insertable before it becomes current.
CREATE TABLE IF NOT EXISTS corpus.article_extraction (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    article_id          bigint NOT NULL
                        REFERENCES corpus.article (id) ON DELETE CASCADE,

    extractor_version   text NOT NULL,          -- 'causalia-article-extractor/2.0.0'
    extraction_status   text NOT NULL
                        CHECK (extraction_status IN ('success', 'partial', 'failed')),
    extracted_at        timestamptz NOT NULL,

    -- Which capture was read. Not emitted by the extractor (hashing 36 TB a
    -- second time was refused on purpose); ingestion copies it from
    -- archives.wacz_sha256, which is already populated. Makes "re-extract
    -- everything read from a capture we have since replaced" a query.
    wacz_sha256         text,
    archive_row_id      bigint REFERENCES public.archives (id) ON DELETE SET NULL,

    is_current          boolean NOT NULL DEFAULT false,

    ingested_at         timestamptz NOT NULL DEFAULT now()
);

-- Exactly one live reading per article, enforced by the database rather than by
-- hope. This is also what makes the ingestion flip atomic and makes two
-- concurrent ingestions of the same article unable to both win.
CREATE UNIQUE INDEX IF NOT EXISTS article_extraction_one_current
    ON corpus.article_extraction (article_id)
    WHERE is_current;

-- Now that article_extraction exists, close the loop from article.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'article_current_extraction_fk') THEN
        EXECUTE 'ALTER TABLE corpus.article
                 ADD CONSTRAINT article_current_extraction_fk
                 FOREIGN KEY (current_extraction_id)
                 REFERENCES corpus.article_extraction (id) ON DELETE SET NULL';
    END IF;
END
$$;

INSERT INTO corpus.schema_migrations (version) VALUES ('002')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
