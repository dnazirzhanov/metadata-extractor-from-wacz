-- =====================================================================
-- 015  Hungarian writes compounds as one word; search should find them
-- =====================================================================
-- THE GAP
--
-- Hungarian closes its compounds: koronavírus + teszt is written
-- "koronavírusteszt", one token, one lexeme. The stemmer does not split it, so
-- searching "koronavírus" cannot find it. Measured on the evaluation corpus,
-- these words all exist and none of them answered their own head word:
--
--     koronavírusteszt   koronavírustesztek   koronavírusteszthez
--     koronavírusos      nyomásgyakorlás      nyomáscsökkenés
--
-- Derivational suffixes do the same: -os, -zat, -párti are not case endings, so
-- snowball leaves them on. This is the same family as the two recall misses
-- search_report still reports.
--
-- THE FIX, AND WHY IT IS A PREFIX AND NOT A SPLIT
--
-- The linguistically correct fix is decompounding - an ispell/hunspell
-- dictionary that indexes "koronavírusteszt" as koronavírus + teszt. Postgres
-- supports it, but the container ships only hunspell_sample; there is no
-- Hungarian dictionary installed, and adding one changes corpus.search_vector,
-- which means rebuilding three STORED generated columns and their GIN indexes
-- over an eventual 58M blocks. That is a planned operation, not a migration.
--
-- A prefix on the SURFACE lexeme approximates it at query-side cost only.
-- Hungarian compounds keep the head word intact at the FRONT, which is exactly
-- what a prefix matches.
--
-- WHY LENGTH 6, MEASURED
--
-- A prefix on a short lexeme is catastrophic, because short letter-sequences
-- are coincidences rather than morpheme boundaries. Counting the distinct
-- corpus word-forms each term would newly admit, over the 400 most common title
-- words:
--
--     lexeme length     3      4      5      6      7      8
--     forms per term  173     27     18     15    7.5    3.6
--
-- and inspecting what those forms actually are:
--
--     kor    (3) ->  kora, korábbi, korabeli...  464 forms, all about age,
--                    nothing about "kór" (disease).            CATASTROPHIC
--     idén   (4) ->  identitás, identitásukat, identity        FALSE
--     Simon  (5) ->  simonyi                                   FALSE (a surname)
--     Péter  (5) ->  péterfy, péterffy, pétervásárai           FALSE
--     orosz  (5) ->  oroszország, oroszbarát, oroszellenes     correct
--     magyar (6) ->  magyarok, magyarul, magyarország  correct
--                    magyaráz, magyarázat, magyarázta  FALSE - "to explain",
--                    a different word, ~15 forms on a very common query term
--     nyomás (6) ->  nyomásgyakorlás, nyomáscsökkenés  correct
--                    nyomasztó                         FALSE (a different family)
--     kormány(7) ->  kormányfő, kormányzat, kormánypárt, kormánytag,
--                    kormányzás, kormányinfó ... 113 forms, ALL government
--     energia(7) ->  energiaár, energiaválság, energiaellátás  ALL energy
--     nyugdíj(7) ->  nyugdíjas, nyugdíjaskor, nyugdíjasklub    ALL pension
--     járvány(7) ->  járványügyi, járványhelyzet, járványveszély  ALL epidemic
--     baleset(7) ->  baleseti, balesetmentes, balesetveszély   ALL accident
--
-- 7 is the first length at which no false family was observed. 6 was tried
-- first and rejected on evidence: magyar->magyaráz is wrong, "magyar" is one of
-- the most common words a reader of this corpus will type, and the family is
-- ~15 forms wide. nyomás->nyomasztó is the same shape. The only borderline case
-- at 7 is helyett->helyettes, which is a genuine morphological relative.
--
-- NOT a reason for the threshold, though it looks like one: searching "Viktor"
-- already returns "Viktória", because the stemmer takes Viktória to the lexeme
-- 'viktor'. That is a pre-existing lemma collision, true before this migration
-- and unchanged by it, and prefixing neither causes nor worsens it.
--
-- There is no sharp cliff in the data - forms per term decay smoothly, 173 at
-- 3 characters, 27 at 4, 18 at 5, 15 at 6, 7.5 at 7, 3.6 at 8 - so this is a
-- judgement placed at the last length where a false match was actually seen,
-- not a threshold the numbers handed over. corpus.prefix_min_length() exists so
-- that judgement is revisable in one place.
--
-- SURFACE ONLY. The lemma side keeps no prefix: it already carries the
-- over-stemming 010 had to guard against, and a prefix on top of an invented
-- stem multiplies that risk instead of containing it.
--
-- MEASURED EFFECT (articles, 1,008-article corpus)
--
--     kormány    184 -> 288        migráció    33 ->  47
--     energia     28 ->  63        gazdaság   126 -> 139
--     fejlesztés  85 ->  87        koronavírus 63 ->  63 (already found via metadata)
--     Orbán, Magyarország, választás, oktatás, bíróság, rendőrség: unchanged
--
-- Deliberately NOT fixed, because their head words are 6 characters and fall
-- below the threshold: nyomás -> nyomásgyakorlás (+14 articles) and
-- háború -> háborúellenes (+9). nyomásgyakorlás is one of the two misses
-- search_report still reports, and it stays.
--
-- Prefix queries stay index-served: 'kormany':* is a Bitmap Index Scan on
-- content_block_text_tsv_idx returning 594 rows in 9.4 ms.
--
-- A CAVEAT THAT MUST BE STATED
--
-- scripts/search_report.py's independent yardstick tests whether a term STARTS
-- A WORD in the document. That is prefix semantics. So the yardstick will score
-- this change favourably BY CONSTRUCTION and cannot be used to validate it -
-- exactly the self-agreement trap that migration 010's notes warn about. The
-- evidence for this change is the word lists above, not the recall number.
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
-- 1. Record what the SHORT terms answer today
-- ---------------------------------------------------------------------
-- Terms below the threshold must come back byte-identical: this migration is
-- supposed to touch long terms only.
CREATE TEMP TABLE _tq_short_before ON COMMIT DROP AS
SELECT t AS term, corpus.search_query(t)::text AS expected
FROM unnest(ARRAY['kor','USA','ki','kik','idén','Orbán','Orban','Simon',
                  'Péter','orosz','ukrán','Viktor','magyar','nyomás','háború',
                  'Orbán Viktor']) AS t;

-- ---------------------------------------------------------------------
-- 2. The threshold, in one place
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION corpus.prefix_min_length()
RETURNS int
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $fn$ SELECT 7 $fn$;

COMMENT ON FUNCTION corpus.prefix_min_length() IS
    'Shortest surface lexeme that may be matched as a prefix, to reach Hungarian '
    'closed compounds (koronavírus -> koronavírusteszt). 7 is the first length '
    'at which no false family was observed: 6 admits Viktor->viktória and '
    'magyar->magyaráz. Revisable here, in one place. See 015.';

-- ---------------------------------------------------------------------
-- 3. term_query: prefix the surface side when the lexeme is long enough
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

    lem := coalesce(corpus.lemma_lexemes(term),   ARRAY[]::text[]);
    sur := coalesce(corpus.surface_lexemes(term), ARRAY[]::text[]);

    -- 010's guard, unchanged.
    kept := ARRAY(SELECT DISTINCT l FROM unnest(lem) AS l
                   WHERE length(l) > 2 OR l = ANY(sur)
                   ORDER BY l);

    -- Lemma side: lexemes ANDed (013), never prefixed - see the header.
    IF array_length(kept, 1) IS NOT NULL THEN
        sides := sides || ('(' || array_to_string(
                     ARRAY(SELECT quote_literal(x) FROM unnest(kept) AS x),
                     ' & ') || ')');
    END IF;

    -- Surface side: lexemes ANDed (013), each prefixed when long enough (015).
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
    'One query term as a tsquery: the lexemes of each SPELLING ANDed, the '
    'spellings ORed (013). The surface lexemes are matched as PREFIXES when at '
    'least corpus.prefix_min_length() characters long, so a closed compound '
    'answers its head word - koronavírus finds koronavírusteszt (015).';

-- ---------------------------------------------------------------------
-- 4. Prove it
-- ---------------------------------------------------------------------
DO $verify$
DECLARE
    bad text;
BEGIN
    -- 4a. Short terms are untouched. This is the containment claim.
    SELECT string_agg(format('%L: was %s, now %s',
                             term, expected, corpus.search_query(term)::text), E'\n  ')
      INTO bad
      FROM _tq_short_before
     WHERE corpus.search_query(term)::text IS DISTINCT FROM expected;

    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'a term below the threshold changed:%s  %s', E'\n', bad;
    END IF;

    -- 4b. The gap this migration exists to close.
    IF NOT corpus.search_vector('koronavírusteszthez állt sorba')
           @@ corpus.search_query('koronavírus') THEN
        RAISE EXCEPTION 'a closed compound still does not answer its head word';
    END IF;
    IF NOT corpus.search_vector('a kormányzat döntött')
           @@ corpus.search_query('kormány') THEN
        RAISE EXCEPTION 'kormányzat still does not answer kormány';
    END IF;

    -- 4c. The measured false matches must stay false, which is what the
    -- threshold is for.
    IF corpus.search_vector('az identitás kérdése')
       @@ corpus.search_query('idén') THEN
        RAISE EXCEPTION 'idén matched identitás - the threshold is too low';
    END IF;
    IF corpus.search_vector('a kormány döntött')
       @@ corpus.search_query('kór') THEN
        RAISE EXCEPTION 'kór matched kormány - the threshold is too low';
    END IF;
    IF corpus.search_vector('Simonyi Zsigmond nyelvész')
       @@ corpus.search_query('Simon') THEN
        RAISE EXCEPTION 'Simon matched Simonyi - the threshold is too low';
    END IF;
    -- The family that rejected a threshold of 6. (Viktor->Viktória looks like
    -- another, but that is the stemmer taking Viktória to 'viktor' and is true
    -- with or without this migration - see the header.)
    IF corpus.search_vector('a magyarázat egyszerű')
       @@ corpus.search_query('magyar') THEN
        RAISE EXCEPTION 'magyar matched magyarázat - the threshold is too low';
    END IF;

    -- 4d. A prefix must not leak across the AND between terms, nor turn a
    -- two-word query into a one-word one.
    IF corpus.search_vector('kormányzati energiapolitika')
       @@ corpus.search_query('kormány büdzsé') THEN
        RAISE EXCEPTION 'the AND across terms stopped biting under prefixing';
    END IF;

    -- 4e. The threshold is reachable and honest about itself.
    IF corpus.prefix_min_length() <> 7 THEN
        RAISE EXCEPTION 'unexpected threshold %', corpus.prefix_min_length();
    END IF;
    IF corpus.search_query('kormány')::text NOT LIKE '%:*%' THEN
        RAISE EXCEPTION 'a 7-character term should be prefixed, got %',
            corpus.search_query('kormány')::text;
    END IF;
    IF corpus.search_query('magyar')::text LIKE '%:*%' THEN
        RAISE EXCEPTION 'a 6-character term must NOT be prefixed, got %',
            corpus.search_query('magyar')::text;
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('015')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
