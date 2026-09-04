-- =====================================================================
-- 013  A hyphenated term means ALL of its pieces, not any one of them
-- =====================================================================
-- THE DEFECT
--
-- corpus.term_query takes the lexemes a term produces and ORs them together.
-- For a one-word term that is exactly right: the lemma and the surface form are
-- two SPELLINGS of one word, and either will do. For a hyphenated word it is
-- badly wrong, because the parser emits the compound AND its parts:
--
--     corpus.lemma_lexemes('európa-bajnokság')
--         -> {bajnoksag, europ, europa, europa-bajnoksag}
--
-- ORing those makes 'europa' alone satisfy the term. Measured on the
-- 1,008-article evaluation corpus:
--
--     term                today   after this migration   articles tagged with it
--     európa-bajnokság      263                      7                        1
--     Közép-Ázsia            73                      1                        1
--     CT-vizsgálat           67                      2                        1
--     orosz-ukrán           113                     30                        -
--
-- 'európa-bajnokság' matched a QUARTER OF THE CORPUS because it matched
-- 'európa'. 'Közép-Ázsia' matched 73 articles because 'közép' is an ordinary
-- Hungarian word. These are not near-misses; the term was being ignored and one
-- of its halves searched instead.
--
-- The corpus has 109 distinct hyphenated tags - Közép-Ázsia, CT-vizsgálat,
-- 2019-es ep-választás - so this is a shape the data is full of, not a curio.
--
-- THE FIX, PART ONE
--
-- AND the lexemes WITHIN a spelling; OR across spellings.
--
--     ('europa-bajnoksag' & 'europa' & 'bajnoksag' & 'europ')   lemma side
--   | ('europa-bajnoksag' & 'europa' & 'bajnoksag')             surface side
--
-- No false negatives are possible: the query text and the document text run
-- through the SAME lexeme functions, so every lexeme the term produces is
-- present in any document that contains the term. ANDing them can only remove
-- documents that never contained the compound at all.
--
-- For a term that yields ONE lexeme per side - every ordinary word, which is
-- the overwhelming majority - each side is a single lexeme and the result is
-- `lemma | surface`, exactly as before. Section 4 asserts that, by comparing
-- match behaviour captured before the rewrite.
--
-- WHY NOT A PHRASE QUERY
--
-- `to_tsquery` renders a hyphenated term as 'europa-bajnoksag' <-> 'europa'
-- <-> 'bajnoksag', which relies on POSITIONS - and corpus.search_vector's
-- positions are meaningless. Its lemma half is re-vectorised from a
-- lexicographically sorted array, and `||` shifts the surface half above it, so
-- the halves even fabricate an adjacency across their boundary (see 009). The
-- AND above needs no positions at all.
--
-- That is also why this builds the tsquery with an EXPLICIT ::tsquery CAST and
-- not to_tsquery: to_tsquery re-parses, and re-parsing a hyphenated lexeme
-- decomposes it right back into the phrase.
--
--     to_tsquery('simple', $$'orosz-ukran'$$)  ->  'orosz-ukran' <-> 'orosz' <-> 'ukran'
--     ($$'orosz-ukran'$$)::tsquery             ->  'orosz-ukran'
--
-- THE FIX, PART TWO: A HYPHENATED TOKEN IS SEVERAL TERMS
--
-- The AND above, alone, would require the literal compound - so an article
-- writing "orosz es ukran csapatok" would stop answering 'orosz-ukran'. That is
-- a stricter reading than 012 takes everywhere else, where a term is looked for
-- across the WHOLE document rather than inside one paragraph.
--
-- So corpus.search_terms now splits a token on '-' as well as on whitespace,
-- and each piece becomes its own term. Document-level AND then requires every
-- piece somewhere in the article:
--
--     'orosz-ukran haboru'  ->  3 terms: orosz, ukran, haboru
--
-- Both changes attack the same defect from opposite ends, and both are needed.
-- Splitting alone would leave any compound the parser decomposes on some OTHER
-- separator still matchable by one piece; the AND alone would demand the
-- literal compound.
--
-- Measured, this lands on the INDEPENDENT Python yardstick rather than being
-- graded by its own engine - the yardstick already split on '-' and was not
-- touched:
--
--     query                 before   after   yardstick
--     orosz-ukran haboru       113      30          31
--     europa-bajnoksag         263       7           7
--     Kozep-Azsia               73       1           6
--     CT-vizsgalat              67       2           2
--
-- The alternative reading - require the literal compound - scores 15, 5, 1, 2.
-- It is defensible, and it was rejected for one reason: it only looks correct
-- against a yardstick edited to agree with it. This one does not need the ruler
-- moved.
--
-- Query side only. No table rewritten, no GIN index rebuilt.
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
       AND pg_get_expr(d.adbin, d.adrelid) ILIKE '%term_query%';

    IF dependent IS NOT NULL THEN
        RAISE EXCEPTION
            'term_query is referenced by STORED generated column(s): %. '
            'Redefining it would desynchronise them.', dependent;
    END IF;
END
$guard$;

-- ---------------------------------------------------------------------
-- 1. Record how the CURRENT term_query behaves, as MATCHING not as text
-- ---------------------------------------------------------------------
-- Text comparison would fail on a harmless re-ordering of an OR. What must not
-- change is which documents a one-word term accepts, so that is what gets
-- captured: every (term, text) pair, and whether it matched.
CREATE TEMP TABLE _tq_before ON COMMIT DROP AS
SELECT t.term, x.txt,
       corpus.search_vector(x.txt) @@ corpus.term_query(t.term) AS matched
FROM unnest(ARRAY[
        'Orbán', 'Orban', 'Orbánt', 'Orbánnak', 'Magyarország',
        'Magyarországról', 'magyarországi', 'idén', 'Simon', 'USA', 'kik',
        'koronavírus', 'háború', 'baloldal', 'fejlesztés', 'nyomás'
     ]) AS t(term),
     unnest(ARRAY[
        'Orbán Viktor Brüsszelben tárgyalt',
        'Orban Viktor Brusszelben targyalt',
        'a magyar kormány Magyarországról beszélt',
        'idén nyáron a baloldal tüntetett',
        'koronavírus-járvány sújtotta Simon urat az USA-ban',
        'elhúzódó háború és migrációs nyomás',
        'ukrajnai fejlesztésekre költenek',
        'semmi köze a kérdéshez'
     ]) AS x(txt);

-- ---------------------------------------------------------------------
-- 2. AND within a spelling, OR across spellings
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

    -- The 010 guard, unchanged: drop a lemma of 1-2 characters that no spelling
    -- of the term produces on the surface. Applied per side, before the AND.
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
                             FROM (SELECT DISTINCT s FROM unnest(sur) AS s
                                    ORDER BY s) AS d(x)),
                     ' & ') || ')');
    END IF;

    IF array_length(sides, 1) IS NULL THEN
        RETURN ''::tsquery;          -- a stopword expands to nothing
    END IF;

    -- CAST, not to_tsquery: to_tsquery would re-parse and split the compound
    -- lexeme straight back into the phrase this migration exists to avoid.
    RETURN (array_to_string(sides, ' | '))::tsquery;
END
$fn$;

COMMENT ON FUNCTION corpus.term_query(text) IS
    'One query term as a tsquery: the lexemes of each SPELLING ANDed together, '
    'the spellings ORed. For an ordinary word that is lemma | surface, as '
    'before. For a hyphenated word it requires the compound rather than '
    'accepting either half - europa-bajnoksag no longer means europa (013).';

-- ---------------------------------------------------------------------
-- 2b. A hyphenated token contributes its pieces as separate terms
-- ---------------------------------------------------------------------
-- Only the ASCII hyphen, because that is what the text-search parser treats as
-- a compound separator and therefore what the stored lexemes were built around.
-- collapse_whitespace has already reduced every whitespace character in the
-- class to a single space (011), so the split is over ' ' and '-' only.
CREATE OR REPLACE FUNCTION corpus.search_terms(q text)
RETURNS tsquery[]
LANGUAGE sql
IMMUTABLE PARALLEL SAFE STRICT
AS $fn$
    SELECT coalesce(
             array_agg(tq ORDER BY ord)
               FILTER (WHERE tq <> ''::tsquery),
             ARRAY[]::tsquery[])
    FROM unnest(regexp_split_to_array(corpus.collapse_whitespace(q), '[ -]+'))
         WITH ORDINALITY AS s(term, ord),
    LATERAL (SELECT corpus.term_query(s.term) AS tq) t;
$fn$;

COMMENT ON FUNCTION corpus.search_terms(text) IS
    'One tsquery per query term, in order, stopwords dropped. The AND of these '
    'IS corpus.search_query. Terms split on the whitespace class (011) AND on '
    'the ASCII hyphen (013), so a hyphenated word contributes each of its '
    'pieces as a term that must appear somewhere in the document.';

-- ---------------------------------------------------------------------
-- 3. Prove it
-- ---------------------------------------------------------------------
DO $verify$
DECLARE
    bad text;
BEGIN
    -- 3a. Ordinary one-word terms must accept exactly the documents they did
    -- before. This is the compatibility claim, and it is checked, not asserted.
    SELECT string_agg(format('%L vs %L: was %s, now %s',
                             term, txt, matched,
                             corpus.search_vector(txt) @@ corpus.term_query(term)),
                      E'\n  ')
      INTO bad
      FROM _tq_before
     WHERE (corpus.search_vector(txt) @@ corpus.term_query(term))
           IS DISTINCT FROM matched;

    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'term_query changed a one-word answer:%s  %s', E'\n', bad;
    END IF;

    -- 3b. A hyphenated term matches text that contains the compound.
    IF NOT corpus.search_vector('az orosz-ukrán háború ötödik éve')
           @@ corpus.term_query('orosz-ukrán') THEN
        RAISE EXCEPTION 'the compound must match its own text';
    END IF;
    IF NOT corpus.search_vector('Közép-Ázsia felé fordult')
           @@ corpus.term_query('Közép-Ázsia') THEN
        RAISE EXCEPTION 'Közép-Ázsia must match its own text';
    END IF;

    -- 3c. THE DEFECT, at the term level: a bare part must no longer satisfy a
    -- compound handed to term_query directly.
    IF corpus.search_vector('orosz katonák a határon')
       @@ corpus.term_query('orosz-ukrán') THEN
        RAISE EXCEPTION 'bare "orosz" still satisfies the term "orosz-ukrán"';
    END IF;
    IF corpus.search_vector('Európa nagy kontinens')
       @@ corpus.term_query('európa-bajnokság') THEN
        RAISE EXCEPTION 'bare "európa" still satisfies "európa-bajnokság"';
    END IF;

    -- 3c2. THE DEFECT, at the query level, which is the path search actually
    -- takes: every piece of the hyphenated word must be present.
    IF corpus.search_vector('orosz katonák a határon')
       @@ corpus.search_query('orosz-ukrán') THEN
        RAISE EXCEPTION 'bare "orosz" still answers the query "orosz-ukrán"';
    END IF;
    IF corpus.search_vector('Európa közepén jártunk')
       @@ corpus.search_query('Közép-Ázsia') THEN
        RAISE EXCEPTION 'bare "közép" still answers the query "Közép-Ázsia"';
    END IF;

    -- 3d. Accent-free spelling of a compound still works - the surface side is
    -- unaccented, so this must hold without the lemma side helping.
    IF NOT corpus.search_vector('a Kozep-Azsia kerdes')
           @@ corpus.term_query('Közép-Ázsia') THEN
        RAISE EXCEPTION 'the accent-free spelling of a compound must match';
    END IF;

    -- 3e. Both halves present, written apart, DOES answer the query. This is
    -- the reading 012 takes everywhere else, and it is what lets the
    -- independent yardstick stay unedited. Pinned so a later change to it is a
    -- decision rather than an accident.
    IF NOT corpus.search_vector('orosz és ukrán csapatok')
           @@ corpus.search_query('orosz-ukrán') THEN
        RAISE EXCEPTION 'halves written apart should still answer the query';
    END IF;

    -- 3f. A hyphenated word contributes one term PER PIECE.
    IF cardinality(corpus.search_terms('orosz-ukrán háború')) <> 3 THEN
        RAISE EXCEPTION 'expected 3 terms for a hyphenated word plus one, got %',
            cardinality(corpus.search_terms('orosz-ukrán háború'));
    END IF;
    IF cardinality(corpus.search_terms('Közép-Ázsia')) <> 2 THEN
        RAISE EXCEPTION 'a hyphenated word alone is 2 terms';
    END IF;
    -- A run of hyphens, or a trailing one, must not produce an empty term.
    IF cardinality(corpus.search_terms('Állat- és Növénykert')) <> 3 THEN
        RAISE EXCEPTION 'a trailing hyphen must not create an empty term, got %',
            cardinality(corpus.search_terms('Állat- és Növénykert'));
    END IF;
    IF corpus.search_vector('az orosz-ukrán háború')
       @@ corpus.search_query('orosz-ukrán béke') THEN
        RAISE EXCEPTION 'the AND across terms must still bite';
    END IF;
END
$verify$;

INSERT INTO corpus.schema_migrations (version) VALUES ('013')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
