-- =====================================================================
-- 022  The folded branch needs a configuration without a stopword list
-- =====================================================================
-- WHAT 021 GOT WRONG
--
-- 021 stopped a leading or trailing Hungarian function word from silently
-- turning a phrase into a one-word query, by skipping any branch whose
-- configuration had swallowed an edge token. That was right, and it stays.
--
-- What it missed is that corpus.hungarian_surface carries a stopword list of
-- its OWN. The edge guard therefore fires on the folded branch as well, and for
-- one shape of query every branch then declines, each for a locally correct
-- reason:
--
--     phrase_match('a felek között van a vita', 'kozott van')   ->  false
--
--     lemma   'van' is a stopword there, so the edge guard skips the branch
--     folded  hungarian_surface stopwords 'van' too, so it is skipped as well
--     exact   `simple` preserves accents, so 'kozott' cannot reach 'között'
--
-- An accent-free needle with a function word at either end could not reach
-- accented text at all. Before 021 it returned true, but only through the
-- degenerate query 021 exists to prevent - so 021 traded a false positive for a
-- false negative in this one shape rather than inventing it from nothing.
--
-- HOW IT WAS FOUND
--
-- Not by the in-repo suite, which asserted only that the candidate filter is a
-- superset of phrase_match. That property held even while phrase_match was
-- wrong: a wrong exact layer trivially sits inside a broad filter. It took the
-- full chain against a reference computed outside Postgres -
--
--     TRUE_REFERENCE(q,b)  =>  candidate_filter(q,b)  =>  phrase_match(q,b)
--
-- over 120 phrases drawn from the corpus itself and all 10,589 blocks. Of 878
-- reference true positives, phrase_match missed 44. Twenty-six of those are
-- this shape.
--
-- THE FIX
--
-- Give the folded branch a configuration with no stopword list: `simple` over
-- accent-folded text. corpus.unaccent_immutable already exists (007) and is
-- IMMUTABLE, so phrase_match stays IMMUTABLE and indexable.
--
-- Because `simple` drops nothing, that branch can never lose an edge token, so
-- it needs no edge guard - the guard remains where it is still needed, on the
-- lemma branch. hungarian_surface is not dropped from the schema; it still
-- builds half of corpus.search_vector and still serves ts_headline. It simply
-- stops being asked a positional question it cannot answer.
--
-- A SECOND DEFECT THIS CLOSES BY CONSTRUCTION
--
-- hungarian_surface unaccents as a DICTIONARY, which is to say AFTER parsing.
-- The parser therefore sees the accented text, and an accent inside a
-- slash-joined token breaks it apart:
--
--     to_tsvector('corpus.hungarian_surface', 'MTI/Miniszterelnöki Sajtóiroda')
--        ->  'mti/minisztereln':1 'oki':2 'sajtoiroda':3
--
-- "Miniszterelnöki" is torn into 'minisztereln' and 'oki'. Unaccenting FIRST
-- and parsing ASCII gives 'mti/miniszterelnoki':1 'sajtoiroda':2. Measured over
-- 400 real blocks, every lexeme hungarian_surface had that `simple` lacked was
-- a fragment of this kind; there was no case of real content being lost.
--
-- MEASURED, before -> after, on the same 120-phrase probe set
--
--     phrase_match false negatives      44  ->  16
--     contract cases answered wrongly     2  ->   0
--
-- The residual 16 are entirely the dash question - whether 'orosz–ukrán' with
-- an en dash is the same word as 'orosz-ukrán' with a hyphen. That is a product
-- decision, it is pinned deliberately in tests/test_search_db.py, and it is NOT
-- addressed here.
--
-- WHAT THIS DELIBERATELY DOES NOT DO
--
--   * No schema change, no index, no generated column, no reindex. Query side
--     only, exactly as 021 was.
--   * It does not touch the candidate filter. Seven candidate-side false
--     negatives remain and are index-side: a term that is a stopword in BOTH
--     configurations emits a lexeme no vector contains. Fixing that means not
--     stopwording the surface configuration, which is a reindex of the whole
--     corpus and a separate decision.
--   * It does not widen inflection. The lemma branch is untouched.
-- =====================================================================

BEGIN;

-- Snapshot first, so the verify block can prove that only the intended shape
-- moved. The two rows that MUST change are excluded by name below.
CREATE TEMP TABLE _pm_before ON COMMIT DROP AS
SELECT h AS haystack, n AS needle, corpus.phrase_match(h, n) AS matched
FROM (VALUES
        ('Orbán Viktor Brüsszelben tárgyalt', 'Orbán Viktor'),
        ('Orbán Viktor Brüsszelben tárgyalt', 'Viktor Orbán'),
        ('Orbán Viktor Brüsszelben tárgyalt', 'Orban'),
        ('Orban Viktor Brusszelben targyalt', 'Orban'),
        ('Orbánnak üzent a miniszter',        'Orbán'),
        ('az apja orra a tóban',              'Orban'),
        ('Ludovic Orban Romaniaban',          'Orbán'),
        ('a kor jo volt',                     'kór'),
        ('a kór terjedt',                     'kór'),
        ('a kor jó volt',                     'kor'),
        ('a kormány döntött',                 'kormány'),
        ('a kormány arról beszélt',           'kormány arról'),
        ('A kormány döntött. Erről semmit nem mondott arról a kérdésről.',
                                              'kormány arról'),
        ('a kormány arról döntött',           'kormány arról döntött'),
        ('az orosz-ukrán háború',             'orosz-ukrán háború'),
        ('az orosz-ukrán háború',             'háború orosz-ukrán'),
        ('az orosz–ukrán háború kitört',      'orosz-ukrán háború'),
        ('Már megint elhatalmasodott rajtad az Orbán Viktor-fóbia!!',
                                              'Orbán Viktor'),
        ('Magyarországról érkezett',          'Magyarországról'),
        ('az Európai Unió döntése',           'Európai Unió')
     ) AS v(h, n);

CREATE OR REPLACE FUNCTION corpus.phrase_match(haystack text, needle text)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $fn$
    SELECT haystack IS NOT NULL AND needle IS NOT NULL
       AND (
            -- INFLECTION. Accent-preserving and stemmed, so 'Orbánnak' answers
            -- 'Orbán'. Skipped when the needle over-stems (014) and when an edge
            -- token was swallowed by the stopword list (021).
            (corpus.lemma_phrase_safe(needle)
             AND corpus.phrase_edges_survive('corpus.hungarian_lemma', needle)
             AND to_tsvector('corpus.hungarian_lemma', haystack)
                     @@ phraseto_tsquery('corpus.hungarian_lemma', needle))

            -- EXACT, ACCENT-PRESERVING. No stemming, no stopwords, so it can
            -- see a function word, and it is what an accented needle has once
            -- the folded branch is gated off.
         OR (to_tsvector('simple', haystack)
                     @@ phraseto_tsquery('simple', needle))

            -- EXACT, ACCENT-FOLDED. 017 is one-way, so the gate reads the
            -- NEEDLE: typing the accent narrows, typing without it does not.
            -- 022 replaces corpus.hungarian_surface here with `simple` over
            -- folded text. Two reasons: hungarian_surface has a stopword list,
            -- which is what stranded this branch behind the edge guard; and it
            -- unaccents after parsing, which tears accented slash-joined tokens
            -- apart. Folding first and parsing ASCII does neither, and needs no
            -- edge guard because `simple` drops nothing.
         OR (needle = corpus.unaccent_immutable(needle)
             AND to_tsvector('simple', corpus.unaccent_immutable(haystack))
                     @@ phraseto_tsquery('simple',
                                         corpus.unaccent_immutable(needle)))
       )
$fn$;

COMMENT ON FUNCTION corpus.phrase_match(text, text) IS
    'The exact layer behind the GIN candidate filter. Guarantees: every word of '
    'the needle is present AND adjacent in order, including Hungarian function '
    'words at either end; an accented needle matches only accented text (017), '
    'while an accent-free needle matches either; inflection is tolerated as far '
    'as the Hungarian stemmer reaches. Dash variants are NOT interchangeable - '
    'that is a pinned product decision, see tests/test_search_db.py. The '
    'candidate filter may over-produce and must never under-produce '
    '(migrations/021, 022).';

DO $verify$
DECLARE
    bad text;
BEGIN
    -- 1. Only the intended shape moved. Everything in the snapshot must answer
    --    exactly as it did before this migration ran.
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

    -- 2. THE FIX. An accent-free needle with an edge function word now reaches
    --    accented text.
    IF NOT corpus.phrase_match('a felek között van a vita', 'kozott van') THEN
        RAISE EXCEPTION 'an accent-free needle still cannot cross a stopword';
    END IF;
    IF NOT corpus.phrase_match('mindenki számára elérhető', 'mindenki szamara') THEN
        RAISE EXCEPTION 'the szamara case is still unreachable';
    END IF;

    -- 3. ...and it is still a PHRASE. Fixing reach must not cost adjacency.
    IF corpus.phrase_match('a felek döntöttek. Van egy másik vita is.',
                           'kozott van') THEN
        RAISE EXCEPTION 'the folded branch stopped requiring adjacency';
    END IF;
    IF corpus.phrase_match('A kormany dontott. Nem mondott arrol semmit.',
                           'kormany arrol') THEN
        RAISE EXCEPTION 'the accent-free stopword phrase lost its adjacency';
    END IF;

    -- 4. 017 stays one-way in BOTH directions.
    IF corpus.phrase_match('Ludovic Orban Romaniaban', 'Orbán') THEN
        RAISE EXCEPTION 'an accented needle reached folded text again';
    END IF;
    IF corpus.phrase_match('a kor jo volt', 'kór') THEN
        RAISE EXCEPTION 'kór matched kor again';
    END IF;
    IF NOT corpus.phrase_match('Orbán Viktor Brüsszelben tárgyalt', 'Orban') THEN
        RAISE EXCEPTION 'the accent-free needle stopped reaching accented text';
    END IF;

    -- 5. 014's contract, restated because the body was rewritten again.
    IF corpus.phrase_match('az apja orra a tóban', 'Orban') THEN
        RAISE EXCEPTION 'bare "orra" satisfies the phrase "Orban" again';
    END IF;
    IF NOT corpus.phrase_match('Orbánnak üzent a miniszter', 'Orbán') THEN
        RAISE EXCEPTION 'the lemma branch stopped tolerating inflection';
    END IF;
    IF corpus.phrase_match('Orbán Viktor Brüsszelben tárgyalt', 'Viktor Orbán') THEN
        RAISE EXCEPTION 'a reversed phrase started matching';
    END IF;

    -- 6. 021's two defects stay closed.
    IF corpus.phrase_match(
           'A kormány döntött. Erről semmit nem mondott arról a kérdésről.',
           'kormány arról') THEN
        RAISE EXCEPTION '021 defect A reopened';
    END IF;
    IF NOT corpus.phrase_match('a kormány arról döntött',
                               'kormány arról döntött') THEN
        RAISE EXCEPTION 'an interior stopword broke again';
    END IF;

    -- 7. The tokenisation decisions stay where the team pinned them. These are
    --    product choices, not accidents, and this migration must not move them.
    IF corpus.phrase_match('az orosz–ukrán háború kitört', 'orosz-ukrán háború') THEN
        RAISE EXCEPTION 'an en dash silently became a hyphen';
    END IF;
    IF corpus.phrase_match('Már megint elhatalmasodott rajtad az Orbán Viktor-fóbia!!',
                           'Orbán Viktor') THEN
        RAISE EXCEPTION 'a phrase silently became findable inside a compound';
    END IF;

    -- 8. The parser no longer tears an accented slash-joined token apart.
    IF NOT corpus.phrase_match('Fotó: MTI/Miniszterelnöki Sajtóiroda',
                               'mti/miniszterelnoki sajtoiroda') THEN
        RAISE EXCEPTION 'an accented slash token is still being split';
    END IF;

    -- 9. NULL handling is unchanged - deliberately not STRICT.
    IF corpus.phrase_match(NULL, 'kormány') IS NOT FALSE
       OR corpus.phrase_match('a kormány döntött', NULL) IS NOT FALSE THEN
        RAISE EXCEPTION 'NULL handling changed';
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('022')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
