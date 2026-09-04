-- =====================================================================
-- 017  If you typed the accent, you meant it
-- =====================================================================
-- 016 stored an accent-preserving lemma on every vector. Nothing asked for it.
-- 017 is the asking.
--
-- THE RULE
--
--     query contains an accent  ->  match the ACCENT-PRESERVING lemma
--     query contains none       ->  match exactly as before, accent-blind
--
-- The second half is not a compromise, it is the point. Migration 007 exists so
-- that a foreign keyboard finds Hungarian text - "kormany" must keep finding
-- "kormány". A user who does not type accents cannot be assumed to have meant
-- any particular one. A user who DOES type them has said which word they mean.
--
-- MEASURED ON THE 1,008-ARTICLE CORPUS
--
--     term            today   accent-sensitive
--     ügy               380                119     ugyan, ügyel... separated
--     kór               166                 10     kor and kör separated
--     kör               166                 99     kor and kór separated
--     Magyarország      254                254     unchanged
--     Magyarországról   254                254     unchanged
--     Orbánnak          102                102     unchanged
--     háború            101                101     unchanged
--     koronavírus        63                 63     unchanged
--     nyomás             31                 31     unchanged
--     párt              193                131     part/riverbank separated
--     Orbán             103                102     -1
--     fejlesztés         87                 85     -2
--
-- The recall cost is 1-2 articles on two probes and zero on the rest. Those are
-- documents that spell the word WITHOUT the accent, which an accented query now
-- declines to match. That is the trade, and it is small because Hungarian text
-- in this corpus is written with its accents.
--
-- 015'S PREFIX MUST FOLLOW THE ACCENT
--
-- The first version of this measurement showed kormány 288 -> 184, which looked
-- like a catastrophic recall loss and was not: it was 015's compound prefix
-- being dropped along with the folded lexemes. The prefix rule and the 010
-- length guard both apply to whichever lexemes are chosen - see section 2.
-- Anything that forgets this will read as an accent problem and be misdiagnosed.
--
-- WHAT THIS STILL DOES NOT FIX, BY CONSTRUCTION
--
-- "Viktor" carries no accent, so it takes the accent-blind branch and still
-- matches "Viktória" (both fold to 'viktor'; only their accented lemmas,
-- 'viktor' and 'viktór', differ). No matching rule can separate them without
-- breaking accent-blindness. corpus.accented_query() below exists so RANKING
-- can: scripts/search.py scores an accent-exact match above a folded-only one,
-- so Viktória sorts below the real Viktor articles instead of among them.
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
       AND pg_get_expr(d.adbin, d.adrelid) ILIKE '%term_query%';

    IF dependent IS NOT NULL THEN
        RAISE EXCEPTION
            'term_query is referenced by STORED generated column(s): %.', dependent;
    END IF;
END
$guard$;

-- ---------------------------------------------------------------------
-- 1. Record what ACCENT-FREE terms answer today
-- ---------------------------------------------------------------------
-- These take the unchanged branch and must come back byte-identical. If any of
-- them moves, the accent branch has leaked into the accent-blind path.
CREATE TEMP TABLE _tq_plain_before ON COMMIT DROP AS
SELECT t AS term, corpus.search_query(t)::text AS expected
FROM unnest(ARRAY['kor','Orban','Viktor','magyar','part','ugy','usa','ki',
                  'magyarorszag','kormany','Orban Viktor']) AS t;

-- ---------------------------------------------------------------------
-- 2. The accented lexemes of a term, guarded and prefixed like any other
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION corpus.accented_lexemes(t text)
RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $fn$
    SELECT tsvector_to_array(to_tsvector('corpus.hungarian_lemma', t))
$fn$;

COMMENT ON FUNCTION corpus.accented_lexemes(text) IS
    'The accent-PRESERVING lemma lexemes - the third component corpus.'
    'search_vector stores since 016. These are what tell kór from kor.';

CREATE OR REPLACE FUNCTION corpus.accented_query(t text)
RETURNS tsquery
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT
AS $fn$
DECLARE
    acc   text[];
    sur   text[];
    kept  text[];
BEGIN
    acc := coalesce(corpus.accented_lexemes(t),   ARRAY[]::text[]);
    sur := coalesce(corpus.surface_lexemes(t),    ARRAY[]::text[]);

    -- 010's guard: an invented 1-2 character lemma is a collision magnet
    -- whether or not it kept its accent.
    kept := ARRAY(SELECT DISTINCT l FROM unnest(acc) AS l
                   WHERE length(l) > 2 OR l = ANY(sur)
                   ORDER BY l);

    IF array_length(kept, 1) IS NULL THEN
        RETURN ''::tsquery;
    END IF;

    -- 015's compound prefix, applied to the accented lexemes for exactly the
    -- same reason it is applied to the surface ones.
    RETURN ('(' || array_to_string(
        ARRAY(SELECT quote_literal(x)
                     || CASE WHEN length(x) >= corpus.prefix_min_length()
                             THEN ':*' ELSE '' END
                FROM unnest(kept) AS x),
        ' & ') || ')')::tsquery;
END
$fn$;

COMMENT ON FUNCTION corpus.accented_query(text) IS
    'One term as an ACCENT-SENSITIVE tsquery, with the 010 guard and the 015 '
    'prefix applied. Used by corpus.term_query when the term carries an accent, '
    'and by scripts/search.py to score an accent-exact match above a '
    'folded-only one even when the query carries no accent (017).';

-- ---------------------------------------------------------------------
-- 3. term_query picks the branch
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION corpus.term_query(term text)
RETURNS tsquery
LANGUAGE plpgsql
IMMUTABLE PARALLEL SAFE STRICT
AS $fn$
DECLARE
    lem   text[];
    sur   text[];
    kept  text[];
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
$fn$;

COMMENT ON FUNCTION corpus.term_query(text) IS
    'One query term as a tsquery. A term carrying an ACCENT is matched against '
    'the accent-preserving lemma alone, so kór stops meaning kor (017). A term '
    'without one keeps the accent-blind expansion of 007/010/013/015, so a '
    'foreign keyboard still finds Hungarian text.';

-- ---------------------------------------------------------------------
-- 4. Prove it
-- ---------------------------------------------------------------------
DO $verify$
DECLARE
    bad text;
BEGIN
    -- 4a. Accent-free queries are byte-identical. This is 007's contract.
    SELECT string_agg(format('%L: was %s, now %s',
                             term, expected, corpus.search_query(term)::text), E'\n  ')
      INTO bad
      FROM _tq_plain_before
     WHERE corpus.search_query(term)::text IS DISTINCT FROM expected;

    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'an accent-free query changed:%s  %s', E'\n', bad;
    END IF;

    -- 4b. The collision this migration exists for.
    IF corpus.search_vector('a kor szelleme') @@ corpus.search_query('kór') THEN
        RAISE EXCEPTION 'kór still matches kor';
    END IF;
    IF corpus.search_vector('a kör közepe') @@ corpus.search_query('kór') THEN
        RAISE EXCEPTION 'kór still matches kör';
    END IF;
    IF NOT corpus.search_vector('a kór terjed') @@ corpus.search_query('kór') THEN
        RAISE EXCEPTION 'kór stopped matching its own word';
    END IF;
    IF corpus.search_vector('ugyanaz a helyzet') @@ corpus.search_query('ügy') THEN
        RAISE EXCEPTION 'ügy still matches ugyanaz';
    END IF;

    -- 4c. Accent-blindness survives for the query that did not ask.
    IF NOT (corpus.search_vector('a kór terjed') @@ corpus.search_query('kor')
        AND corpus.search_vector('a kör közepe') @@ corpus.search_query('kor')
        AND corpus.search_vector('a kor szelleme') @@ corpus.search_query('kor')) THEN
        RAISE EXCEPTION 'an accent-free query lost its accent-blindness';
    END IF;
    IF NOT corpus.search_vector('a kormány döntött') @@ corpus.search_query('kormany') THEN
        RAISE EXCEPTION 'a foreign keyboard stopped finding accented text';
    END IF;

    -- 4d. Inflection still works on the accented branch.
    IF NOT corpus.search_vector('Orbánnak üzent') @@ corpus.search_query('Orbán') THEN
        RAISE EXCEPTION 'the accented branch lost inflection tolerance';
    END IF;

    -- 4e. 015's prefix follows the accent. Without this, kormány collapses from
    -- 288 articles to 184 and looks like an accent bug.
    IF corpus.search_query('kormány')::text NOT LIKE '%:*%' THEN
        RAISE EXCEPTION 'the compound prefix was lost on the accented branch: %',
            corpus.search_query('kormány')::text;
    END IF;
    IF NOT corpus.search_vector('a kormányzat döntött') @@ corpus.search_query('kormány') THEN
        RAISE EXCEPTION 'kormányzat stopped answering kormány';
    END IF;

    -- 4f. Viktor/Viktória, and the limit of what a stored vector can do.
    --
    -- corpus.search_vector holds all three components in ONE tsvector, so a
    -- lexeme cannot be attributed to the component it came from. For an
    -- accent-free term that is fatal: accented_query('Viktor') is 'viktor',
    -- which the FOLDED component supplies for Viktória as well. Asserted here
    -- so nobody re-derives it the hard way:
    IF NOT corpus.search_vector('Viktória királynő') @@ corpus.accented_query('Viktor') THEN
        RAISE EXCEPTION 'expected the stored vector to be unable to separate these';
    END IF;

    -- Against a FRESHLY COMPUTED accented vector - one component, no folded
    -- lexemes to leak - it separates cleanly. That is the vector
    -- scripts/search.py computes per row for the ranking bonus.
    IF NOT to_tsvector('corpus.hungarian_lemma', 'Orbán Viktor')
           @@ corpus.accented_query('Viktor') THEN
        RAISE EXCEPTION 'accented_query does not recognise the real Viktor';
    END IF;
    IF to_tsvector('corpus.hungarian_lemma', 'Viktória királynő')
       @@ corpus.accented_query('Viktor') THEN
        RAISE EXCEPTION 'accented_query fails to separate Viktória from Viktor';
    END IF;

    -- ...while MATCHING still returns both, by design.
    IF NOT corpus.search_vector('Viktória királynő') @@ corpus.search_query('Viktor') THEN
        RAISE EXCEPTION 'the accent-free branch stopped being accent-blind';
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('017')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
