-- =====================================================================
-- 012  Make a query's terms separately addressable, so an AND can span
--      the whole document instead of one paragraph
-- =====================================================================
-- THE DEFECT (query side; this migration only supplies the tool)
--
-- scripts/search.py matches an article with
--
--     a.search_tsv @@ q.tsq
--     OR EXISTS (block  b WHERE b.text_tsv    @@ q.tsq)
--     OR EXISTS (image  i WHERE i.caption_tsv @@ q.tsq)
--
-- where q.tsq is the WHOLE query. Every term of a multi-word AND therefore has
-- to be satisfied INSIDE ONE VECTOR - one block, or the metadata, or one image
-- caption. An article whose terms are spread across paragraphs matches nothing,
-- although the document plainly contains all of them.
--
-- Measured on the 1,008-article evaluation corpus, article #158:
--
--     'orosz-ukrán háború'  ->  whole query: metadata 0, blocks 0   (dropped)
--        term 1  orosz-ukrán    metadata no,   blocks [4, 9, 10]
--        term 2  háború         metadata yes,  blocks [1, 5, 6, 14]
--
-- The article contains both terms. No single vector contains both: the blocks
-- carrying term 1 do not carry term 2, and the metadata carries only term 2.
-- So the article is not returned.
--
-- Terms split on whitespace ONLY, so the hyphenated compound stays ONE term -
-- its own alternation already covers orosz and ukrán. Read the split off
-- corpus.search_terms() rather than assuming it; a word is not a term. This is 12 of the 13 standing recall misses. On the probe set:
--
--     query                  shipped  document-level  misses -> misses
--     orosz-ukrán háború          41              56       4 -> 0
--     ukrajnai fejlesztés          4              11       6 -> 0
--     migrációs nyomás             8              10       3 -> 1
--
-- 'ukrajnai fejlesztés' returns 4 of 11 true matches - a 64% recall loss on an
-- ordinary two-word query. The queries that survive are the ones whose terms
-- are ADJACENT (Orbán Viktor, Donald Trump), because those land in one title;
-- the ones that break are topical, which is the analytic use this corpus is
-- for. It also gets worse as articles get longer.
--
-- WHY THE DESIGN NOTE DID NOT CATCH IT
--
-- docs/postgres-schema.md D.2 defends having no whole-body vector and names
-- exactly one cost: "whole-document ranking (ts_rank over the full body) is not
-- available". That is a RANKING caveat. The recall cost is not mentioned, and
-- it is the larger one.
--
-- WHAT THIS MIGRATION ADDS
--
-- corpus.search_query already walks the query term by term and ANDs the
-- per-term alternations together. It just never exposed the terms, so the
-- caller could only ever hand the whole tsquery to one vector.
--
--     corpus.term_query(text)   -> tsquery     one term's alternation
--     corpus.search_terms(text) -> tsquery[]   one entry per term
--
-- and corpus.search_query is REDEFINED as the AND-fold of corpus.search_terms.
-- That direction matters: the terms the caller ANDs across vectors are then the
-- same terms search_query ANDs inside one, BY CONSTRUCTION, and cannot drift
-- apart later. The verification below captures search_query's output for 26
-- probes BEFORE the rewrite and requires it to be byte-identical after, so this
-- cannot quietly change what anything already matches.
--
-- Query side only. No table is rewritten, no GIN index is rebuilt, and
-- corpus.search_vector and the three STORED generated columns are untouched -
-- the same shape as 010 and 011, for the same reason: it has to be applicable
-- to 4.2M articles.
-- =====================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------
-- 0. Refuse to run if this became a data migration
-- ---------------------------------------------------------------------
-- Same guard as 010. A STORED generated column that referenced search_query
-- would be silently desynchronised from its own text by redefining it, and a
-- query-side change would have become a table rewrite without anyone saying so.
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
       AND pg_get_expr(d.adbin, d.adrelid) ILIKE '%search_query%';

    IF dependent IS NOT NULL THEN
        RAISE EXCEPTION
            'search_query is referenced by STORED generated column(s): %. '
            'Redefining it would desynchronise them; this is not a query-side '
            'change any more.', dependent;
    END IF;
END
$guard$;

-- ---------------------------------------------------------------------
-- 1. Record what search_query answers today
-- ---------------------------------------------------------------------
-- Captured BEFORE the rewrite. Section 4 requires every one of these to come
-- back byte-identical, which is what makes "this only exposes the terms, it
-- does not change matching" a checked claim rather than an intention.
CREATE TEMP TABLE _sq_before ON COMMIT DROP AS
SELECT p AS probe, corpus.search_query(p)::text AS expected
FROM unnest(ARRAY[
        'Orbán', 'Orban', 'Orbánt', 'Orbánnak', 'Orbánnal',
        'Magyarország', 'magyarorszag', 'Magyarországról', 'magyarországi',
        'idén', 'Simon', 'USA', 'kik', 'koronavírus', 'baloldal',
        'Orbán Viktor', 'Szijjártó Péter', 'Soros György', 'Donald Trump',
        'orosz-ukrán háború', 'ukrajnai fejlesztés', 'migrációs nyomás',
        'Orban' || chr(160) || 'Viktor',
        '  Orban ' || chr(9) || ' Viktor  ',
        '', '   '
     ]) AS p;

-- ---------------------------------------------------------------------
-- 2. One term's expansion, lifted out of search_query unchanged
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION corpus.term_query(term text)
RETURNS tsquery
LANGUAGE plpgsql
IMMUTABLE PARALLEL SAFE STRICT
AS $fn$
DECLARE
    lem  text[];
    sur  text[];
    alts text[];
BEGIN
    IF term = '' THEN
        RETURN ''::tsquery;
    END IF;

    -- The 010 guard, verbatim: drop a lemma of 1-2 characters unless some
    -- spelling of the term really produces it on the surface. A lemma the
    -- stemmer INVENTED ('or' for Orban, 'id' for idén) is a collision magnet;
    -- a genuinely short lemma is kept.
    lem := coalesce(corpus.lemma_lexemes(term),   ARRAY[]::text[]);
    sur := coalesce(corpus.surface_lexemes(term), ARRAY[]::text[]);

    SELECT array_agg(DISTINCT a ORDER BY a) INTO alts
    FROM unnest(
             ARRAY(SELECT l FROM unnest(lem) AS l
                    WHERE length(l) > 2 OR l = ANY(sur))
          || sur
         ) AS a;

    IF alts IS NULL OR array_length(alts, 1) IS NULL THEN
        RETURN ''::tsquery;          -- a stopword expands to nothing
    END IF;

    RETURN to_tsquery('simple',
               '(' || array_to_string(
                   ARRAY(SELECT quote_literal(a) FROM unnest(alts) AS a), ' | ')
            || ')');
END
$fn$;

COMMENT ON FUNCTION corpus.term_query(text) IS
    'The (lemma | surface) alternation for ONE query term, with the 010 guard '
    'applied. corpus.search_query ANDs these together; a caller doing '
    'document-level matching ANDs them across vectors instead.';

-- ---------------------------------------------------------------------
-- 3. The terms of a query, separately addressable
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION corpus.search_terms(q text)
RETURNS tsquery[]
LANGUAGE sql
IMMUTABLE PARALLEL SAFE STRICT
AS $fn$
    -- Split on corpus.whitespace_chars(), never on backslash-s: Postgres's is
    -- ASCII-only, and a NO-BREAK SPACE between two words would otherwise make
    -- them one term (011).
    SELECT coalesce(
             array_agg(tq ORDER BY ord)
               FILTER (WHERE tq <> ''::tsquery),
             ARRAY[]::tsquery[])
    FROM unnest(string_to_array(corpus.collapse_whitespace(q), ' '))
         WITH ORDINALITY AS s(term, ord),
    LATERAL (SELECT corpus.term_query(s.term) AS tq) t;
$fn$;

COMMENT ON FUNCTION corpus.search_terms(text) IS
    'One tsquery per query term, in order, stopwords dropped. The AND of these '
    'IS corpus.search_query - see 012. Hand them to separate vectors to match a '
    'document rather than a single paragraph.';

-- ---------------------------------------------------------------------
-- 4. search_query becomes the AND-fold of search_terms
-- ---------------------------------------------------------------------
-- Defining it this way round is the point of the migration. Two functions that
-- each walked the query independently would be free to disagree about what a
-- term is; this way there is one tokenizer and one expansion, and the AND is
-- the only thing that differs between matching a paragraph and matching a
-- document.
CREATE OR REPLACE FUNCTION corpus.search_query(q text)
RETURNS tsquery
LANGUAGE plpgsql
IMMUTABLE PARALLEL SAFE STRICT
AS $fn$
DECLARE
    acc tsquery := NULL;
    tq  tsquery;
BEGIN
    FOREACH tq IN ARRAY corpus.search_terms(q) LOOP
        acc := CASE WHEN acc IS NULL THEN tq ELSE acc && tq END;
    END LOOP;
    RETURN coalesce(acc, ''::tsquery);
END
$fn$;

COMMENT ON FUNCTION corpus.search_query(text) IS
    'Expands a user query into (lemma | surface) alternatives per term, ANDed '
    'across terms, dropping a lemma of 1-2 characters that no spelling of the '
    'term produces on the surface (010). Terms are split on '
    'corpus.whitespace_chars(), not on Postgres backslash-s, so a NO-BREAK '
    'SPACE does not silently turn the AND into an OR (011). Since 012 this is '
    'literally the AND-fold of corpus.search_terms(), so the two cannot drift.';

-- ---------------------------------------------------------------------
-- 5. Prove it
-- ---------------------------------------------------------------------
DO $verify$
DECLARE
    bad      text;
    n_terms  int;
    baseline tsquery := corpus.search_query('Orban Viktor');
    cp       int;
BEGIN
    -- 5a. THE central assertion: matching is unchanged. Every probe captured
    -- before the rewrite must come back byte-identical.
    SELECT string_agg(format('%L: was %s, now %s',
                             probe, expected, corpus.search_query(probe)::text),
                      E'\n  ')
      INTO bad
      FROM _sq_before
     WHERE corpus.search_query(probe)::text IS DISTINCT FROM expected;

    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'search_query changed its answer:%s  %s', E'\n', bad;
    END IF;

    -- 5b. The invariant the whole fix rests on: for ONE vector, matching the
    -- query is exactly matching every term. Asserting this rather than
    -- re-deriving the AND-fold matters - search_query IS the fold now, so
    -- comparing it to a fold would be a tautology. This compares MATCHING,
    -- which is what the search layer actually does with the terms.
    SELECT string_agg(format('%L vs %L', t.txt, t.q), ', ') INTO bad
      FROM (VALUES
              ('Orbán Viktor Brüsszelben tárgyalt', 'Orbán Viktor'),
              ('Orbán Viktor Brüsszelben tárgyalt', 'Orbán Brüsszel'),
              ('Orbán Viktor Brüsszelben tárgyalt', 'Orbán Kairó'),
              ('orosz katonák a határon',           'orosz háború'),
              ('ukrajnai fejlesztésekre költenek',  'ukrajnai fejlesztés'),
              ('a migrációs nyomás nőtt',           'migrációs nyomás')
           ) AS t(txt, q)
     WHERE (corpus.search_vector(t.txt) @@ corpus.search_query(t.q))
           IS DISTINCT FROM
           (SELECT bool_and(corpus.search_vector(t.txt) @@ tq)
              FROM unnest(corpus.search_terms(t.q)) AS tq);

    IF bad IS NOT NULL THEN
        RAISE EXCEPTION
            'matching the query is not the same as matching every term: %', bad;
    END IF;

    -- 5c. One entry per surviving term, in query order.
    SELECT cardinality(corpus.search_terms('orosz-ukrán háború')) INTO n_terms;
    IF n_terms <> 2 THEN
        RAISE EXCEPTION 'expected 2 terms for a hyphenated word plus one, got %',
            n_terms;
    END IF;
    IF cardinality(corpus.search_terms('Orbán Viktor')) <> 2 THEN
        RAISE EXCEPTION 'expected 2 terms for a two-word query';
    END IF;

    -- 5d. A stopword contributes no term at all, rather than an empty one that
    -- would make a document-level AND unsatisfiable. 'ki' is the stopword here;
    -- 'kik' is NOT one - it expands to 'kik' and matches 2 articles - which is
    -- exactly the confusion this assertion exists to prevent.
    IF cardinality(corpus.search_terms('ki')) <> 0 THEN
        RAISE EXCEPTION 'a stopword must contribute no term, got %',
            corpus.search_terms('ki')::text;
    END IF;
    IF cardinality(corpus.search_terms('Orbán ki Viktor')) <> 2 THEN
        RAISE EXCEPTION 'a stopword between two terms must simply vanish, got %',
            corpus.search_terms('Orbán ki Viktor')::text;
    END IF;
    IF cardinality(corpus.search_terms('kik')) <> 1 THEN
        RAISE EXCEPTION 'kik is not a stopword and must survive as one term';
    END IF;

    -- 5e. An empty query yields no terms. A document-level AND over zero terms
    -- must therefore match NOTHING, which is what search_query already answers.
    IF cardinality(corpus.search_terms('')) <> 0
       OR cardinality(corpus.search_terms('   ')) <> 0 THEN
        RAISE EXCEPTION 'an empty query must contribute no terms';
    END IF;

    -- 5f. 011's property survives the rewrite: every character of the class
    -- separates two words exactly as a plain space does. Re-checked here
    -- because the splitting moved into a different function.
    FOREACH cp IN ARRAY ARRAY(
        SELECT ascii(c)
        FROM regexp_split_to_table(corpus.whitespace_chars(), '') AS c
        WHERE c <> ''
    ) LOOP
        IF corpus.search_query('Orban' || chr(cp) || 'Viktor')
           IS DISTINCT FROM baseline THEN
            RAISE EXCEPTION 'U+% no longer separates terms', upper(to_hex(cp));
        END IF;
        IF cardinality(corpus.search_terms('Orban' || chr(cp) || 'Viktor')) <> 2 THEN
            RAISE EXCEPTION 'U+% does not split into 2 terms', upper(to_hex(cp));
        END IF;
    END LOOP;

    -- 5g. The defect, restated as a passing test. One vector holding only part
    -- of the query matches the corresponding TERM but not the whole query -
    -- which is precisely why an article split across two vectors was dropped,
    -- and why the caller can now put those halves back together.
    IF NOT corpus.search_vector('orosz katonák a határon')
           @@ (corpus.search_terms('orosz háború'))[1] THEN
        RAISE EXCEPTION 'term 1 should match a vector that contains only it';
    END IF;
    IF NOT corpus.search_vector('elhúzódó háború')
           @@ (corpus.search_terms('orosz háború'))[2] THEN
        RAISE EXCEPTION 'term 2 should match a vector that contains only it';
    END IF;
    IF corpus.search_vector('orosz katonák a határon')
       @@ corpus.search_query('orosz háború') THEN
        RAISE EXCEPTION 'the whole query must NOT match a vector missing a term';
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('012')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
