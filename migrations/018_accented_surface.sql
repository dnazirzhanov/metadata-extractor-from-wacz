-- =====================================================================
-- 018  The accented branch gets its own surface form
-- =====================================================================
-- THE DEFECT
--
-- The Hungarian snowball stemmer reads a final -t as the accusative ending and
-- lengthens the vowel, so "párt" (a political party) stems to 'pár' - which is
-- also the lemma of "pár" (a couple). 010's length guard does not fire: 'pár'
-- is three characters.
--
-- Worse, the stemmer SPLITS THE PARADIGM. The nominative over-stems and its own
-- inflections do not:
--
--     párt     ->  'pár'      nominative, over-stemmed
--     pártja   ->  'párt'     inflected, correct
--     pártok   ->  'párt'     inflected, correct
--
-- Before 017 an accented query took the accent-folded path, which has TWO
-- alternatives - `'par' | 'part'`, the folded lemma and the surface - and
-- between them they covered both halves of the split. 017 gave the accented
-- branch only ONE alternative, the accented lemma, and so broke it:
--
--     párt finds pártok            false     <- recall lost by 017
--     párt finds pár (a couple)    true      <- the original complaint
--     part finds pártok            true      <- the folded path still works
--
-- 131 articles answered "párt" where 176 contain a part-family word. The
-- 193 -> 131 drop reported with 017 as "riverbank separated" was partly this
-- regression, not all precision.
--
-- THE FIX
--
-- Give the accented branch a second alternative of its own: the term's accented,
-- UNSTEMMED form, ORed with the accented lemma.
--
--     ( accented lemma lexemes  ANDed )      'pár'
--   | ( accented raw lexemes    ANDed )      'párt'
--
-- That is exactly the shape corpus.term_query's accent-free branch has always
-- had (lemma | surface). The accented branch simply never got its surface.
--
-- WHY THE RAW FORM IS THE RIGHT SECOND ALTERNATIVE
--
-- Not an arbitrary widening. When the stemmer over-strips a BASE form, the base
-- form itself is what that word's own inflections stem to - pártok and pártja
-- both stem to 'párt', the nominative. So adding the raw term recovers precisely
-- the half of the paradigm the over-stem cut off, and nothing else.
--
-- to_tsvector('simple', t) is the right source: simple neither stems nor
-- unaccents, so it preserves the term as typed.
--
-- IT IS A NO-OP FOR MOST TERMS, which is the containment argument:
--
--     term           accented lemma   accented raw    changes?
--     kór, ügy       'kór', 'ügy'     same            no
--     háború         'háború'         same            no
--     kormány        'kormány'        same            no
--     Magyarország   'magyarország'   same            no
--     Orbán          'orb'            'orbán'         adds an alternative no
--                                                     document carries: harmless
--     párt           'pár'            'párt'          YES - this is the fix
--
-- MEASURED: párt 131 -> 159 articles, and "a Duna partján" still does NOT
-- answer "párt" - 017's accent separation is preserved, which is the thing most
-- at risk from widening this branch.
--
-- WHAT THIS DOES NOT FIX
--
-- "párt" still matches "pár". That is not a pipeline defect: párt genuinely IS
-- the accusative of pár, and no rule resolves that without a dictionary.
-- "kormánypárt" stems to 'kormánypár' and answers neither - that is the closed
-- compound gap, and 'párt' is below 015's seven-character prefix threshold.
--
-- Query side only. No table rewritten, no GIN index rebuilt.
-- =====================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

DO $guard$
DECLARE
    dependent text;
BEGIN
    SELECT string_agg(format('%I.%I.%I', n.nspname, c.relname, a.attname), ', ')
      INTO dependent
      FROM pg_attrdef      d
      JOIN pg_attribute    a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
      JOIN pg_class        c ON c.oid = d.adrelid
      JOIN pg_namespace    n ON n.oid = c.relnamespace
     WHERE a.attgenerated = 's'
       AND pg_get_expr(d.adbin, d.adrelid) ILIKE '%accented_query%';

    IF dependent IS NOT NULL THEN
        RAISE EXCEPTION
            'accented_query is referenced by STORED generated column(s): %.', dependent;
    END IF;
END
$guard$;

-- ---------------------------------------------------------------------
-- 1. Record what every branch answers today
-- ---------------------------------------------------------------------
-- Accent-FREE terms must not move at all - they never reach accented_query.
-- Accented terms whose lemma already equals their raw form must not move either.
CREATE TEMP TABLE _q_before ON COMMIT DROP AS
SELECT t AS term, corpus.search_query(t)::text AS expected
FROM unnest(ARRAY['kor','part','Orban','kormany','magyarorszag','Viktor','ki',
                  'kór','kör','ügy','háború','kormány','Magyarország',
                  'koronavírus','migrációs']) AS t;

-- ---------------------------------------------------------------------
-- 2. The term as typed: accented, unstemmed
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION corpus.accented_raw_lexemes(t text)
RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $fn$
    -- 'simple' neither stems nor unaccents, so this is the term as the user
    -- typed it, tokenised and lowercased and nothing else.
    SELECT tsvector_to_array(to_tsvector('simple', t))
$fn$;

COMMENT ON FUNCTION corpus.accented_raw_lexemes(text) IS
    'The term as typed - accented, unstemmed. When the stemmer over-strips a '
    'base form (párt -> pár) this is what that word''s own inflections stem to, '
    'so it is the alternative that recovers the split paradigm (018).';

-- ---------------------------------------------------------------------
-- 3. accented_query gains the second alternative
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION corpus.accented_query(t text)
RETURNS tsquery
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT
AS $fn$
DECLARE
    sur   text[];
    sides text[] := ARRAY[]::text[];
    side  text;
    lx    text[];
BEGIN
    sur := coalesce(corpus.surface_lexemes(t), ARRAY[]::text[]);

    -- Two alternatives, same shape as the accent-free branch of term_query:
    -- the stemmed accented lemma, and the term exactly as typed.
    FOREACH side IN ARRAY ARRAY['lemma', 'raw'] LOOP
        lx := CASE side
                WHEN 'lemma' THEN coalesce(corpus.accented_lexemes(t),     ARRAY[]::text[])
                ELSE              coalesce(corpus.accented_raw_lexemes(t), ARRAY[]::text[])
              END;

        -- 010's guard: an invented 1-2 character lemma is a collision magnet
        -- whether or not it kept its accent.
        lx := ARRAY(SELECT DISTINCT l FROM unnest(lx) AS l
                     WHERE length(l) > 2 OR l = ANY(sur)
                     ORDER BY l);

        CONTINUE WHEN array_length(lx, 1) IS NULL;

        -- 015's compound prefix, for the same reason it applies elsewhere.
        sides := sides || ('(' || array_to_string(
            ARRAY(SELECT quote_literal(x)
                         || CASE WHEN length(x) >= corpus.prefix_min_length()
                                 THEN ':*' ELSE '' END
                    FROM unnest(lx) AS x),
            ' & ') || ')');
    END LOOP;

    IF array_length(sides, 1) IS NULL THEN
        RETURN ''::tsquery;
    END IF;

    -- De-duplicate: for most terms the two sides are identical, and emitting
    -- `X | X` would be noise in every EXPLAIN and every debugging session.
    RETURN (array_to_string(ARRAY(SELECT DISTINCT s FROM unnest(sides) AS s), ' | '))::tsquery;
END
$fn$;

COMMENT ON FUNCTION corpus.accented_query(text) IS
    'One term as an ACCENT-SENSITIVE tsquery: the accented lemma OR the term as '
    'typed, each with the 010 guard and the 015 prefix. The second alternative '
    'recovers paradigms the stemmer splits - párt stems to pár but pártok stems '
    'to párt (018). Used by corpus.term_query for an accented term, and by '
    'scripts/search.py for the accent ranking bonus.';

-- ---------------------------------------------------------------------
-- 4. Prove it
-- ---------------------------------------------------------------------
DO $verify$
DECLARE
    bad text;
BEGIN
    -- 4a. Nothing moves except the terms whose paradigm was split.
    SELECT string_agg(format('%L: was %s, now %s',
                             term, expected, corpus.search_query(term)::text), E'\n  ')
      INTO bad
      FROM _q_before
     WHERE corpus.search_query(term)::text IS DISTINCT FROM expected;

    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'a term that should not have moved did:%s  %s', E'\n', bad;
    END IF;

    -- 4b. THE REGRESSION 017 INTRODUCED, closed.
    IF NOT corpus.search_vector('a pártok megegyeztek') @@ corpus.search_query('párt') THEN
        RAISE EXCEPTION 'párt still does not find pártok';
    END IF;
    IF NOT corpus.search_vector('a pártja elnöke') @@ corpus.search_query('párt') THEN
        RAISE EXCEPTION 'párt still does not find pártja';
    END IF;
    IF NOT corpus.search_vector('az új párt indul') @@ corpus.search_query('párt') THEN
        RAISE EXCEPTION 'párt stopped finding its own nominative';
    END IF;

    -- 4c. ...without giving back 017. This is the assertion most at risk from
    -- widening the accented branch, so it is stated for both directions.
    IF corpus.search_vector('a Duna partján horgászik') @@ corpus.search_query('párt') THEN
        RAISE EXCEPTION 'párt matches the riverbank again - 017 was undone';
    END IF;
    IF NOT corpus.search_vector('a Duna partján horgászik') @@ corpus.search_query('part') THEN
        RAISE EXCEPTION 'the accent-free part stopped finding the riverbank';
    END IF;
    IF corpus.search_vector('a kor szelleme') @@ corpus.search_query('kór') THEN
        RAISE EXCEPTION 'kór matches kor again - 017 was undone';
    END IF;

    -- 4d. Accent-blindness, still intact.
    IF NOT corpus.search_vector('a kormány döntött') @@ corpus.search_query('kormany') THEN
        RAISE EXCEPTION 'a foreign keyboard stopped finding accented text';
    END IF;

    -- 4e. The de-duplication really fires, or every accented query doubles.
    IF corpus.search_query('kór')::text <> '''kór''' THEN
        RAISE EXCEPTION 'expected a single alternative for kór, got %',
            corpus.search_query('kór')::text;
    END IF;
    IF corpus.search_query('párt')::text NOT LIKE '%|%' THEN
        RAISE EXCEPTION 'expected two alternatives for párt, got %',
            corpus.search_query('párt')::text;
    END IF;

    -- 4f. The known residue, pinned so a later change to it is a decision.
    -- párt IS the accusative of pár; no rule separates them without a
    -- dictionary, and this migration does not pretend to.
    IF NOT corpus.search_vector('egy pár cipő') @@ corpus.search_query('párt') THEN
        RAISE EXCEPTION 'the pár residue changed - if deliberate, update 018';
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('018')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
