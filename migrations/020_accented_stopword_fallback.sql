-- =====================================================================
-- 020  A stopword the reader typed with its accent still has to match
-- =====================================================================
-- THE DEFECT
--
-- The Hungarian stopword list is spelled WITH accents, and it is applied in the
-- lemma configuration only. So the two spellings of one function word disagree
-- about whether the word exists at all:
--
--     corpus.accented_lexemes('arról')  ->  {}          stopworded
--     corpus.lemma_lexemes('arrol')     ->  {arrol}     not in the list
--
-- The index side resolves that quietly. corpus.search_vector emits an
-- accent-preserving lexeme only through the lemma branch, so for a stopword it
-- emits nothing accented and the document keeps only the folded surface form:
--
--     corpus.search_vector('Beszélt arról a kérdésről.')
--       ->  'arrol':4 'beszel':1 'beszelt':3 'beszél':7 'kerdes':2
--           'kerdesrol':6 'kérdés':10
--
-- 'beszél' and 'kérdés' kept their accented forms. 'arról' did not - only
-- 'arrol' is there. The QUERY side does not follow that rule: accented_query
-- falls through to corpus.accented_raw_lexemes, which does not consult the
-- stopword list and happily returns {arról}. So the query asks for a lexeme the
-- index never writes, and the term matches nothing:
--
--     arról   0 articles        arrol   208
--     ezért   0                 ezert   195
--     azért   0                 azert   135
--
-- THE BLAST RADIUS IS NOT THE SINGLE-WORD QUERY
--
-- 012 made every term of a multi-word query have to appear SOMEWHERE in the
-- article. A term that matches nothing therefore makes the whole conjunction
-- unsatisfiable, and one function word annihilates the query around it:
--
--     kormány                290 articles
--     kormány arról            0            <- adding a common word destroys it
--     kormany arrol           97
--     Orbán beszélt           15
--     Orbán arról beszélt      0
--     Orban arrol beszelt     11
--
-- Typing correct, fully accented Hungarian - which is what a Hungarian
-- journalist does - returns nothing the moment the sentence contains one of
-- these words. Typing it sloppily works. That is the exact inversion of the
-- accent-insensitivity this schema promised, and it is worse than 019's cliff
-- because it takes the other terms down with it.
--
-- WHAT THIS MIGRATION DOES
--
-- Query side only. No DDL, no change to corpus.search_vector, nothing
-- reindexed, nothing re-ingested.
--
-- When the accented LEMMA branch comes back empty - which is precisely what
-- being stopworded looks like - the accent-preserving lexeme cannot be in the
-- index either, because the index derives it from that same branch. The folded
-- surface form IS in the index, so it is ORed in as an additional alternative.
--
-- WHY THIS DOES NOT UNDO 017
--
-- 017 made an accented query accent-SENSITIVE so that 'kór' stops matching
-- 'kor'. That rule is untouched here, structurally rather than by care: the
-- fallback fires only when accented_lexemes is EMPTY, and a word that has an
-- accented lemma never takes it.
--
--     corpus.accented_lexemes('kór')  ->  {kór}   non-empty, no fallback
--     corpus.accented_lexemes('szól') ->  {szól}  non-empty, no fallback
--
-- The words that do take it are the ones with no accented form in the index at
-- all, so there is no accented/unaccented pair left for the fallback to
-- collapse. 'kór' and 'kor' remain distinguishable; 'arról' has nothing to be
-- distinguished from.
--
-- WHY NOT SIMPLY DROP THE TERM, WHICH IS WHAT FULL-TEXT SEARCH USUALLY DOES
--
-- Dropping stopwords from the query would also cure the annihilation, and would
-- give 'kormány arról' the 290 articles of 'kormány' alone. It was rejected:
-- this corpus INDEXES its stopwords on the surface side - 'arrol' is a lexeme
-- in the vector above - so they are searchable, and the accent-free spelling
-- already searches them. Dropping would mean silently ignoring a word the
-- reader typed, and would leave the two spellings disagreeing in the other
-- direction: 'kormany arrol' 97 against 'kormány arról' 290. The point of the
-- migration is that the two spellings agree.
--
-- WHY NOT FIX THE INDEX INSTEAD
--
-- Making search_vector emit the raw accented form for stopwords would be the
-- deeper fix and would need every generated tsvector column recomputed. The
-- evaluation corpus is 1,008 articles; the archive behind it is over four
-- million. The information is not lost - the folded form is indexed - so a
-- query-side rule recovers it at no storage cost. If the accented form is ever
-- wanted in the index for another reason, this migration does not stand in the
-- way.
-- =====================================================================

BEGIN;

CREATE OR REPLACE FUNCTION corpus.accented_query(t text)
RETURNS tsquery
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT
AS $function$
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

    -- 020. An empty accented lemma branch means the stopword list swallowed the
    -- word. The index builds its accent-preserving lexemes from that same
    -- branch, so it holds only the folded surface form for this word - which is
    -- therefore the only thing a query can match. Additive: the raw side above
    -- is left in place, it simply has nothing to match.
    IF coalesce(array_length(corpus.accented_lexemes(t), 1), 0) = 0
       AND array_length(sur, 1) IS NOT NULL THEN
        sides := sides || ('(' || array_to_string(
            ARRAY(SELECT quote_literal(x)
                         || CASE WHEN length(x) >= corpus.prefix_min_length()
                                 THEN ':*' ELSE '' END
                    FROM (SELECT DISTINCT s FROM unnest(sur) AS s ORDER BY s) AS d(x)),
            ' & ') || ')');
    END IF;

    IF array_length(sides, 1) IS NULL THEN
        RETURN ''::tsquery;
    END IF;

    -- De-duplicate: for most terms the two sides are identical, and emitting
    -- `X | X` would be noise in every EXPLAIN and every debugging session.
    RETURN (array_to_string(ARRAY(SELECT DISTINCT s FROM unnest(sides) AS s), ' | '))::tsquery;
END
$function$;

DO $verify$
BEGIN
    -- 1. The defect: an accented stopword reaches the document that contains it.
    IF NOT corpus.search_vector('Beszélt arról a kérdésről.')
           @@ corpus.search_query('arról') THEN
        RAISE EXCEPTION 'arról still does not reach a document containing arról';
    END IF;
    IF NOT corpus.search_vector('Ezért döntött így a testület.')
           @@ corpus.search_query('ezért') THEN
        RAISE EXCEPTION 'ezért still does not reach a document containing ezért';
    END IF;

    -- 2. The blast radius: one function word no longer annihilates the
    -- conjunction 012 built around it.
    IF NOT corpus.search_vector('A kormány beszélt arról a kérdésről.')
           @@ corpus.search_query('kormány arról') THEN
        RAISE EXCEPTION 'an accented stopword still destroys the query around it';
    END IF;

    -- 3. The two spellings now agree, which is the whole point.
    IF NOT corpus.search_vector('Beszélt arról a kérdésről.')
           @@ corpus.search_query('arrol') THEN
        RAISE EXCEPTION 'the accent-free spelling regressed';
    END IF;

    -- 4. 017 is untouched: a word WITH an accented lemma never takes the
    -- fallback, so accent-sensitivity survives where it was meant to.
    IF corpus.search_vector('a kor jó volt')
       @@ corpus.search_query('kór') THEN
        RAISE EXCEPTION 'kór matched kor - 020 collapsed the accent distinction';
    END IF;
    -- The other direction is asserted to STAY true. 017 is one-way on purpose:
    -- typing the accent narrows, typing without it does not. 'kor' reaching
    -- 'kór' is the accent-insensitivity the schema promises the reader who has
    -- no Hungarian keyboard, and 020 must not cost them it.
    IF NOT corpus.search_vector('a kór terjedt')
           @@ corpus.search_query('kor') THEN
        RAISE EXCEPTION 'kor stopped reaching kór - 020 broke accent-insensitivity';
    END IF;

    -- 5. The fallback is conditional, not universal. A term with an accented
    -- lemma must not acquire the folded side.
    IF corpus.accented_query('kór')::text LIKE '%''kor''%' THEN
        RAISE EXCEPTION 'a non-stopword acquired the folded fallback: %',
            corpus.accented_query('kór')::text;
    END IF;
    IF corpus.accented_query('szól')::text LIKE '%''szol''%' THEN
        RAISE EXCEPTION 'szól acquired the folded fallback';
    END IF;

    -- 6. 015's AND across terms still bites - the new side is an alternative
    -- within one term, not a way out of the conjunction.
    IF corpus.search_vector('kormányzati energiapolitika')
       @@ corpus.search_query('arról büdzsé') THEN
        RAISE EXCEPTION 'the AND across terms stopped biting under 020';
    END IF;

    -- 7. 019 still stands.
    IF NOT corpus.search_vector('döntött a kormányról a testület')
           @@ corpus.search_query('kormanyrol') THEN
        RAISE EXCEPTION '020 broke 019';
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('020')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
