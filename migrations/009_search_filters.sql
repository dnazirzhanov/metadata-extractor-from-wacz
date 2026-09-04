-- =====================================================================
-- 009  Author filtering, date filtering, and phrase search
-- =====================================================================
-- Three capabilities the corpus was asked for and could not serve:
-- an exact author filter, a date-range browse, and a search for an exact
-- sequence of words ("one sentence search"). Only the first and third need
-- anything from the database; the date range is already served by
-- article_outlet_published_idx from 006 and needs no DDL.
--
-- ---------------------------------------------------------------------
-- WHY PHRASE SEARCH CANNOT REUSE corpus.search_vector
-- ---------------------------------------------------------------------
-- The obvious implementation is to keep using the stored vectors and hand
-- them a tsquery built with the <-> operator. That is WRONG here, and the
-- reason is visible in one call. For the text
--
--     Orbán Viktor Brüsszelben tárgyalt
--
-- corpus.search_vector returns
--
--     'brusszel':1 'orb':2 'targyal':3 'viktor':4      <- lemma side
--     'orban':5 'viktor':6 'brusszelben':7 'targyalt':8 <- surface side
--
-- Two independent defects for positional purposes:
--
--   1. The lemma side is built by flattening a tsvector through
--      tsvector_to_array, which returns lexemes in LEXICOGRAPHIC order.
--      Re-vectorising that string assigns positions alphabetically, so
--      'brusszel' - the third word of the sentence - is at position 1.
--      Distance between lemma lexemes carries no information about the text.
--
--   2. `tsvector || tsvector` renumbers the right operand above the left,
--      so the surface side is shifted by however many distinct lemmas the
--      document produced. Relative order survives there, but the JOIN between
--      the two halves invents an adjacency that is not in the text.
--
-- Defect 2 is not theoretical. Measured on this database:
--
--     SELECT corpus.search_vector('Viktor Orbán')
--        @@ ('viktor <-> viktor')::tsquery;          -->  TRUE
--
-- The text contains "viktor" exactly once. The match comes from the lemma
-- side's last lexeme ('viktor':2) sitting immediately before the surface
-- side's first ('viktor':3). A phrase search that fabricates a repeated word
-- is the same class of error as a fabricated citation, and this project
-- refuses those.
--
-- ---------------------------------------------------------------------
-- WHAT THIS DOES INSTEAD
-- ---------------------------------------------------------------------
-- Two stages, cheap then exact:
--
--   PREFILTER   corpus.search_query(q) on the existing GIN index. Its AND of
--               per-term (lemma | surface) alternatives is a guaranteed
--               SUPERSET of any phrase match: if the words occur adjacently
--               they occur at all, and every lexeme the phrase side can match
--               on is present in the stored vector by construction.
--
--   RECHECK     corpus.phrase_match(haystack, needle) recomputes a
--               document-ORDER vector for the few surviving rows and tests
--               the phrase properly.
--
-- The recheck is a sequential expression over a set the index has already
-- cut down, which is why no new stored column and no table rewrite is needed.
--
-- The recheck tests BOTH configurations and ORs them, because they fail on
-- opposite inputs and neither alone is enough:
--
--     text                     needle          lemma  surface
--     Orbán Viktor ...         Orbán Viktor      t      t
--     Viktor Orbán ...         Orbán Viktor      f      f     (order respected)
--     Viktor Orbán ...         Viktor Viktor     f      f     (the FP above)
--     Orbán Viktor ...         orban viktor      f      t     (accent-free)
--     Orbán Viktornak mondta   Orbán Viktor      t      f     (inflected)
--     Orbán ma Viktor          Orbán Viktor      f      f     (not adjacent)
--
-- lemma keeps accents and stems, so it tolerates inflection; surface folds
-- accents and does not stem, so it tolerates a keyboard without ékezet.
-- =====================================================================

BEGIN;

SET LOCAL lock_timeout = '5s';

-- ---------------------------------------------------------------------
-- 1. Exact author filter
-- ---------------------------------------------------------------------
-- Deferred in 006 (docs/postgres-schema.md D.3) on the grounds that
-- search_tsv already offers fuzzy author matching at weight C. That is the
-- right answer for "find articles that mention this person" and the wrong
-- one for "list what this person wrote": the weight-C match also fires on
-- tags and on any body-adjacent prose, so a byline filter built on it
-- silently returns articles the person did not write.
--
-- Same argument, and the same shape, as article_tags_idx.
CREATE INDEX IF NOT EXISTS article_authors_idx
    ON corpus.article USING gin (authors);

COMMENT ON INDEX corpus.article_authors_idx IS
    'Exact byline filter: authors @> ARRAY[''Nagy Márton'']. Deliberately '
    'distinct from the fuzzy weight-C match in search_tsv, which also fires '
    'on tags and body text.';

-- ---------------------------------------------------------------------
-- 2. Phrase matching
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION corpus.phrase_match(haystack text, needle text)
RETURNS boolean
LANGUAGE sql
IMMUTABLE PARALLEL SAFE
AS $$
    SELECT haystack IS NOT NULL AND needle IS NOT NULL
       AND (
            to_tsvector('corpus.hungarian_lemma', haystack)
                @@ phraseto_tsquery('corpus.hungarian_lemma', needle)
         OR to_tsvector('corpus.hungarian_surface', haystack)
                @@ phraseto_tsquery('corpus.hungarian_surface', needle)
       )
$$;

COMMENT ON FUNCTION corpus.phrase_match(text, text) IS
    'True when needle occurs in haystack as an adjacent word sequence. '
    'Recomputes document-order vectors on purpose: the stored '
    'corpus.search_vector concatenates an alphabetically-ordered lemma side '
    'with a shifted surface side, so <-> over it is meaningless and can '
    'fabricate adjacency. Always pair with corpus.search_query() as an '
    'index-served prefilter - this function alone forces a sequential scan.';

INSERT INTO corpus.schema_migrations (version) VALUES ('009')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
