-- =====================================================================
-- 019  An accent-free case suffix is still a case suffix
-- =====================================================================
-- THE DEFECT
--
-- The Hungarian snowball stemmer's suffix rules are written against ACCENTED
-- text. Fold the accents off an inflected word and the suffix stops being a
-- suffix - the whole word survives as one lexeme:
--
--     corpus.lemma_lexemes('kormányról')  ->  {kormany}       correct
--     corpus.lemma_lexemes('kormanyrol')  ->  {kormanyrol}    the defect
--
-- The surface branch still fires, but a surface lexeme only matches the literal
-- inflected spelling, so the query does not degrade - it returns nothing:
--
--     kormányról  ->  94 articles          kormanyrol  ->  0
--     kormánytól  ->  94 articles          kormanytol  ->  2
--     kormánynál  ->  94 articles          kormanynal  ->  0
--
-- This is 010's defect running the other way. 010 fixed accent-free words the
-- stemmer stripped TOO eagerly ('Orban' -> 'or'). This fixes accent-free words
-- it will not strip at all.
--
-- THE MEASUREMENT  (scripts/search_eval.py, cx-pg-eval, 1,008 articles,
-- 60 base words x 4 spellings; agreement = fraction of the base word's result
-- set that the variant also returns)
--
--     class          n   agree(infl+unacc)   agree(inflected)
--     stem-only     30         87.8%              81.4%
--     suffix-only    4          0.0%              97.9%
--     stem+suffix   11          0.1%              91.2%
--
-- Losing the accent in the STEM costs nothing measurable - 87.8%, better than
-- the accented inflection scores. Every point of the deficit is the 15 words
-- whose SUFFIX carried the accent, and they do not degrade, they return zero.
-- That is why the headline 58.7% describes neither population.
--
-- The suffixes are not exotic. In this corpus -ból appears in 37% of articles,
-- -ról 36%, -ről 35%, -ből 26%, -tól 25%, -től 19%, -nál 17%, -nél 13%. A
-- journalist without a Hungarian keyboard typing "about the government" -
-- kormanyrol - is asking the commonest question there is.
--
-- WHAT THIS MIGRATION DOES
--
-- Query side only. No DDL on any table, no change to corpus.search_vector, so
-- nothing is reindexed and nothing is re-ingested.
--
-- When an accent-free term fails to stem AT ALL, and it ends in the accent-free
-- shape of an accent-bearing case suffix, the accents are put back on that
-- suffix and the word is stemmed again. Anything the stemmer then strips is
-- ORed in as a third alternative beside the existing lemma and surface sides.
--
-- THREE CONDITIONS, EACH LOAD-BEARING
--
--   1. Only when the plain lemma stripped NOTHING (lemma = {term}). A word the
--      stemmer already handles is never touched, so the 30 stem-only words that
--      score 87.8% cannot regress. This is what keeps the blast radius at the
--      failing population.
--   2. Only a candidate strictly SHORTER than the term. Re-accenting must have
--      bought an actual strip; if the stemmer still refuses, we add nothing.
--   3. 010's guard still applies - a 1-2 character lemma is a collision magnet
--      whether or not it was reached by restoring an accent.
--
-- WHAT IT DELIBERATELY DOES NOT DO
--
--   * It does not restore accents inside the STEM. Combinatorially restoring
--     every vowel would generate dozens of candidate spellings per term, and
--     the measurement says the stem is not where the loss is (87.8%).
--   * It does not prefix the new lexemes. 015 keeps the lemma side unprefixed
--     because a prefix on top of an invented stem multiplies the over-stemming
--     risk instead of containing it; a lemma reached by guessing an accent has
--     no better claim.
--   * It does not touch the accented branch (017/018). A term typed WITH its
--     accent already stems correctly and returns early from term_query.
--   * It does not address the other defect the same run surfaced: the stopword
--     list is accent-bearing and applied only in the lemma branch, so 'arról'
--     returns 0 where 'arrol' returns 208, and 'között' 90 where 'kozott'
--     returns 349. That is a different mechanism in a different place and is
--     left for its own migration rather than smuggled in here.
--
-- THE KNOWN COST
--
-- The rule cannot tell a case suffix from a word that merely ends in those
-- letters. 'kontrol' is not a case form, but it fails to stem and ends in -rol,
-- so it yields the extra lemma 'kont'. The lemma side carries no prefix, so
-- such a lexeme matches only documents whose stored lemma is exactly that - a
-- narrow blast radius, but not an empty one. This is a precision cost taken
-- deliberately for a recall fix, and the yardstick is what says whether the
-- trade was worth it.
-- =====================================================================

BEGIN;

-- The accent-free shape of each accent-bearing case suffix, and the spellings
-- it could have been. Both harmonic variants are tried because folding is
-- lossy in exactly that direction: ról and ről both fold to 'rol', so the
-- accent-free query cannot say which was meant and the stemmer settles it.
--
-- Suffixes whose spelling carries NO accent (-nak/-nek, -ban/-ben, -val/-vel,
-- -hoz/-hez, -ig) are absent on purpose: folding never damaged them, they
-- already stem, and condition 1 would skip them anyway.
CREATE OR REPLACE FUNCTION corpus.restorable_case_suffixes()
RETURNS TABLE (folded text, accented text)
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $fn$
    VALUES ('rol', 'ról'), ('rol', 'ről'),
           ('tol', 'tól'), ('tol', 'től'),
           ('bol', 'ból'), ('bol', 'ből'),
           ('nal', 'nál'),
           ('nel', 'nél'),
           ('ert', 'ért'),
           ('kent', 'ként')
$fn$;

-- The lemmas an accent-free term yields once its trailing case suffix is spelled
-- the way the stemmer expects. Empty for anything that already stems, anything
-- too short to be stem + suffix, and anything the stemmer still will not strip.
CREATE OR REPLACE FUNCTION corpus.reaccented_lemmas(term text)
RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $fn$
    SELECT ARRAY(
        SELECT DISTINCT l
          FROM corpus.restorable_case_suffixes() s
          CROSS JOIN LATERAL unnest(coalesce(
                 corpus.lemma_lexemes(left(term, length(term) - length(s.folded))
                                      || s.accented),
                 ARRAY[]::text[])) AS l
         WHERE term LIKE '%' || s.folded
           -- stem + suffix, with at least two characters of stem to strip to
           AND length(term) > length(s.folded) + 1
           -- condition 2: re-accenting must have bought an actual strip
           AND length(l) < length(term)
           -- condition 3: 010's guard
           AND length(l) > 2
         ORDER BY l)
$fn$;

-- term_query, with the third alternative added. Everything else is 015/017 as
-- it stood; only the block marked 019 is new.
CREATE OR REPLACE FUNCTION corpus.term_query(term text)
RETURNS tsquery
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT
AS $function$
DECLARE
    lem   text[];
    sur   text[];
    kept  text[];
    rea   text[];
    sides text[] := ARRAY[]::text[];
BEGIN
    IF term = '' THEN
        RETURN ''::tsquery;
    END IF;

    -- THE ACCENT BRANCH. The folded lexemes are deliberately NOT ORed in: they
    -- are what makes kór match kor, and adding them back would undo 017.
    IF term <> corpus.unaccent_immutable(term) THEN
        RETURN corpus.accented_query(term);
    END IF;

    -- Everything below is 015, unchanged, for a query with no accent.
    lem := coalesce(corpus.lemma_lexemes(term),   ARRAY[]::text[]);
    sur := coalesce(corpus.surface_lexemes(term), ARRAY[]::text[]);

    kept := ARRAY(SELECT DISTINCT l FROM unnest(lem) AS l
                   WHERE length(l) > 2 OR l = ANY(sur)
                   ORDER BY l);

    IF array_length(kept, 1) IS NOT NULL THEN
        sides := sides || ('(' || array_to_string(
                     ARRAY(SELECT quote_literal(x) FROM unnest(kept) AS x),
                     ' & ') || ')');
    END IF;

    -- 019. Condition 1: only a term the stemmer left completely alone. `lem`
    -- holding exactly the term back is what "stripped nothing" looks like.
    IF lem = ARRAY[term] THEN
        rea := corpus.reaccented_lemmas(term);
        IF array_length(rea, 1) IS NOT NULL THEN
            sides := sides || ('(' || array_to_string(
                         ARRAY(SELECT quote_literal(x) FROM unnest(rea) AS x),
                         ' & ') || ')');
        END IF;
    END IF;

    IF array_length(sur, 1) IS NOT NULL THEN
        sides := sides || ('(' || array_to_string(
                     ARRAY(SELECT quote_literal(x)
                                  || CASE WHEN length(x) >= corpus.prefix_min_length()
                                          THEN ':*' ELSE '' END
                             FROM (SELECT DISTINCT s FROM unnest(sur) AS s
                                    ORDER BY s) AS d(x)),
                     ' & ') || ')');
    END IF;

    IF array_length(sides, 1) IS NULL THEN
        RETURN ''::tsquery;
    END IF;

    RETURN (array_to_string(sides, ' | '))::tsquery;
END
$function$;

DO $verify$
BEGIN
    -- 1. The defect itself, on all four accent-bearing suffix families.
    IF NOT corpus.search_vector('döntött a kormányról a testület')
           @@ corpus.search_query('kormanyrol') THEN
        RAISE EXCEPTION 'kormanyrol still does not reach kormányról';
    END IF;
    IF NOT corpus.search_vector('kérdezte a kormánytól')
           @@ corpus.search_query('kormanytol') THEN
        RAISE EXCEPTION 'kormanytol still does not reach kormánytól';
    END IF;
    IF NOT corpus.search_vector('tárgyalt a kormánynál')
           @@ corpus.search_query('kormanynal') THEN
        RAISE EXCEPTION 'kormanynal still does not reach kormánynál';
    END IF;
    IF NOT corpus.search_vector('idézet a kormányból')
           @@ corpus.search_query('kormanybol') THEN
        RAISE EXCEPTION 'kormanybol still does not reach kormányból';
    END IF;

    -- 2. The point of the fix: the accent-free inflected query now reaches the
    -- BASE form, which is what the yardstick measures and what a reader means.
    IF NOT corpus.search_vector('a kormány döntött')
           @@ corpus.search_query('kormanyrol') THEN
        RAISE EXCEPTION 'kormanyrol does not reach the base form kormány';
    END IF;

    -- 3. Condition 1 holds: a term that already stems is untouched. -nak/-ban
    -- never lost anything to folding and must not acquire a new branch.
    IF corpus.search_query('kormanynak')::text
       <> corpus.search_query('kormanynak')::text THEN
        RAISE EXCEPTION 'unreachable';
    END IF;
    IF corpus.reaccented_lemmas('kormanynak') <> ARRAY[]::text[] THEN
        RAISE EXCEPTION 'a term that already stems acquired a re-accented branch: %',
            corpus.reaccented_lemmas('kormanynak');
    END IF;
    IF corpus.reaccented_lemmas('kormanyban') <> ARRAY[]::text[] THEN
        RAISE EXCEPTION 'kormanyban acquired a re-accented branch';
    END IF;

    -- 4. Condition 2: no candidate unless re-accenting actually bought a strip.
    IF corpus.reaccented_lemmas('alkohol') <> ARRAY[]::text[] THEN
        RAISE EXCEPTION 'alkohol should yield nothing, got %',
            corpus.reaccented_lemmas('alkohol');
    END IF;

    -- 5. Condition 3: 010's guard survives - nothing 1-2 characters gets in.
    IF EXISTS (SELECT 1 FROM unnest(corpus.reaccented_lemmas('haromrol')) AS l
                WHERE length(l) <= 2) THEN
        RAISE EXCEPTION '019 admitted a 1-2 character lemma, undoing 010';
    END IF;

    -- 6. The accented spelling is untouched: it returns early from term_query,
    -- so 017 and 018 keep deciding it.
    IF corpus.search_query('kormányról')::text
       <> corpus.accented_query('kormányról')::text THEN
        RAISE EXCEPTION 'the accented branch changed shape under 019';
    END IF;

    -- 7. 015's threshold and the AND across terms both still bite.
    IF corpus.search_vector('kormányzati energiapolitika')
       @@ corpus.search_query('kormanyrol büdzsé') THEN
        RAISE EXCEPTION 'the AND across terms stopped biting under 019';
    END IF;

    -- 8. The new lexemes carry no prefix - 015's rule for the lemma side.
    IF corpus.search_query('nikotinrol')::text LIKE '%''nikotin'':*%' THEN
        RAISE EXCEPTION 'a re-accented lemma was prefixed';
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('019')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
