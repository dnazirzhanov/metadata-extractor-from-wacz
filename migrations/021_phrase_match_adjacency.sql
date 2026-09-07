-- =====================================================================
-- 021  A phrase is a phrase: adjacency, and the accent the reader typed
-- =====================================================================
-- WHY THIS EXISTS
--
-- corpus.phrase_match is documented as the exact layer - the thing that decides
-- truth after the GIN candidate filter has cheaply narrowed the field. A
-- benchmark over the 1,008-article evaluation corpus found it was not doing
-- that job, in two independent ways, and that the pipeline only LOOKED correct
-- because the candidate filter's stricter policy was overriding it upstream.
-- That is accidental, and this migration removes the accident.
--
-- The candidate filter itself was measured and is sound: 100% recall against an
-- adjacency reference computed in Python, and 200-2,300x cheaper than the layer
-- it protects. Nothing about the architecture changes here. Query side only: no
-- DDL on any table, no new index, no new generated column, no change to
-- corpus.search_vector, so nothing is reindexed and nothing is re-ingested.
--
-- DEFECT A - AN EDGE STOPWORD SILENTLY STOPPED THE PHRASE BEING A PHRASE
--
-- corpus.hungarian_lemma carries the Hungarian stopword list, so
-- phraseto_tsquery drops those words. Where the dropped word sits decides
-- whether that is harmless:
--
--     phraseto_tsquery(lemma,'kormány arról')         -> 'kormány'
--     phraseto_tsquery(lemma,'arról kormány')         -> 'kormány'
--     phraseto_tsquery(lemma,'kormány arról döntött') -> 'kormány' <2> 'döntöt'
--
-- An INTERIOR stopword becomes a <N> gap, which is exactly right and must not
-- be touched. A LEADING or TRAILING one is dropped outright and the phrase
-- quietly loses a word - a two-word phrase becomes a one-word query. Because
-- the branches are ORed, that degenerate branch then wins:
--
--     phrase_match('A kormány döntött. Erről semmit nem mondott arról
--                   a kérdésről.', 'kormány arról')            was TRUE
--
-- Six words apart, in different sentences. This reaches every phrase containing
-- a Hungarian function word at either end - arról, ezért, között, előtt, által,
-- saját - which is most phrases anyone would actually search for.
--
-- DEFECT B - THE FOLDED BRANCH WAS AVAILABLE TO AN ACCENTED NEEDLE
--
-- corpus.hungarian_surface is unaccent + simple, so it folds. Offering it to a
-- needle that was typed WITH its accent contradicts 017, whose whole point is
-- that typing the accent narrows the search:
--
--     phrase_match('Ludovic Orban Romaniaban', 'Orbán')        was TRUE
--     phrase_match('a kor jo volt', 'kór')                     was TRUE
--
-- The first is the Romanian politician, found on real blocks in the evaluation
-- corpus. The lemma branch alone already rejects him - 'Orban' stems to 'or'
-- while 'Orbán' stems to 'orb' - so the folded branch was the sole cause.
--
-- 017 IS ONE-WAY, AND STAYS ONE-WAY
--
-- Typing the accent narrows; typing without it does not. An accent-free needle
-- must still reach accented text, which is what the reader with no Hungarian
-- keyboard depends on, and 014 pins it:
--
--     phrase_match('Orbán Viktor Brüsszelben tárgyalt', 'Orban')  stays TRUE
--
-- So the gate is on the NEEDLE, never on the haystack.
--
-- THE SHAPE OF THE FIX
--
-- Three branches, gated the way corpus.term_query already gates itself - on
-- `needle <> corpus.unaccent_immutable(needle)` - so the accent policy is
-- written the same way in both halves of the engine:
--
--     lemma    hungarian_lemma      inflection tolerance
--                                   gated by 014's lemma_phrase_safe AND the
--                                   new edge guard
--     exact    simple               accent-preserving exact adjacency; this is
--                                   what rescues a stopword phrase
--     folded   hungarian_surface    accent-free permissiveness, for an
--                                   accent-free needle ONLY, and edge-guarded
--
-- `simple` is the configuration that makes this work and it was already in the
-- schema, used by 018's corpus.accented_raw_lexemes: it preserves accents, does
-- not stem, and has NO stopwords. to_tsvector('simple','arról') is 'arról':1,
-- and 'Orbán' and 'Orban' stay distinct lexemes under it.
--
-- WHAT THIS DELIBERATELY DOES NOT DO
--
--   * It does not touch the candidate filter. The measured architecture stands:
--     the filter may over-produce candidates, and must never under-produce.
--   * It does not add lemma_tsv, surface_tsv, an ordered vector, or an index.
--     The benchmark put the runtime cost ceiling near 1M content blocks; this
--     corpus has 10,589 and the projection is not evidence. That is a separate
--     scaling decision to be made after a staged ingest, not here.
--   * It does not make the lemma branch tolerate more inflection than it did.
--     'döntött' stems to 'döntöt' while 'döntöttek' stems to 'döntött', so a
--     phrase can still miss an inflected haystack. That is the stemmer's
--     behaviour, it is unchanged by this migration, and pretending otherwise
--     would mean inventing morphology.
--
-- THE KNOWN COST
--
-- The exact branch adds a third to_tsvector over the haystack. Measured
-- per-block cost of phrase_match was 35.4 microseconds before this change; the
-- extra branch is unstemmed and so the cheapest of the three. Set against it,
-- the edge guard SKIPS the lemma branch for the needles that used to take it
-- wrongly, so the change is close to cost-neutral and is dominated either way
-- by the candidate filter having already reduced the field.
-- =====================================================================

BEGIN;

-- Snapshot BEFORE anything changes, so the verify block can prove that only the
-- two defect classes moved. Same device 014 used when it last touched this
-- function.
CREATE TEMP TABLE _pm_before ON COMMIT DROP AS
SELECT h AS haystack, n AS needle, corpus.phrase_match(h, n) AS matched
FROM (VALUES
        ('Orbán Viktor Brüsszelben tárgyalt', 'Orbán Viktor'),
        ('Orbán Viktor Brüsszelben tárgyalt', 'Viktor Orbán'),
        ('Orbán Viktor Brüsszelben tárgyalt', 'Orban'),
        ('Orban Viktor Brusszelben targyalt', 'Orban'),
        ('Orbánnak üzent a miniszter',        'Orbán'),
        ('az apja orra a tóban',              'Orban'),
        ('a kormány döntött',                 'kormány'),
        ('a kormányról szól a hír',           'kormány'),
        ('az orosz-ukrán háború',             'orosz-ukrán háború'),
        ('a kór terjedt',                     'kór'),
        ('a kor jó volt',                     'kor'),
        ('Magyarországról érkezett',          'Magyarországról'),
        ('a kormányok arról döntöttek',       'kormány arról döntött'),
        ('az Európai Unió döntése',           'Európai Unió')
     ) AS v(h, n);

-- ---------------------------------------------------------------------
-- Does `cfg` keep the FIRST and LAST token of the needle?
--
-- phraseto_tsquery encodes the distance between surviving tokens, so a token
-- lost from the MIDDLE still constrains the phrase through the <N> gap either
-- side of it. A token lost from an END constrains nothing at all - the phrase
-- simply stops one word short - and that is the whole of defect A.
--
-- Positions are compared against `simple`, which drops nothing. Both go through
-- the SAME parser, so only the dictionaries differ and a missing position means
-- a dictionary discarded that token. Counting positions rather than lexemes is
-- deliberate: a tsvector deduplicates a repeated word into one lexeme carrying
-- two positions, and 'kormány kormány' is a two-token phrase.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION corpus.phrase_edges_survive(cfg regconfig, needle text)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $fn$
    SELECT s.tokens > 0
       AND 1 = ANY(k.kept)
       AND s.tokens = ANY(k.kept)
      FROM (SELECT coalesce(max(p), 0)::int AS tokens
              FROM unnest(to_tsvector('simple', needle)) AS v,
                   unnest(v.positions) AS p) AS s,
           (SELECT coalesce(array_agg(p::int), ARRAY[]::int[]) AS kept
              FROM unnest(to_tsvector(cfg, needle)) AS v,
                   unnest(v.positions) AS p) AS k
$fn$;

COMMENT ON FUNCTION corpus.phrase_edges_survive(regconfig, text) IS
    'True when cfg keeps the first and last token of the needle, so '
    'phraseto_tsquery''s distances constrain the whole phrase. An interior '
    'token lost to a stopword list is fine - it becomes a <N> gap. An edge one '
    'is not: the phrase silently loses a word (migrations/021).';

-- ---------------------------------------------------------------------
-- The exact layer. Deliberately NOT STRICT: it decides its own NULL answer, and
-- 014 relied on that.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION corpus.phrase_match(haystack text, needle text)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $fn$
    SELECT haystack IS NOT NULL AND needle IS NOT NULL
       AND (
            -- INFLECTION. Accent-preserving and stemmed, so 'Orbánnak' answers
            -- 'Orbán'. Skipped when the needle over-stems (014's guard) and now
            -- also when an edge token was swallowed by the stopword list (021).
            (corpus.lemma_phrase_safe(needle)
             AND corpus.phrase_edges_survive('corpus.hungarian_lemma', needle)
             AND to_tsvector('corpus.hungarian_lemma', haystack)
                     @@ phraseto_tsquery('corpus.hungarian_lemma', needle))

            -- EXACT. Accent-preserving, unstemmed, no stopwords - so it is the
            -- branch that can still see a function word, and the only one an
            -- accented needle has once the folded branch is gated off. Strictly
            -- narrower than the lemma branch, so it adds no false positive.
         OR (to_tsvector('simple', haystack)
                     @@ phraseto_tsquery('simple', needle))

            -- FOLDED. Accent-free needles only. This is 017's one-way rule:
            -- typing the accent narrows, typing without it does not, so the
            -- gate reads the NEEDLE and never the haystack.
         OR (needle = corpus.unaccent_immutable(needle)
             AND corpus.phrase_edges_survive('corpus.hungarian_surface', needle)
             AND to_tsvector('corpus.hungarian_surface', haystack)
                     @@ phraseto_tsquery('corpus.hungarian_surface', needle))
       )
$fn$;

COMMENT ON FUNCTION corpus.phrase_match(text, text) IS
    'The exact layer behind the GIN candidate filter. Guarantees: every word of '
    'the needle is present AND adjacent in order, including Hungarian function '
    'words; an accented needle matches only accented text (017), while an '
    'accent-free needle matches either; inflection is tolerated as far as the '
    'Hungarian stemmer reaches. The candidate filter may over-produce and must '
    'never under-produce (migrations/021).';

DO $verify$
DECLARE
    bad text;
BEGIN
    -- 1. Nothing outside the two defect classes changed its answer. Every row
    --    in the snapshot is a pair whose result must survive this migration.
    SELECT string_agg(format('%L / %L: was %s, now %s',
                             haystack, needle, matched,
                             corpus.phrase_match(haystack, needle)), E'\n  ')
      INTO bad
      FROM _pm_before
     WHERE corpus.phrase_match(haystack, needle) IS DISTINCT FROM matched;

    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'phrase_match changed an answer it must not:%s  %s',
            E'\n', bad;
    END IF;

    -- 2. DEFECT A is closed. The phrase must require adjacency even though
    --    'arról' is a stopword in the lemma configuration.
    IF corpus.phrase_match(
           'A kormány döntött. Erről semmit nem mondott arról a kérdésről.',
           'kormány arról') THEN
        RAISE EXCEPTION 'a trailing stopword still lets a non-adjacent pair match';
    END IF;
    IF corpus.phrase_match('arról beszélt, majd a kormány döntött',
                           'arról kormány') THEN
        RAISE EXCEPTION 'a leading stopword still lets a non-adjacent pair match';
    END IF;

    -- 3. ...without losing the phrase when the words really are adjacent. This
    --    is the risk of the change and is asserted in both spellings.
    IF NOT corpus.phrase_match('a kormány arról beszélt', 'kormány arról') THEN
        RAISE EXCEPTION 'an adjacent stopword phrase stopped matching';
    END IF;
    IF NOT corpus.phrase_match('a kormany arrol beszelt', 'kormany arrol') THEN
        RAISE EXCEPTION 'the accent-free spelling of a stopword phrase stopped matching';
    END IF;

    -- 4. An INTERIOR stopword is not collateral damage: it becomes a <N> gap
    --    and must keep working exactly as before.
    IF NOT corpus.phrase_match('a kormány arról döntött', 'kormány arról döntött') THEN
        RAISE EXCEPTION 'an interior stopword was wrongly treated as an edge one';
    END IF;
    IF NOT corpus.phrase_edges_survive('corpus.hungarian_lemma',
                                       'kormány arról döntött') THEN
        RAISE EXCEPTION 'the edge guard fires on an interior stopword';
    END IF;

    -- 5. DEFECT B is closed. An accented needle no longer reaches folded text.
    IF corpus.phrase_match('Ludovic Orban Romaniaban', 'Orbán') THEN
        RAISE EXCEPTION 'the accented needle Orbán still matches Orban';
    END IF;
    IF corpus.phrase_match('a kor jo volt', 'kór') THEN
        RAISE EXCEPTION 'the accented needle kór still matches kor';
    END IF;

    -- 6. ...while 017 stays ONE-WAY. An accent-free needle must still reach
    --    accented text, and an accented needle must still find its own.
    IF NOT corpus.phrase_match('Orbán Viktor Brüsszelben tárgyalt', 'Orban') THEN
        RAISE EXCEPTION 'the accent-free needle stopped reaching accented text';
    END IF;
    IF NOT corpus.phrase_match('a kór terjedt', 'kór') THEN
        RAISE EXCEPTION 'kór stopped finding kór';
    END IF;

    -- 7. 014's contract, restated in full, because this migration rewrote the
    --    function body it was written against.
    IF corpus.phrase_match('az apja orra a tóban', 'Orban') THEN
        RAISE EXCEPTION 'bare "orra" satisfies the phrase "Orban" again';
    END IF;
    IF NOT corpus.phrase_match('Orban Viktor Brusszelben targyalt', 'Orban') THEN
        RAISE EXCEPTION 'accent-free needle stopped matching accent-free text';
    END IF;
    IF NOT corpus.phrase_match('Orbán Viktor Brüsszelben tárgyalt', 'Orbán Viktor') THEN
        RAISE EXCEPTION 'a real phrase stopped matching';
    END IF;
    IF corpus.phrase_match('Orbán Viktor Brüsszelben tárgyalt', 'Viktor Orbán') THEN
        RAISE EXCEPTION 'a reversed phrase started matching';
    END IF;
    IF NOT corpus.phrase_match('Orbánnak üzent a miniszter', 'Orbán') THEN
        RAISE EXCEPTION 'the lemma branch stopped tolerating inflection';
    END IF;

    -- 8. A hyphenated compound still behaves, and order still decides inside it.
    IF NOT corpus.phrase_match('az orosz-ukrán háború', 'orosz-ukrán háború') THEN
        RAISE EXCEPTION 'a hyphenated phrase stopped matching';
    END IF;
    IF corpus.phrase_match('az orosz-ukrán háború', 'háború orosz-ukrán') THEN
        RAISE EXCEPTION 'a reversed hyphenated phrase started matching';
    END IF;

    -- 9. The edge guard itself, on the cases that defined it.
    IF corpus.phrase_edges_survive('corpus.hungarian_lemma', 'kormány arról') THEN
        RAISE EXCEPTION 'the edge guard missed a trailing stopword';
    END IF;
    IF corpus.phrase_edges_survive('corpus.hungarian_lemma', 'arról kormány') THEN
        RAISE EXCEPTION 'the edge guard missed a leading stopword';
    END IF;
    IF corpus.phrase_edges_survive('corpus.hungarian_lemma', 'arról') THEN
        RAISE EXCEPTION 'the edge guard passed an all-stopword needle';
    END IF;
    IF NOT corpus.phrase_edges_survive('corpus.hungarian_lemma', 'kormány kormány') THEN
        RAISE EXCEPTION 'the edge guard tripped on a repeated word';
    END IF;
    IF NOT corpus.phrase_edges_survive('corpus.hungarian_lemma', 'orosz-ukrán háború') THEN
        RAISE EXCEPTION 'the edge guard tripped on a hyphenated compound';
    END IF;

    -- 10. NULL handling is unchanged - the function is deliberately not STRICT.
    IF corpus.phrase_match(NULL, 'kormány') IS NOT FALSE THEN
        RAISE EXCEPTION 'a NULL haystack stopped returning false';
    END IF;
    IF corpus.phrase_match('a kormány döntött', NULL) IS NOT FALSE THEN
        RAISE EXCEPTION 'a NULL needle stopped returning false';
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('021')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
