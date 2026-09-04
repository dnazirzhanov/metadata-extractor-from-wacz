-- =====================================================================
-- 010  Stop an over-stemmed 1-2 character lemma from entering a query
-- =====================================================================
-- THE DEFECT
--
-- 007 made Hungarian search work for the ACCENTED spelling of a word and left
-- the accent-free spelling going through the stemmer, where it over-stems:
--
--     corpus.search_query('Orbán')  ->  'orb' | 'orban'      correct
--     corpus.search_query('Orban')  ->  'or'  | 'orban'      the defect
--
-- With the accent gone, snowball reads the trailing -ban of "Orban" as the
-- inessive case ending and strips it. `or` is not a word, it is a collision
-- magnet: on the 1,008-article evaluation corpus that single lexeme matches
-- 143 articles (14%), so the two spellings of one name returned different
-- corpora - 104 articles accented, 243 unaccented. Accent-insensitivity is the
-- `_ci` this schema promised.
--
-- It is not one name. Of 2,872 distinct accented words in article titles, 693
-- (24.1%) expand to a DIFFERENT query depending on whether the accent is typed.
--
-- THE MEASUREMENT  (scripts/stemming_lab.py, 1,008 articles, 19 queries,
-- scored against a word-initial accent-folded yardstick computed in Python so
-- no configuration can score well by agreeing with itself)
--
--     cand  recall  precision   what it is
--     A      66.8%      90.0%   the retired corpus.hungarian_ci
--     D      58.5%     100.0%   no stemming, prefix query
--     E      73.2%      81.9%   pg_trgm word_similarity 0.6
--     H      88.6%      89.8%   SHIPPED BY 007 - the baseline
--     H1     83.5%      92.8%   drop lemmas shorter than 4
--     H2     78.8%      93.4%   accent-free terms go surface-only
--     H3     78.9%      93.8%   accent-free terms go surface-PREFIX
--     H4     88.6%      92.8%   drop lemmas of 1-2 chars   <-- this migration
--     H5     88.6%      92.8%   H4 restricted to accent-free terms
--
-- H4 loses recall on NO query and gains 3.0 points of mean precision.
--
-- WHY NOT THE OTHER THREE, WHICH LOOKED MORE PRINCIPLED
--
--     H1  took 'Orbánnak' from 99% recall to 1%. 'orb' is THREE characters and
--         is the legitimate lemma of every Orbán form; a threshold of 4 throws
--         it away. The cut has to be at 2.
--     H2  took 'kormanynak' from 100% to 13% and 'Orbant' from 100% to 0%. For
--         an accent-free INFLECTED query the lemma side is load-bearing - it is
--         the only thing that reaches the base form.
--     H3  does not rescue H2 (78.9%). A surface prefix runs the wrong way: the
--         query is the long inflected form and the stored lexeme is the short
--         stem, so 'kormanynak':* reaches nothing.
--
-- H5 scores identically to H4, which means the "only for accent-free terms"
-- restriction never changes an outcome. H4 is the simpler function, so H4 ships.
--
-- WHAT THIS TOUCHES, AND WHAT IT DELIBERATELY DOES NOT
--
-- Only `corpus.search_query`, the QUERY side. `corpus.search_vector` and the
-- three STORED generated columns built from it (article.search_tsv,
-- content_block.text_tsv, article_image.caption_tsv) are NOT touched, so:
--
--     * no generated column is dropped and re-added
--     * no table is rewritten
--     * neither GIN index is rebuilt
--
-- That is the whole reason this shape was chosen. On 1,008 rows a rebuild is
-- seconds; on 4.2M articles with 58M content blocks it is a planned operation.
-- A DO block below REFUSES to run if any generated column has come to depend on
-- search_query, because then this would silently be a data migration.
--
-- THE RESIDUAL RISK, STATED
--
-- Dropping 'or' means a document whose ONLY occurrence is an accent-free
-- "Orbanban" in the source text is no longer reached by the query "Orban". No
-- such loss appeared on any of the 19 queries over 1,008 real articles, but the
-- guard is not free in principle - it trades a rare true positive for a large
-- number of false ones. The surface alternative is ALWAYS kept, so every word
-- remains findable by its exact spelling.
--
-- The guard fires on 114 of 4,400 distinct title words (2.6%), and they are
-- over-stems: simon->si, usa->us, idén->id, kik->ki, írnak->ir, ezeken->ez.
-- =====================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------
-- 1. The two halves of corpus.search_vector, separately addressable
-- ---------------------------------------------------------------------
-- search_query has to tell a lemma lexeme from a surface one in order to guard
-- only the first. Today it cannot: it reads the flattened union out of
-- corpus.search_vector and never learns which half a lexeme came from.
--
-- These two functions are the LEFT and RIGHT operands of corpus.search_vector,
-- copied expression-for-expression. Their union must equal what search_vector
-- produces, and the DO block below proves it rather than asserting it.

CREATE OR REPLACE FUNCTION corpus.lemma_lexemes(t text)
RETURNS text[]
LANGUAGE sql
IMMUTABLE PARALLEL SAFE STRICT
AS $$
    SELECT tsvector_to_array(
        to_tsvector('simple',
            corpus.unaccent_immutable(
                corpus.text_array_to_string(
                    tsvector_to_array(
                        to_tsvector('corpus.hungarian_lemma', t))))))
$$;

COMMENT ON FUNCTION corpus.lemma_lexemes(text) IS
    'The lemma half of corpus.search_vector, as an array. Must be changed '
    'together with corpus.search_vector - 010 checks that they agree.';

CREATE OR REPLACE FUNCTION corpus.surface_lexemes(t text)
RETURNS text[]
LANGUAGE sql
IMMUTABLE PARALLEL SAFE STRICT
AS $$
    SELECT tsvector_to_array(to_tsvector('corpus.hungarian_surface', t))
$$;

COMMENT ON FUNCTION corpus.surface_lexemes(text) IS
    'The surface half of corpus.search_vector, as an array.';

-- The halves must reconstruct the whole, or the query side would be able to ask
-- for a lexeme the document side never stores.
DO $check$
DECLARE
    probe text;
    got   text[];
    want  text[];
BEGIN
    FOREACH probe IN ARRAY ARRAY[
        'Orbán Viktor', 'Orban', 'kormányban', 'Magyarországról',
        'USA', 'elsősorban', 'koronavírus', 'Szijjártó Péter'
    ] LOOP
        SELECT array_agg(DISTINCT a ORDER BY a) INTO got
          FROM unnest(coalesce(corpus.lemma_lexemes(probe),  ARRAY[]::text[])
                   || coalesce(corpus.surface_lexemes(probe), ARRAY[]::text[])) AS a;
        SELECT array_agg(DISTINCT a ORDER BY a) INTO want
          FROM unnest(tsvector_to_array(corpus.search_vector(probe))) AS a;
        IF got IS DISTINCT FROM want THEN
            RAISE EXCEPTION
                'lemma_lexemes || surface_lexemes <> search_vector for %: % vs %',
                probe, got, want;
        END IF;
    END LOOP;
END
$check$;

-- ---------------------------------------------------------------------
-- 2. Refuse to run if this would secretly be a data migration
-- ---------------------------------------------------------------------
DO $guard$
DECLARE n int;
BEGIN
    SELECT count(*) INTO n
    FROM pg_attrdef d
    JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
    JOIN pg_class c     ON c.oid = d.adrelid
    JOIN pg_namespace ns ON ns.oid = c.relnamespace
    WHERE ns.nspname = 'corpus'
      AND a.attgenerated = 's'
      AND pg_get_expr(d.adbin, d.adrelid) LIKE '%search_query%';
    IF n > 0 THEN
        RAISE EXCEPTION
            'corpus.search_query is referenced by % STORED generated column(s); '
            'replacing it would desynchronise them from their own text. '
            'This migration assumes search_query is query-side only.', n;
    END IF;
END
$guard$;

-- ---------------------------------------------------------------------
-- 3. The guard itself
-- ---------------------------------------------------------------------
-- Unchanged from 007 except for the WHERE clause building `alts`.
--
-- `length(l) > 2 OR l = ANY(sur)` - the second half is not decoration. A short
-- word that really is short keeps its lemma: 'ki' lemmatises to 'ki', which is
-- also its surface form, so it survives. Only a lemma the stemmer INVENTED,
-- one that no spelling of the term produces on the surface, is dropped.
CREATE OR REPLACE FUNCTION corpus.search_query(q text)
RETURNS tsquery
LANGUAGE plpgsql
IMMUTABLE PARALLEL SAFE STRICT
AS $$
DECLARE
    term  text;
    lem   text[];
    sur   text[];
    alts  text[];
    parts text[] := ARRAY[]::text[];
BEGIN
    FOREACH term IN ARRAY regexp_split_to_array(trim(q), '[\s]+') LOOP
        CONTINUE WHEN term = '';

        lem := coalesce(corpus.lemma_lexemes(term),   ARRAY[]::text[]);
        sur := coalesce(corpus.surface_lexemes(term), ARRAY[]::text[]);

        SELECT array_agg(DISTINCT a ORDER BY a) INTO alts
        FROM unnest(
                 ARRAY(SELECT l FROM unnest(lem) AS l
                        WHERE length(l) > 2 OR l = ANY(sur))
              || sur
             ) AS a;

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
    'across terms, DROPPING a lemma of 1-2 characters that no spelling of the '
    'term produces on the surface - snowball reads the accent-folded -ban of '
    '"Orban" as the inessive case and leaves "or". Query side only: '
    'corpus.search_vector and the STORED columns built from it are untouched.';

-- ---------------------------------------------------------------------
-- 4. Prove the fix, and prove it changed nothing else
-- ---------------------------------------------------------------------
DO $verify$
DECLARE
    accented   text := corpus.search_query('Orbán')::text;
    unaccented text := corpus.search_query('Orban')::text;
BEGIN
    IF position('''or''' in unaccented) > 0 THEN
        RAISE EXCEPTION 'the over-stemmed lexeme ''or'' is still in %', unaccented;
    END IF;
    IF accented <> '''orb'' | ''orban''' THEN
        RAISE EXCEPTION 'the accented spelling changed, it must not: %', accented;
    END IF;
    -- THE INVARIANT: the surface alternative is never dropped, so every word
    -- stays findable by its exact spelling however badly the stemmer mangled
    -- it. Checked as a property rather than on one hand-picked word - the
    -- obvious candidate, 'ki', turned out to be a Hungarian STOPWORD whose
    -- expansion is legitimately empty both before and after.
    DECLARE
        probe   text;
        missing text;
    BEGIN
        FOREACH probe IN ARRAY ARRAY[
            'Orban', 'Orbán', 'USA', 'Simon', 'idén', 'kormányban',
            'Magyarországról', 'koronavírus', 'elsősorban'
        ] LOOP
            SELECT s INTO missing
            FROM unnest(corpus.surface_lexemes(probe)) AS s
            WHERE position('''' || s || '''' in corpus.search_query(probe)::text) = 0
            LIMIT 1;
            IF missing IS NOT NULL THEN
                RAISE EXCEPTION
                    'surface lexeme % was dropped from the query for %: %',
                    missing, probe, corpus.search_query(probe)::text;
            END IF;
        END LOOP;
    END;

    -- A stopword must still expand to nothing rather than error.
    IF corpus.search_query('ki')::text <> '' THEN
        RAISE EXCEPTION 'expected the stopword ''ki'' to expand to nothing, got %',
            corpus.search_query('ki')::text;
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('010')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
