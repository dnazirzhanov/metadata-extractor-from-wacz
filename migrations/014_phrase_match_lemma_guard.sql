-- =====================================================================
-- 014  The phrase verifier stops trusting a lemma the stemmer invented
-- =====================================================================
-- THE DEFECT
--
-- corpus.phrase_match is the EXACT VERIFICATION step of phrase search - the
-- thing that decides whether a phrase really occurs, after the index has
-- narrowed the candidates. It builds its own tsquery and never goes through
-- corpus.search_query, so migration 010's lemma guard never reaches it:
--
--     phraseto_tsquery('corpus.hungarian_lemma', 'Orban')  ->  'or'      unguarded
--     corpus.search_query('Orban')                         ->  'orban'   guarded
--
-- 'or' is the over-stem 010 exists to suppress - alone it matched 14% of the
-- corpus. Inside phrase_match it is still live. Measured on the 1,008-article
-- evaluation corpus, two blocks satisfy phrase_match(block_text, 'Orban') and
-- both are about somebody's NOSE, because 'orra' stems to 'or':
--
--     metropol.hu #6  "...a saját tavában, amelyet az apja orra..."
--     ripost.hu   #6  "...bevallotta: orra miatt egyszer már feküdt..."
--
-- WHY NOBODY NOTICED
--
-- scripts/search.py always ANDs the GIN prefilter in front:
--
--     b.text_tsv @@ corpus.search_query(...)          <- guarded, excludes them
--     AND corpus.phrase_match(b.block_text, ...)      <- unguarded
--
-- so the AND masks it completely. Measured over 7 probes, the prefiltered path
-- is byte-identical before and after this migration: 571, 216, 214, 178, 132,
-- 15, 6. What was actually wrong is that the PREFILTER was carrying a
-- correctness responsibility the comments assign to phrase_match. Anything
-- calling phrase_match on its own - scripts/search_demo.sql, a psql session, a
-- future refactor that drops the prefilter - got the collision back.
--
-- THE FIX
--
-- Gate the LEMMA branch on whether the needle's lemma lexemes survive 010's
-- rule. The SURFACE branch needs no guard: corpus.hungarian_surface does not
-- stem, so it cannot over-stem.
--
-- The guard must read to_tsvector('corpus.hungarian_lemma', ...) and NOT
-- corpus.lemma_lexemes(). Those are different lexeme sets - lemma_lexemes is
-- stem-THEN-unaccent, the configuration is stem-only - and phrase_match matches
-- against the configuration's output, so the guard has to be computed over the
-- very lexemes it is guarding. Getting this backwards would guard the wrong
-- alphabet and silently do nothing.
--
-- WHY THE NEEDLE AND NOT THE HAYSTACK
--
-- The alternative is to AND the guarded lexeme test onto the result:
--     phrase_match(...) AND search_vector(haystack) @@ search_query(needle)
-- Both were measured and they agree on every probe - Orban 214, idén 132,
-- Simon 6, USA 15, Orbán 216, Magyarországról 571. The needle-side guard was
-- chosen because it never rebuilds a vector over the document:
--
--     query              today     needle guard     haystack guard
--     Orbán Viktor      12.4 ms         24.1 ms            48.3 ms
--     Magyarországról   41.2 ms         41.1 ms           205.2 ms
--
-- WHAT GETS STRICTER
--
-- Standalone phrase_match, in exactly the way 010 already decided for the query
-- side - the same lexemes, the same trade:
--
--     Orban   216 -> 214        idén   262 -> 132
--     Simon    28 ->   6        USA     25 ->  15
--
-- Query side only. No table rewritten, no GIN index rebuilt, no stored data
-- touched. Same shape as 010-013.
-- =====================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------
-- 0. Refuse to run if this became a data migration
-- ---------------------------------------------------------------------
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
       AND pg_get_expr(d.adbin, d.adrelid) ILIKE '%phrase_match%';

    IF dependent IS NOT NULL THEN
        RAISE EXCEPTION
            'phrase_match is referenced by STORED generated column(s): %. '
            'Redefining it would desynchronise them.', dependent;
    END IF;
END
$guard$;

-- ---------------------------------------------------------------------
-- 1. Record how phrase_match answers today
-- ---------------------------------------------------------------------
-- Captured BEFORE the rewrite. Section 4 requires every pair to come back
-- unchanged EXCEPT the one this migration exists to flip, which makes "only the
-- collision moved" a checked claim rather than an intention.
CREATE TEMP TABLE _pm_before ON COMMIT DROP AS
SELECT c.haystack, c.needle, corpus.phrase_match(c.haystack, c.needle) AS matched
FROM (VALUES
    -- the defect: 'orra' stems to 'or', which is what 'Orban' over-stems to
    ('az apja orra a tóban',               'Orban'),
    ('bevallotta: orra miatt feküdt kés alatt', 'Orban'),
    -- the accent-free spelling must KEEP working where the name really occurs
    ('Orbán Viktor Brüsszelben tárgyalt',  'Orban'),
    ('Orban Viktor Brusszelben targyalt',  'Orban'),
    -- ordinary phrases, none of which may move
    ('Orbán Viktor Brüsszelben tárgyalt',  'Orbán Viktor'),
    ('Orbán Viktor Brüsszelben tárgyalt',  'Viktor Orbán'),
    ('Orbán Viktor Brüsszelben tárgyalt',  'Orbán'),
    ('Orbánnak üzent a miniszter',         'Orbán'),
    ('a magyar kormány Magyarországról beszélt', 'Magyarországról'),
    ('elhúzódó háború és migrációs nyomás','migrációs nyomás'),
    ('az orosz-ukrán háború ötödik éve',   'orosz-ukrán háború'),
    ('a Kozep-Azsia kerdes',               'Közép-Ázsia'),
    ('semmi köze a kérdéshez',             'Orbán'),
    -- a stopword needle must behave exactly as before
    ('ki a felelős',                       'ki')
) AS c(haystack, needle);

-- ---------------------------------------------------------------------
-- 2. Is the lemma branch trustworthy for this needle?
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION corpus.lemma_phrase_safe(needle text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE PARALLEL SAFE STRICT
AS $fn$
    -- 010's rule, applied to the lexemes phrase_match actually matches against:
    -- a lemma of 1-2 characters that no spelling of the needle produces on the
    -- surface was INVENTED by the stemmer, and is a collision magnet.
    SELECT NOT EXISTS (
        SELECT 1
        FROM unnest(tsvector_to_array(
                 to_tsvector('corpus.hungarian_lemma', needle))) AS l
        WHERE length(l) <= 2
          AND NOT (l = ANY(tsvector_to_array(
                       to_tsvector('corpus.hungarian_surface', needle))))
    )
$fn$;

COMMENT ON FUNCTION corpus.lemma_phrase_safe(text) IS
    'False when a needle over-stems on the lemma side - some lexeme of 1-2 '
    'characters that no spelling of the needle produces on the surface. '
    'corpus.phrase_match uses it to skip its lemma branch (014). Computed over '
    'to_tsvector(hungarian_lemma), NOT corpus.lemma_lexemes: those are '
    'different lexeme sets, and phrase_match matches against the former.';

-- ---------------------------------------------------------------------
-- 3. The verifier itself
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION corpus.phrase_match(haystack text, needle text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE PARALLEL SAFE
AS $fn$
    SELECT haystack IS NOT NULL AND needle IS NOT NULL
       AND (
            -- lemma: tolerates INFLECTION, misses the accent-free spelling.
            -- Skipped entirely when the needle over-stems (014).
            (corpus.lemma_phrase_safe(needle)
             AND to_tsvector('corpus.hungarian_lemma', haystack)
                     @@ phraseto_tsquery('corpus.hungarian_lemma', needle))
            -- surface: tolerates NO ÉKEZET, misses inflection. Unstemmed, so
            -- it cannot over-stem and needs no guard.
         OR to_tsvector('corpus.hungarian_surface', haystack)
                     @@ phraseto_tsquery('corpus.hungarian_surface', needle)
       )
$fn$;

COMMENT ON FUNCTION corpus.phrase_match(text, text) IS
    'True when needle occurs in haystack as an adjacent word sequence. '
    'Recomputes DOCUMENT-ORDER vectors for the row, because corpus.search_vector '
    'positions are meaningless (009). The lemma branch is skipped when the '
    'needle over-stems, so this is safe called on its own and not only behind '
    'the GIN prefilter (014).';

-- ---------------------------------------------------------------------
-- 4. Prove it
-- ---------------------------------------------------------------------
DO $verify$
DECLARE
    bad text;
BEGIN
    -- 4a. THE central assertion: exactly the two nose cases flip, nothing else.
    SELECT string_agg(format('%L / %L: was %s, now %s',
                             haystack, needle, matched,
                             corpus.phrase_match(haystack, needle)), E'\n  ')
      INTO bad
      FROM _pm_before
     WHERE (corpus.phrase_match(haystack, needle) IS DISTINCT FROM matched)
       AND needle <> 'Orban';

    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'phrase_match changed an answer it must not:%s  %s',
            E'\n', bad;
    END IF;

    -- 4b. The collision is gone.
    IF corpus.phrase_match('az apja orra a tóban', 'Orban') THEN
        RAISE EXCEPTION 'bare "orra" still satisfies the phrase "Orban"';
    END IF;
    IF corpus.phrase_match('bevallotta: orra miatt feküdt kés alatt', 'Orban') THEN
        RAISE EXCEPTION 'the second nose case still matches "Orban"';
    END IF;

    -- 4c. ...without taking the real name with it. This is the whole risk of
    -- the change, so it is asserted in both spellings.
    IF NOT corpus.phrase_match('Orbán Viktor Brüsszelben tárgyalt', 'Orban') THEN
        RAISE EXCEPTION 'the accent-free spelling stopped finding the real name';
    END IF;
    IF NOT corpus.phrase_match('Orban Viktor Brusszelben targyalt', 'Orban') THEN
        RAISE EXCEPTION 'the accent-free spelling stopped matching accent-free text';
    END IF;

    -- 4d. Order still decides, and inflection is still tolerated.
    IF NOT corpus.phrase_match('Orbán Viktor Brüsszelben tárgyalt', 'Orbán Viktor') THEN
        RAISE EXCEPTION 'a real phrase stopped matching';
    END IF;
    IF corpus.phrase_match('Orbán Viktor Brüsszelben tárgyalt', 'Viktor Orbán') THEN
        RAISE EXCEPTION 'a reversed phrase started matching';
    END IF;
    IF NOT corpus.phrase_match('Orbánnak üzent a miniszter', 'Orbán') THEN
        RAISE EXCEPTION 'the lemma branch stopped tolerating inflection';
    END IF;

    -- 4e. The guard fires where it should and only there.
    IF NOT corpus.lemma_phrase_safe('Orbán') THEN
        RAISE EXCEPTION 'Orbán stems to orb (3 chars) and must stay safe';
    END IF;
    IF corpus.lemma_phrase_safe('Orban') THEN
        RAISE EXCEPTION 'Orban stems to the invented "or" and must be unsafe';
    END IF;
    IF NOT corpus.lemma_phrase_safe('Magyarországról') THEN
        RAISE EXCEPTION 'an ordinary long word must stay safe';
    END IF;
    IF NOT corpus.lemma_phrase_safe('Orbán Viktor') THEN
        RAISE EXCEPTION 'a multi-word needle of safe words must stay safe';
    END IF;
    -- One unsafe word poisons the branch for the whole needle: phraseto_tsquery
    -- builds ONE chain, so it cannot be disabled per word.
    IF corpus.lemma_phrase_safe('Orban Viktor') THEN
        RAISE EXCEPTION 'one over-stemming word must make the whole needle unsafe';
    END IF;

    -- 4f. NULL handling is unchanged - the function is deliberately not STRICT.
    IF corpus.phrase_match(NULL, 'Orbán') IS NOT FALSE
       OR corpus.phrase_match('Orbán Viktor', NULL) IS NOT FALSE THEN
        RAISE EXCEPTION 'NULL handling changed';
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('014')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
