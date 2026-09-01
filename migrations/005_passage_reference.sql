-- =====================================================================
-- 005  Passage references: the citation layer Neo4j points at
-- =====================================================================
-- The table the whole design exists to make possible.
--
-- A citation must NOT be a foreign key to a content block. Content blocks carry
-- positional block_index and positional xpath, both of which provably drift on
-- this corpus: the lead-paragraph fix shifted every index, and the link-strip
-- fix moved p[9] to p[8]. Measured over 402 passages, a single inserted
-- paragraph left only 43% of positional XPaths resolving to their intended
-- element - the other 57% resolved to the WRONG element, silently.
--
-- So a reference stores the full selector AS A VALUE. content_block_id is a
-- convenience pointer that is allowed to go stale (ON DELETE SET NULL), and
-- quote_exact is the authority.
--
-- Note what is absent: no FK to article_extraction. A citation is about the
-- article, not about a reading of it. That is the point.
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS corpus.passage_reference (
    -- uuid, not bigint, and this is the one place that argument applies: the
    -- claims layer must be able to mint a reference id BEFORE insert so a Neo4j
    -- Evidence node can be created without a round-trip to Postgres.
    -- gen_random_uuid() is built in on PG 13+; no pgcrypto needed.
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),

    article_id      bigint NOT NULL REFERENCES corpus.article (id) ON DELETE CASCADE,
    content_block_id bigint REFERENCES corpus.content_block (id) ON DELETE SET NULL,

    -- The selector. NOT NULL here, unlike on article_link: a citation with no
    -- selector is not a citation.
    selector_xpath  text NOT NULL,
    quote_start     int  NOT NULL,
    quote_end       int  NOT NULL,
    quote_exact     text NOT NULL,
    quote_prefix    text,
    quote_suffix    text,

    -- Never silently assumed. 'repaired' means the xpath drifted and was
    -- re-found by prefix+exact+suffix, and the corrected xpath was written back;
    -- 'quote_not_found' means the passage is not in the current document and a
    -- human must look. A drifted reference is never repointed.
    resolution_status text NOT NULL DEFAULT 'unverified'
                      CHECK (resolution_status IN
                          ('unverified', 'ok', 'repaired', 'quote_not_found')),
    last_verified_at  timestamptz,
    verified_against_extraction_id bigint
                      REFERENCES corpus.article_extraction (id) ON DELETE SET NULL,

    created_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT passage_reference_range CHECK (quote_start >= 0
                                          AND quote_end > quote_start),
    -- The offsets and the quote must agree with each other on their own terms,
    -- independent of any document.
    CONSTRAINT passage_reference_quote_length
        CHECK (length(quote_exact) = quote_end - quote_start)
);

COMMENT ON TABLE corpus.passage_reference IS
    'A citation to an exact passage. Survives re-extraction: the selector is a '
    'value, not a pointer. quote_exact is the authority; selector_xpath is '
    'positional and drifts.';

-- ---------------------------------------------------------------------
-- The wire format, assembled at the edge
-- ---------------------------------------------------------------------
-- Columns are the truth: they get CHECK constraints, plain equality lookups and
-- real planner statistics, and they cannot accept a malformed selector. JSONB
-- would need an expression index for the same lookup and would swallow
-- {"nonsense": true} silently.
--
-- A view is the hybrid: it produces the canonical shape for an API or MCP tool
-- with no stored duplication, and it cannot drift from the columns. A
-- GENERATED ... STORED jsonb column would duplicate the bytes for the same
-- result. `type` and `refinedBy.type` are constants in every record the
-- extractor emits, so they are re-added here rather than stored 4M times.
CREATE OR REPLACE VIEW corpus.passage_selector AS
SELECT p.id,
       p.article_id,
       p.content_block_id,
       p.resolution_status,
       a.url_hash,
       jsonb_build_object(
           'type',  'XPathSelector',
           'value', p.selector_xpath,
           'refinedBy', jsonb_build_object(
               'type',  'TextPositionSelector',
               'start', p.quote_start,
               'end',   p.quote_end),
           'quote', jsonb_strip_nulls(jsonb_build_object(
               'exact',  p.quote_exact,
               'prefix', p.quote_prefix,
               'suffix', p.quote_suffix))
       ) AS selector
FROM corpus.passage_reference p
JOIN corpus.article a ON a.id = p.article_id;

COMMENT ON VIEW corpus.passage_selector IS
    'passage_reference in the canonical W3C-style selector shape, for API/MCP '
    'responses. Read-only projection of the columns.';

INSERT INTO corpus.schema_migrations (version) VALUES ('005')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
