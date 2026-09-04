-- =====================================================================
-- 016  Keep the accent through stemming, so kór and kor stop being one word
-- =====================================================================
-- THE DEFECT
--
-- corpus.search_vector unaccents BOTH of its halves - the lemma half is
-- stem-then-unaccent, the surface half is unaccent-then-no-stem - so NOTHING in
-- the stored vector preserves an accent. Three distinct Hungarian words become
-- one lexeme. Measured on the 1,008-article evaluation corpus:
--
--     kör (circle) / kór (disease) / kor (age)  ->  'kor'  ->  166 articles EACH,
--                                                              the same 166
--     ügy (case)                                ->  'ugy'  ->  380
--     Viktor                                    -> 'viktor'->  101, of which 15
--                                                              contain no literal
--                                                              "viktor" at all
--
-- Corpus-wide: 639 collision groups covering 4,302 word forms.
--
-- WHAT PRESERVING THE ACCENT BUYS, MEASURED
--
-- All 639 groups separate once the accent survives stemming:
--
--     kór -> 'kór'      kor -> 'kor'      kör -> 'kör'
--     Viktória -> 'viktór'   vs   Viktor -> 'viktor'
--
-- párt / part is a PARTIAL case, and the measurement corrected an earlier guess
-- of mine. Bare "párt" and bare "part" do both stem to 'pár' with accents kept,
-- so they are not separated by their nominative forms - but their inflections
-- are ("partján" -> 'part'), and in practice the accent separates the families:
-- párt drops 193 -> 131 while part stays 193, and "a Duna partján" stops
-- answering "párt". What remains is over-stemming, not accent-folding: 'pár' is
-- also the lemma of pár (a couple), and "kormánypárt" stems to 'kormánypár' and
-- so answers neither. That residue is a separate defect and out of scope here.
--
-- THE CHANGE
--
-- A third component on corpus.search_vector:
--
--       to_tsvector('simple', unaccent(...hungarian_lemma...))   unchanged
--    || to_tsvector('corpus.hungarian_surface', t)               unchanged
--    || to_tsvector('corpus.hungarian_lemma', t)                 NEW, accents kept
--
-- It is PURELY ADDITIVE to the lexeme set, and that is the property that makes
-- it safe: every lexeme a query matches today is still present, so an
-- accent-free query cannot change its answer. Section 4 asserts exactly that,
-- by comparing the rebuilt columns against what they held before.
--
-- The new component is not new code. corpus.phrase_match (009) already builds
-- to_tsvector('corpus.hungarian_lemma', ...) for every row it checks; 016 stores
-- what that function was recomputing.
--
-- WHY THIS ONE IS A REWRITE
--
-- Three STORED generated columns reference search_vector. Replacing the function
-- underneath them leaves their stored values stale - the desynchronisation every
-- guard since 010 has been refusing to cause. So the columns and their GIN
-- indexes are dropped and re-added, which recomputes every row.
--
--     corpus.article.search_tsv        + article_search_tsv_idx
--     corpus.content_block.text_tsv    + content_block_text_tsv_idx
--     corpus.article_image.caption_tsv + article_image_caption_tsv_idx (partial)
--
-- At 1,008 articles / 10,589 blocks that is seconds, and that is the argument
-- for doing it NOW: the corpus schema is not in production, the only deployment
-- is cx-pg-eval, and the same change against a loaded 4.2M-article corpus is a
-- planned operation rather than a migration.
--
-- The cost, MEASURED rather than estimated - I guessed "roughly a third" and
-- that was wrong. Most words carry no accent, so their accented lemma is a
-- duplicate of the folded one and adds nothing:
--
--     content_block_text_tsv_idx   8272 kB -> 8320 kB   +0.6%
--     article_search_tsv_idx       3152 kB -> 1912 kB   SMALLER, the rebuild
--                                                       compacted years of bloat
--     database                       39 MB ->   40 MB
--
-- Queries stay index-served: 'kór' is a Bitmap Index Scan returning 11 rows in
-- 0.34 ms.
--
-- 016 changes only what is STORED. Nothing yet asks for an accented lexeme -
-- that is 017.
-- =====================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------
-- 1. Record what the columns hold today
-- ---------------------------------------------------------------------
-- Section 4 requires the rebuilt vectors to be SUPERSETS of these. A superset is
-- the exact claim "additive": no lexeme may be lost, or an accent-free query
-- would start missing documents it finds today.
CREATE TEMP TABLE _tsv_before ON COMMIT DROP AS
SELECT 'article'::text AS src, id, search_tsv AS tsv FROM corpus.article
UNION ALL
SELECT 'block', id, text_tsv FROM corpus.content_block
UNION ALL
SELECT 'image', id, caption_tsv FROM corpus.article_image;

-- ---------------------------------------------------------------------
-- 2. The third component
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION corpus.search_vector(t text)
RETURNS tsvector
LANGUAGE sql
IMMUTABLE PARALLEL SAFE STRICT
AS $fn$
    -- lemma, accent-folded: tolerates inflection AND a foreign keyboard (007)
    SELECT to_tsvector('simple',
               corpus.unaccent_immutable(
                   corpus.text_array_to_string(
                       tsvector_to_array(
                           to_tsvector('corpus.hungarian_lemma', t)))))
    -- surface, accent-folded: tolerates a foreign keyboard, not inflection (007)
        || to_tsvector('corpus.hungarian_surface', t)
    -- lemma, ACCENT PRESERVED: the only component that can tell kór from kor.
    -- Nothing queries it until 017; storing it is what makes 017 query-side.
        || to_tsvector('corpus.hungarian_lemma', t)
$fn$;

COMMENT ON FUNCTION corpus.search_vector(text) IS
    'Three components: the accent-folded lemma and surface halves (007), plus '
    'the accent-PRESERVING lemma (016) that lets an accented query distinguish '
    'kór from kor and Viktória from Viktor. Positions are meaningless across all '
    'of them - see 009 and use corpus.phrase_match for anything positional.';

-- ---------------------------------------------------------------------
-- 3. Rebuild the three stored columns and their indexes
-- ---------------------------------------------------------------------
-- DROP ... ADD rather than a no-op ALTER: a generated column is only recomputed
-- when it is written, so replacing the function alone would leave every stored
-- vector describing the old definition of itself.
ALTER TABLE corpus.article        DROP COLUMN search_tsv;
ALTER TABLE corpus.content_block  DROP COLUMN text_tsv;
ALTER TABLE corpus.article_image  DROP COLUMN caption_tsv;

ALTER TABLE corpus.article ADD COLUMN search_tsv tsvector
    GENERATED ALWAYS AS (
        setweight(corpus.search_vector(coalesce(title, '')), 'A')
     || setweight(corpus.search_vector(coalesce(subtitle, '')), 'B')
     || setweight(corpus.search_vector(coalesce(description, '')), 'B')
     || setweight(corpus.search_vector(corpus.text_array_to_string(authors)), 'C')
     || setweight(corpus.search_vector(corpus.text_array_to_string(tags)), 'C')
    ) STORED;

ALTER TABLE corpus.content_block ADD COLUMN text_tsv tsvector
    GENERATED ALWAYS AS (corpus.search_vector(coalesce(block_text, ''))) STORED;

ALTER TABLE corpus.article_image ADD COLUMN caption_tsv tsvector
    GENERATED ALWAYS AS (
        corpus.search_vector(coalesce(caption, '') || ' ' || coalesce(alt, '')
                             || ' ' || coalesce(credit, ''))
    ) STORED;

CREATE INDEX article_search_tsv_idx        ON corpus.article       USING gin (search_tsv);
CREATE INDEX content_block_text_tsv_idx    ON corpus.content_block USING gin (text_tsv);
CREATE INDEX article_image_caption_tsv_idx ON corpus.article_image USING gin (caption_tsv)
    WHERE caption_tsv <> ''::tsvector;

ANALYZE corpus.article;
ANALYZE corpus.content_block;
ANALYZE corpus.article_image;

-- ---------------------------------------------------------------------
-- 4. Prove it
-- ---------------------------------------------------------------------
DO $verify$
DECLARE
    lost      bigint;
    unchanged bigint;
    bad       text;
BEGIN
    -- 4a. THE central assertion: every rebuilt vector CONTAINS what it held
    -- before. Not equality - it must have grown - but nothing may be lost.
    -- There is no tsvector @> tsvector operator; compare the lexeme SETS.
    SELECT count(*) INTO lost
    FROM _tsv_before b
    JOIN (SELECT 'article'::text AS src, id, search_tsv AS tsv FROM corpus.article
          UNION ALL SELECT 'block', id, text_tsv FROM corpus.content_block
          UNION ALL SELECT 'image', id, caption_tsv FROM corpus.article_image) a
      ON a.src = b.src AND a.id = b.id
    WHERE NOT (coalesce(tsvector_to_array(a.tsv), ARRAY[]::text[])
               @> coalesce(tsvector_to_array(b.tsv), ARRAY[]::text[]));

    IF lost > 0 THEN
        RAISE EXCEPTION
            '% stored vector(s) lost lexemes in the rebuild - an accent-free '
            'query would start missing documents', lost;
    END IF;

    -- 4b. ...and it really did grow, or the migration did nothing.
    SELECT count(*) INTO unchanged
    FROM _tsv_before b
    JOIN (SELECT 'block'::text AS src, id, text_tsv AS tsv FROM corpus.content_block) a
      ON a.src = b.src AND a.id = b.id
    WHERE b.tsv <> ''::tsvector AND strip(a.tsv) = strip(b.tsv);

    IF unchanged > 0 AND (SELECT count(*) FROM corpus.content_block) > 0 THEN
        -- Some blocks legitimately gain nothing (no accented word at all), so
        -- this is only a failure when NOTHING gained anything.
        IF unchanged = (SELECT count(*) FROM corpus.content_block
                         WHERE text_tsv <> ''::tsvector) THEN
            RAISE EXCEPTION 'no vector gained an accented lexeme - 016 is a no-op';
        END IF;
    END IF;

    -- 4c. The accented lexeme is actually there, and it separates the words the
    -- migration exists for.
    IF NOT corpus.search_vector('a kór terjed') @@ 'kór'::tsquery THEN
        RAISE EXCEPTION 'the accent-preserving component is missing';
    END IF;
    IF corpus.search_vector('a kor szelleme') @@ 'kór'::tsquery THEN
        RAISE EXCEPTION 'kor still answers the accented lexeme kór';
    END IF;
    -- The separation lives in the ACCENTED lexeme. 'viktor' is still present in
    -- both, from the folded component, and must be - that is accent-blindness,
    -- and 016 does not touch it. Only an accented query (017) can tell them
    -- apart, and this is the pair that lets it.
    IF NOT corpus.search_vector('Viktória királynő') @@ 'viktór'::tsquery THEN
        RAISE EXCEPTION 'Viktória did not gain its accented lexeme viktór';
    END IF;
    IF corpus.search_vector('Orbán Viktor') @@ 'viktór'::tsquery THEN
        RAISE EXCEPTION 'Viktor wrongly carries Viktória''s accented lexeme';
    END IF;
    IF NOT (corpus.search_vector('Viktória királynő') @@ 'viktor'::tsquery
        AND corpus.search_vector('Orbán Viktor')      @@ 'viktor'::tsquery) THEN
        RAISE EXCEPTION 'the accent-FOLDED lexeme was lost - 016 must be additive';
    END IF;

    -- 4d. Accent-FOLDED matching is untouched: every collision that existed
    -- before still exists for an accent-free query, which is 007's contract.
    IF NOT (corpus.search_vector('a kór terjed') @@ corpus.search_query('kor')
        AND corpus.search_vector('a kör közepe') @@ corpus.search_query('kor')) THEN
        RAISE EXCEPTION 'an accent-free query lost documents it used to match';
    END IF;

    -- 4e. The indexes came back.
    SELECT string_agg(i, ', ') INTO bad FROM unnest(ARRAY[
        'article_search_tsv_idx','content_block_text_tsv_idx',
        'article_image_caption_tsv_idx']) AS i
    WHERE to_regclass('corpus.' || i) IS NULL;

    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'index(es) not recreated: %', bad;
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('016')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
