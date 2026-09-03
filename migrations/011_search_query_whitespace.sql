-- =====================================================================
-- 011  Split query terms on the whitespace the rest of the system uses
-- =====================================================================
-- THE DEFECT
--
-- corpus.search_query splits a user query with
--
--     regexp_split_to_array(trim(q), '[\s]+')
--
-- and Postgres's \s is ASCII-only. It does not cover U+00A0 NO-BREAK SPACE,
-- U+0085 NEXT LINE, U+FEFF, U+202F NARROW NO-BREAK SPACE, or U+001C-U+001F.
-- A query separated by any of those is not split at all: both words become ONE
-- term, and corpus.search_query alternates the lexemes of a single term with
-- `|` rather than ANDing across terms. So the query silently changes meaning
-- from AND to OR.
--
-- Measured on the 1,008-article evaluation corpus:
--
--     'Orban Viktor'            -> 'orban' & 'viktor'   ->  65 articles
--     'Orban' U+00A0 'Viktor'   -> 'orban' | 'viktor'   ->  76 articles
--     'Orban' U+0085 'Viktor'   -> 'orban' | 'viktor'   ->  76 articles
--
-- This is reachable by ordinary use. U+00A0 is what a browser copy-paste
-- produces, what &nbsp; decodes to, and what many CMSs insert between a name
-- and a title - so a journalist pasting a name out of a web page gets the OR.
-- The damage grows as the terms get rarer: an AND of two selective words
-- becomes the union of two broad result sets.
--
-- WHY THIS PROJECT SHOULD HAVE CAUGHT IT
--
-- It already solved this once. src/causalia_extractor/normalize.py defines
-- WHITESPACE_CODEPOINTS and says why, in its own words:
--
--     spelled out explicitly rather than using `\s` because Python's `\s` and
--     JavaScript's `\s` do not agree [...] U+0085 is not hypothetical here -
--     it occurs in real article titles in this corpus and has already broken a
--     JSON Lines reader once.
--
-- Every character offset this system stores is an offset into a string
-- collapsed with that class. corpus.search_query was written later and used the
-- narrow one, so the query side and the text side disagreed about what a word
-- boundary is.
--
-- THE FIX
--
-- corpus.collapse_whitespace mirrors normalize.collapse() in SQL: translate
-- every character of the class to a space, collapse runs, trim. search_query
-- then splits on a single space. The codepoint list below is GENERATED from
-- normalize.WHITESPACE_CODEPOINTS - the same 30 codepoints, in the same order -
-- and the two must be changed together.
--
--   U+0009, U+000A, U+000B, U+000C, U+000D, U+001C, U+001D, U+001E,
--   U+001F, U+0020, U+0085, U+00A0, U+1680, U+2000, U+2001, U+2002,
--   U+2003, U+2004, U+2005, U+2006, U+2007, U+2008, U+2009, U+200A,
--   U+2028, U+2029, U+202F, U+205F, U+3000, U+FEFF
--
-- Query side only. corpus.search_vector and the three STORED generated columns
-- built from it are untouched: no column rebuilt, no GIN reindex. Same shape as
-- 010, for the same reason - this has to be applicable to 4.8M archives.
-- =====================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------
-- 1. The character class, as data
-- ---------------------------------------------------------------------
-- Codepoints rather than literal characters, exactly as normalize.py argues:
-- "an invisible character in source is a bug waiting to happen, and this list
-- is the contract with the frontend."
CREATE OR REPLACE FUNCTION corpus.whitespace_chars()
RETURNS text
LANGUAGE sql
IMMUTABLE PARALLEL SAFE
AS $fn$
    SELECT string_agg(chr(c), '' ORDER BY c) FROM unnest(ARRAY[
        9, 10, 11, 12, 13, 28, 29, 30, 31, 32,
        133, 160, 5760, 8192, 8193, 8194, 8195, 8196, 8197, 8198,
        8199, 8200, 8201, 8202, 8232, 8233, 8239, 8287, 12288, 65279
    ]) AS c
$fn$;

COMMENT ON FUNCTION corpus.whitespace_chars() IS
    'The union of Python and JavaScript whitespace, mirroring '
    'normalize.WHITESPACE_CODEPOINTS. Changing one without the other makes the '
    'query side and the stored-offset side disagree about word boundaries.';

CREATE OR REPLACE FUNCTION corpus.collapse_whitespace(t text)
RETURNS text
LANGUAGE sql
IMMUTABLE PARALLEL SAFE STRICT
AS $fn$
    SELECT btrim(
               regexp_replace(
                   translate(t, corpus.whitespace_chars(),
                                repeat(' ', length(corpus.whitespace_chars()))),
                   ' +', ' ', 'g'),
               ' ')
$fn$;

COMMENT ON FUNCTION corpus.collapse_whitespace(text) IS
    'The SQL equivalent of normalize.collapse(): every character of '
    'corpus.whitespace_chars() becomes a space, runs collapse, ends trimmed.';

-- ---------------------------------------------------------------------
-- 2. Split on it
-- ---------------------------------------------------------------------
-- Identical to 010 except for the split expression.
CREATE OR REPLACE FUNCTION corpus.search_query(q text)
RETURNS tsquery
LANGUAGE plpgsql
IMMUTABLE PARALLEL SAFE STRICT
AS $fn$
DECLARE
    term  text;
    lem   text[];
    sur   text[];
    alts  text[];
    parts text[] := ARRAY[]::text[];
BEGIN
    FOREACH term IN ARRAY
        string_to_array(corpus.collapse_whitespace(q), ' ')
    LOOP
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
$fn$;

COMMENT ON FUNCTION corpus.search_query(text) IS
    'Expands a user query into (lemma | surface) alternatives per term, ANDed '
    'across terms, dropping a lemma of 1-2 characters that no spelling of the '
    'term produces on the surface (010). Terms are split on '
    'corpus.whitespace_chars(), not on Postgres backslash-s, so a NO-BREAK '
    'SPACE does not silently turn the AND into an OR (011).';

-- ---------------------------------------------------------------------
-- 3. Prove it
-- ---------------------------------------------------------------------
DO $verify$
DECLARE
    baseline tsquery := corpus.search_query('Orban Viktor');
    cp       int;
    got      tsquery;
    bad      text;
BEGIN
    -- Every character of the class must separate two words exactly as a plain
    -- space does. Checked over the WHOLE class, not the two that were reported.
    FOREACH cp IN ARRAY ARRAY(
        SELECT ascii(c)
        FROM regexp_split_to_table(corpus.whitespace_chars(), '') AS c
        WHERE c <> ''
    ) LOOP
        got := corpus.search_query('Orban' || chr(cp) || 'Viktor');
        IF got IS DISTINCT FROM baseline THEN
            RAISE EXCEPTION 'U+% does not separate terms: got %, expected %',
                upper(to_hex(cp)), got::text, baseline::text;
        END IF;
    END LOOP;

    IF baseline::text NOT LIKE '%&%' THEN
        RAISE EXCEPTION 'the baseline is not an AND at all: %', baseline::text;
    END IF;

    -- 010 must survive: the over-stemmed lemma stays out.
    bad := quote_literal('or');
    IF position(bad in corpus.search_query('Orban')::text) > 0 THEN
        RAISE EXCEPTION 'the 010 guard regressed: %',
            corpus.search_query('Orban')::text;
    END IF;

    -- Leading, trailing and repeated separators must not produce empty terms.
    IF corpus.search_query('  Orban ' || chr(160) || chr(9) || ' Viktor  ')
       IS DISTINCT FROM baseline THEN
        RAISE EXCEPTION 'mixed whitespace runs are not collapsed';
    END IF;

    -- A stopword must still expand to nothing rather than error.
    IF corpus.search_query('ki')::text <> '' THEN
        RAISE EXCEPTION 'expected the stopword to expand to nothing, got %',
            corpus.search_query('ki')::text;
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('011')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
