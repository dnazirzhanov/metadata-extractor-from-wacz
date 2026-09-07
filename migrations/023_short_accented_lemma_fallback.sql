-- =====================================================================
-- 023  A word whose lemma is two letters long must still find itself
-- =====================================================================
-- THE DEFECT
--
-- 'két' - "two", one of the commonest words in Hungarian - could not find
-- itself:
--
--     corpus.search_vector('két évtizede')
--         ->  'evtized':2 'evtizede':4 'ke':1 'ket':3 'ké':5 'évtized':6
--     corpus.search_query('két')
--         ->  'két'
--     ...   @@ ...                                            ->  FALSE
--
-- The index holds the accent-preserving lemma 'ké'. The query does not ask for
-- it, because 010 refuses to put a lemma of one or two characters into a query -
-- such a lemma is a collision magnet, which is exactly the right rule and is why
-- 'Orban' no longer over-stems to 'or'. With the lemma discarded, the accented
-- branch falls through to the raw spelling 'két', and the index never stores a
-- raw accented form: its accent-preserving component IS the lemma. Nothing
-- matches.
--
-- 020 already fixed the neighbouring case, where the stopword list empties the
-- accented lemma branch, by falling back to the accent-folded surface form. Its
-- condition asks whether corpus.accented_lexemes() returned nothing. For 'két'
-- it returns {ké} - not nothing - and 010's guard discards it a few lines
-- later, so the fallback never fires. 020 tested the wrong moment.
--
-- HOW IT WAS FOUND
--
-- Building a demonstration of full-paragraph search: giving the engine a whole
-- paragraph from the corpus and asking it to find where it came from. The first
-- paragraph tried returned nothing. corpus.search_query ANDs every term of the
-- paragraph together, so ONE unmatchable word is enough to make a whole
-- paragraph unfindable - and 'két' appears in a great many paragraphs.
--
-- THE FIX
--
-- Widen 020's condition from "the accented lemma branch was EMPTY" to "the
-- accented lemma branch CONTRIBUTED NOTHING", which covers both the stopword
-- case 020 handled and the short-lemma case it missed. One boolean.
--
-- WHAT IT COSTS, STATED PLAINLY
--
-- A word whose accented lemma is one or two characters becomes accent-blind:
-- 'két' will now also reach text spelled 'ket'. That is a real, if small,
-- weakening of 017's rule that typing the accent narrows the search. It is
-- taken deliberately, because the alternative is what we had - the word matching
-- NOTHING AT ALL, including itself. A slightly wider answer beats an empty one.
--
-- The exposure is bounded by construction: it applies only to words whose
-- accented lemma is at most two characters, which 010 had already declared
-- unusable as a query term. Words with a longer lemma - 'kór', 'párt', 'Orbán'
-- - are untouched and stay accent-sensitive.
--
-- Query side only. No DDL, no index, no generated column, no reindex.
-- =====================================================================

BEGIN;

-- Snapshot first: everything here must answer exactly as it did before, and the
-- verify block proves it. Only 'két' and its family are expected to change.
CREATE TEMP TABLE _q_before ON COMMIT DROP AS
SELECT t AS term, corpus.search_query(t)::text AS expected
FROM unnest(ARRAY['kor', 'kór', 'kör', 'part', 'párt', 'Orban', 'Orbán',
                  'kormany', 'kormány', 'magyarorszag', 'Magyarország',
                  'Viktor', 'háború', 'koronavírus', 'arról', 'ezért',
                  'kormanyrol', 'kormányról', 'ügy']) AS t;
-- 'idén' and 'két' are deliberately absent: both have a two-character accented
-- lemma and are exactly what this migration changes. They are asserted by name
-- below instead.

CREATE OR REPLACE FUNCTION corpus.accented_query(t text)
RETURNS tsquery
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE STRICT
AS $function$
DECLARE
    sur        text[];
    sides      text[] := ARRAY[]::text[];
    side       text;
    lx         text[];
    -- 023. Whether the LEMMA branch actually put anything into the query. This
    -- is the moment 020 needed to test and did not: it asked whether
    -- accented_lexemes() was empty, which is true for a stopword but false for
    -- 'két', whose lemma {ké} is discarded a few lines below by 010's guard.
    lemma_used boolean := false;
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

        IF side = 'lemma' THEN
            lemma_used := true;
        END IF;

        -- 015's compound prefix, for the same reason it applies elsewhere.
        sides := sides || ('(' || array_to_string(
            ARRAY(SELECT quote_literal(x)
                         || CASE WHEN length(x) >= corpus.prefix_min_length()
                                 THEN ':*' ELSE '' END
                    FROM unnest(lx) AS x),
            ' & ') || ')');
    END LOOP;

    -- 020, widened by 023. The index derives its accent-preserving lexemes from
    -- the lemma branch, so when that branch contributes nothing the index holds
    -- only the folded surface form for this word - which is therefore the only
    -- thing a query can match. Two ways the branch can contribute nothing: the
    -- stopword list emptied it (020), or 010's guard discarded a lemma of one or
    -- two characters (023). Additive: the raw side above is left in place, it
    -- simply has nothing to match.
    IF NOT lemma_used AND array_length(sur, 1) IS NOT NULL THEN
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
DECLARE
    bad text;
BEGIN
    -- 1. Nothing outside the short-lemma family changed its query.
    SELECT string_agg(format('%L: was %s, now %s',
                             term, expected, corpus.search_query(term)::text),
                      E'\n  ')
      INTO bad
      FROM _q_before
     WHERE corpus.search_query(term)::text IS DISTINCT FROM expected;

    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'search_query changed an answer it must not:%s  %s',
            E'\n', bad;
    END IF;

    -- 2. THE DEFECT. A word with a two-character lemma finds itself.
    IF NOT corpus.search_vector('két évtizede') @@ corpus.search_query('két') THEN
        RAISE EXCEPTION 'két still cannot find itself';
    END IF;

    -- 3. ...and a whole paragraph containing it is findable again. This is the
    --    case that exposed the defect: search_query ANDs every term, so one
    --    unmatchable word makes an entire paragraph unfindable.
    IF NOT corpus.search_vector(
             'Emlékeztettek arra, hogy a Lungo Drom több mint két évtizede '
             'szövetségese a Fidesznek és az Orbán-kormánynak.')
           @@ corpus.search_query(
             'Emlékeztettek arra, hogy a Lungo Drom több mint két évtizede '
             'szövetségese a Fidesznek és az Orbán-kormánynak.') THEN
        RAISE EXCEPTION 'a paragraph containing két still cannot find itself';
    END IF;

    -- 3b. 'idén' has the same defect and the same fix - a two-character lemma
    --     'id' that 010 discards. Both went from matching NOTHING to matching
    --     52 and 24 articles respectively on the evaluation corpus.
    IF NOT corpus.search_vector('idén nyáron') @@ corpus.search_query('idén') THEN
        RAISE EXCEPTION 'idén still cannot find itself';
    END IF;

    -- 3c. THE RISK THIS TAKES, pinned. 015 chose a prefix threshold of 7 partly
    --     so that 'idén' would not reach 'identitás'. The folded alternative
    --     'iden' is four characters and therefore carries no prefix, so the
    --     separation survives - but it is the assertion to watch if anyone ever
    --     lowers corpus.prefix_min_length().
    IF corpus.search_vector('az identitás kérdése')
       @@ corpus.search_query('idén') THEN
        RAISE EXCEPTION 'idén reached identitás - 015 threshold defeated by 023';
    END IF;

    -- 4. 017 survives where it matters. A word with a usable accented lemma is
    --    untouched and stays accent-sensitive.
    IF corpus.search_vector('a kor jó volt') @@ corpus.search_query('kór') THEN
        RAISE EXCEPTION 'kór matched kor - 023 widened the fallback too far';
    END IF;
    IF corpus.search_vector('a part menten') @@ corpus.search_query('párt') THEN
        RAISE EXCEPTION 'párt matched part - 023 widened the fallback too far';
    END IF;

    -- 5. ...and the one-way direction still holds.
    IF NOT corpus.search_vector('a kór terjedt') @@ corpus.search_query('kor') THEN
        RAISE EXCEPTION 'kor stopped reaching kór';
    END IF;

    -- 6. 010's guard itself is untouched: the short lemma is still kept OUT of
    --    the query. 023 adds a folded alternative beside it, it does not
    --    readmit the collision magnet.
    IF corpus.search_query('két')::text LIKE '%''ké''%' THEN
        RAISE EXCEPTION 'the two-character lemma was readmitted to the query';
    END IF;
    IF corpus.search_query('Orban')::text LIKE '%''or''%' THEN
        RAISE EXCEPTION '010 over-stemming guard regressed';
    END IF;

    -- 7. 020's own case still works - the stopword route into the same fallback.
    IF NOT corpus.search_vector('Beszélt arról a kérdésről.')
           @@ corpus.search_query('arról') THEN
        RAISE EXCEPTION '020 regressed: an accented stopword stopped matching';
    END IF;

    -- 8. 019 and 022 still stand.
    IF NOT corpus.search_vector('döntött a kormányról a testület')
           @@ corpus.search_query('kormanyrol') THEN
        RAISE EXCEPTION '019 regressed';
    END IF;
    IF NOT corpus.phrase_match('a felek között van a vita', 'kozott van') THEN
        RAISE EXCEPTION '022 regressed';
    END IF;

    -- 9. Phrase semantics are unaffected - this migration touches the query
    --    side only, and phrase_match computes its own vectors.
    IF corpus.phrase_match('Orbán Viktor Brüsszelben tárgyalt', 'Viktor Orbán') THEN
        RAISE EXCEPTION 'phrase order stopped mattering';
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('023')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
