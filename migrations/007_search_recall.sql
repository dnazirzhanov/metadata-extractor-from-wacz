-- =====================================================================
-- 007  Rebuild the search vectors so Hungarian morphology survives them
-- =====================================================================
-- THE DEFECT
--
-- `corpus.hungarian_ci` maps `word` to `unaccent, hungarian_stem` - unaccent
-- runs FIRST, so the snowball stemmer is handed text that is no longer
-- Hungarian, and it strips whatever the accent-free spelling makes look like a
-- suffix. Measured lexemes:
--
--     Orbán            -> 'or'                 (-ban read as the inessive case)
--     orra ("nose")    -> 'or'                 ... so the two collide
--     Orbánnak         -> 'orban'              ... and match neither
--     Magyarország     -> 'magyarorszag'
--     Magyarországról  -> 'magyarorszagrol'    ... the pair does not unify
--
-- The failure is not uniform, which is the worst property a search system can
-- have: `kormány`/`kormányban` unify correctly while `Orbán`/`Orbánnak` do not,
-- so the behaviour cannot be predicted from the query.
--
-- THE MEASUREMENT  (scripts/stemming_lab.py, 36 dev articles, 19 probe queries,
-- scored against an accent-folded substring yardstick computed in Python so no
-- configuration can score well by agreeing with itself)
--
--     candidate                                    recall   precision   index
--     A  unaccent -> snowball          (current)    70.2%       93.4%   288 kB
--     B  snowball only, accents kept                71.2%       97.5%   296 kB
--     C  hunspell hu_HU -> snowball                 74.0%       94.3%   304 kB
--     D  no stemming, unaccent + prefix query       62.4%      100.0%   392 kB
--     E  pg_trgm word_similarity 0.6                76.7%       84.3%        -
--     F  hunspell lemmas, then unaccent             89.0%       88.4%   296 kB
--     G  F + surface vector, per-term alternation   94.3%       91.0%   552 kB
--     H  G with snowball instead of hunspell        93.1%       95.2%   560 kB
--
-- H is what this migration implements. It beats G on precision, gives up 1.2
-- points of recall to it, and - decisively - needs nothing installed on the
-- server. C, F and G all require hu_HU hunspell dictionaries in
-- $SHAREDIR/tsearch_data inside the db container on milab2, which would have to
-- survive every container rebuild.
--
-- B was rejected despite its precision: it drops accent-insensitivity, which is
-- the `_ci` in the name of the configuration it would replace. Under B,
-- `magyarorszag` returns 0 of 21 articles and `orban` 0 of 3.
--
-- THE MECHANISM
--
-- Two lexemes are indexed for every word, in one vector:
--
--     LEMMA    stem the ACCENTED text (so the stemmer sees Hungarian), then
--              fold accents off the LEMMA. kormányban -> kormány -> kormany
--     SURFACE  fold accents off the word itself, no stemming at all.
--              kormányban -> kormanyban
--
-- A query is expanded the same way and alternated per term:
--
--     kormányban  ->  ('kormany' | 'kormanyban')
--
-- The lemma side carries inflection, the surface side carries accent-free
-- spelling, and neither can be broken by the other's failure mode. That is what
-- the current configuration cannot do: it has one lexeme per word and has to
-- serve both jobs with it.
--
-- COST: two lexemes per word roughly doubles both GIN indexes. On the dev
-- corpus 288 kB -> 560 kB. Rebuilding the generated columns rewrites every row
-- of corpus.article and corpus.content_block; on the production corpus that is
-- ~58M blocks and ~11.7 GB of text, so this is a maintenance-window migration,
-- not an online one.
-- =====================================================================

BEGIN;

SET lock_timeout = '5s';

-- ---------------------------------------------------------------------
-- The lemma side: snowball, fed text that still has its accents
-- ---------------------------------------------------------------------
-- A plain copy of pg_catalog.hungarian. The whole point is that NOTHING is
-- placed in front of hungarian_stem - that is the defect being fixed.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_ts_config c
                   JOIN pg_namespace n ON n.oid = c.cfgnamespace
                   WHERE c.cfgname = 'hungarian_lemma' AND n.nspname = 'corpus') THEN
        EXECUTE 'CREATE TEXT SEARCH CONFIGURATION corpus.hungarian_lemma
                 ( COPY = pg_catalog.hungarian )';
    END IF;
END
$$;

-- ---------------------------------------------------------------------
-- The surface side: accent-folded whole words, no stemming
-- ---------------------------------------------------------------------
-- STOPWORDS is not decoration. Without it the surface side indexes every `a`,
-- `az` and `és` in the corpus. The 36-article dev database cannot show the
-- difference (3 distinct lexemes, no measurable index change) because stopword
-- posting lists only become expensive at corpus scale - which is exactly why it
-- is set here rather than after someone measures it on 58M blocks.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_ts_dict d
                   JOIN pg_namespace n ON n.oid = d.dictnamespace
                   WHERE d.dictname = 'hungarian_surface_dict' AND n.nspname = 'corpus') THEN
        EXECUTE 'CREATE TEXT SEARCH DICTIONARY corpus.hungarian_surface_dict
                 ( TEMPLATE = pg_catalog.simple, STOPWORDS = hungarian )';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_ts_config c
                   JOIN pg_namespace n ON n.oid = c.cfgnamespace
                   WHERE c.cfgname = 'hungarian_surface' AND n.nspname = 'corpus') THEN
        EXECUTE 'CREATE TEXT SEARCH CONFIGURATION corpus.hungarian_surface
                 ( COPY = pg_catalog.simple )';
        -- EVERY word-shaped token type, not just the accented ones. The
        -- configuration this replaces alters only `word`, `hword` and
        -- `hword_part`, which is why an ASCII-only token in it is never
        -- unaccented and never stopword-filtered. Here that asymmetry would be
        -- a real defect: the surface side is what an accent-free query hits.
        EXECUTE 'ALTER TEXT SEARCH CONFIGURATION corpus.hungarian_surface
                 ALTER MAPPING FOR
                     asciiword, word, asciihword, hword, hword_asciipart,
                     hword_part, numword, hword_numpart, numhword
                 WITH unaccent, corpus.hungarian_surface_dict';
    END IF;
END
$$;

-- ---------------------------------------------------------------------
-- IMMUTABLE building blocks
-- ---------------------------------------------------------------------
-- The one-argument unaccent(text) is STABLE: it resolves the dictionary through
-- the search path at call time. The two-argument form names the dictionary and
-- is IMMUTABLE, which is what a STORED generated column requires. Same class of
-- problem as corpus.text_array_to_string in 001, same class of fix.
CREATE OR REPLACE FUNCTION corpus.unaccent_immutable(t text)
RETURNS text
LANGUAGE sql
IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT unaccent('unaccent', t) $$;

COMMENT ON FUNCTION corpus.unaccent_immutable(text) IS
    'IMMUTABLE unaccent for use in STORED generated columns. The one-argument '
    'unaccent() is only STABLE.';

-- The vector every search column is built from.
--
-- Read it inside out: stem with accents intact, flatten the resulting lexemes
-- back to text, fold the accents off THOSE, and union with the accent-folded
-- surface words. `to_tsvector(regconfig, text)` and `tsvector_to_array` are
-- IMMUTABLE; `array_to_string` is not, hence corpus.text_array_to_string.
CREATE OR REPLACE FUNCTION corpus.search_vector(t text)
RETURNS tsvector
LANGUAGE sql
IMMUTABLE PARALLEL SAFE STRICT
AS $$
    SELECT to_tsvector('simple',
               corpus.unaccent_immutable(
                   corpus.text_array_to_string(
                       tsvector_to_array(
                           to_tsvector('corpus.hungarian_lemma', t)))))
        || to_tsvector('corpus.hungarian_surface', t)
$$;

COMMENT ON FUNCTION corpus.search_vector(text) IS
    'Unaccented lemma lexemes unioned with unaccented surface lexemes. '
    'Referenced BY NAME from the STORED generated columns on corpus.article '
    'and corpus.content_block - see the warning at the end of this migration.';

-- The query side of the same transformation.
--
-- Per term: (lemma | surface). AND across terms. A term that yields no lexeme
-- at all - punctuation, a bare stopword - is dropped rather than allowed to
-- make the whole query unsatisfiable.
CREATE OR REPLACE FUNCTION corpus.search_query(q text)
RETURNS tsquery
LANGUAGE plpgsql
IMMUTABLE PARALLEL SAFE STRICT
AS $$
DECLARE
    term  text;
    alts  text[];
    parts text[] := ARRAY[]::text[];
BEGIN
    FOREACH term IN ARRAY regexp_split_to_array(trim(q), '[\s]+') LOOP
        CONTINUE WHEN term = '';

        SELECT array_agg(DISTINCT lexeme ORDER BY lexeme) INTO alts
        FROM (
            SELECT unnest(tsvector_to_array(corpus.search_vector(term))) AS lexeme
        ) s;

        CONTINUE WHEN alts IS NULL OR array_length(alts, 1) IS NULL;

        parts := parts || ('(' || array_to_string(
                     ARRAY(SELECT quote_literal(a) FROM unnest(alts) AS a), ' | ')
                 || ')');
    END LOOP;

    IF array_length(parts, 1) IS NULL THEN
        RETURN ''::tsquery;
    END IF;
    RETURN to_tsquery('simple', array_to_string(parts, ' & '));
END
$$;

COMMENT ON FUNCTION corpus.search_query(text) IS
    'Expands a user query into (lemma | surface) alternatives per term, ANDed '
    'across terms. The query-side counterpart of corpus.search_vector; the two '
    'must always be changed together.';

-- ---------------------------------------------------------------------
-- Rebuild the two generated columns
-- ---------------------------------------------------------------------
-- A STORED generated column is not recomputed when the expression it was
-- defined with changes, so the columns must actually be dropped and re-added.
-- Dropping a column drops its indexes with it; both are recreated below.
--
-- Guarded on the generation expression rather than on the ledger so that
-- running this file twice by hand is still a no-op, per migrations/README.md.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_attrdef ad
        JOIN pg_attribute a ON a.attrelid = ad.adrelid AND a.attnum = ad.adnum
        WHERE ad.adrelid = 'corpus.article'::regclass
          AND a.attname = 'search_tsv'
          AND pg_get_expr(ad.adbin, ad.adrelid) LIKE '%search_vector%')
    THEN
        ALTER TABLE corpus.article DROP COLUMN IF EXISTS search_tsv;
        ALTER TABLE corpus.article ADD COLUMN search_tsv tsvector
            GENERATED ALWAYS AS (
                   setweight(corpus.search_vector(coalesce(title, '')), 'A')
                || setweight(corpus.search_vector(coalesce(subtitle, '')), 'B')
                || setweight(corpus.search_vector(coalesce(description, '')), 'B')
                || setweight(corpus.search_vector(
                       corpus.text_array_to_string(authors)), 'C')
                || setweight(corpus.search_vector(
                       corpus.text_array_to_string(tags)), 'C')
            ) STORED;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_attrdef ad
        JOIN pg_attribute a ON a.attrelid = ad.adrelid AND a.attnum = ad.adnum
        WHERE ad.adrelid = 'corpus.content_block'::regclass
          AND a.attname = 'text_tsv'
          AND pg_get_expr(ad.adbin, ad.adrelid) LIKE '%search_vector%')
    THEN
        ALTER TABLE corpus.content_block DROP COLUMN IF EXISTS text_tsv;
        ALTER TABLE corpus.content_block ADD COLUMN text_tsv tsvector
            GENERATED ALWAYS AS (
                corpus.search_vector(coalesce(block_text, ''))
            ) STORED;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS article_search_tsv_idx
    ON corpus.article USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS content_block_text_tsv_idx
    ON corpus.content_block USING gin (text_tsv);

-- ---------------------------------------------------------------------
-- The warning from 001, restated for what now carries it
-- ---------------------------------------------------------------------
-- The hazard has moved but not gone. `corpus.search_vector` is referenced BY
-- NAME from both generated columns, and CREATE OR REPLACE FUNCTION does not
-- recompute them - so replacing that function after data exists silently
-- desynchronises every vector from its own text, exactly as altering
-- `corpus.hungarian_ci` used to. The same is true of the two configurations it
-- reads. Changing any of the three means: drop the generated columns, change
-- the thing, re-add the columns, rebuild the GIN indexes. It is a migration,
-- not a tweak.
--
-- corpus.hungarian_ci is deliberately LEFT IN PLACE. Nothing references it
-- after this migration, but ts_headline() calls in application code may still
-- name it, and dropping a text search configuration out from under a query is
-- a worse failure than an unused object. Drop it once nothing names it.
COMMENT ON SCHEMA corpus IS
    'Causalia extracted article corpus. Search vectors are built by '
    'corpus.search_vector() and queried through corpus.search_query() - both '
    'are referenced by stored generated columns, so changing either requires '
    'rebuilding them (see migrations/007).';

INSERT INTO corpus.schema_migrations (version) VALUES ('007')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
