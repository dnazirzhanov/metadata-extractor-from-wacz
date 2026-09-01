-- A guided tour of how the corpus full-text search actually works.
--
--   ./scripts/devdb.sh psql -f "$PWD/scripts/search_demo.sql"
--
-- or, interactively, so you can edit and re-run individual queries:
--
--   ./scripts/devdb.sh psql
--   \i <absolute path to this repo>/scripts/search_demo.sql
--
-- The path must be ABSOLUTE and must be the repo's real path: psql runs inside
-- the container, where devdb.sh bind-mounts the repo read-only at that same
-- path. A relative path resolves against the container's cwd and will not
-- be found. (That mount is also what lets migrate.sh's `psql -f` work.)
--
-- Everything below is read-only.

\pset border 2
\pset null '(null)'
\timing on

\echo
\echo ===== 1. What the analyzer does to text =====
\echo Search never compares strings. It compares LEXEMES. This is the whole game:
\echo

SELECT word,
       to_tsvector('corpus.hungarian_ci', word)::text AS lexemes
FROM (VALUES ('Orbán Viktor miniszterelnök'),
             ('Magyarországra'),
             ('Magyarországról'),
             ('a japánok is felnéznek'),
             ('MAGYARORSZAG')) AS t(word);

\echo
\echo -- Note: 'a' and 'is' vanished (stopwords), and every accent was folded.
\echo -- corpus.hungarian_ci = unaccent + hungarian_stem. Compare to raw hungarian:
\echo

SELECT to_tsvector('hungarian',       'Magyarországra') AS "hungarian (accents kept)",
       to_tsvector('corpus.hungarian_ci','Magyarországra') AS "hungarian_ci (folded)";

\echo
\echo ===== 2. What your QUERY turns into =====
\echo websearch_to_tsquery is the forgiving one - it accepts what a person types.
\echo

SELECT q AS typed,
       websearch_to_tsquery('corpus.hungarian_ci', q)::text AS parsed
FROM (VALUES ('Orbán Viktor'),
             ('orbán OR trump'),
             ('Magyarország -sport'),
             ('"orosz ukrán"'),
             ('magyarorszag')) AS t(q);

\echo
\echo -- Bare words are ANDed. OR, -exclusion and "phrases" all work.
\echo

\echo
\echo ===== 3. Does it match? The @@ operator =====
\echo

SELECT to_tsvector('corpus.hungarian_ci','Orbán Viktor Debrecenben járt')
         @@ websearch_to_tsquery('corpus.hungarian_ci','Orbán Viktor')  AS "both terms",
       to_tsvector('corpus.hungarian_ci','Orbán Viktor Debrecenben járt')
         @@ websearch_to_tsquery('corpus.hungarian_ci','Orbán Trump')   AS "AND fails",
       to_tsvector('corpus.hungarian_ci','Orbán Viktor Debrecenben járt')
         @@ websearch_to_tsquery('corpus.hungarian_ci','orban viktor')  AS "no accents";

\echo
\echo ===== 4. The weights, which are why a title beats a tag =====
\echo article.search_tsv is built with setweight A=title B=standfirst/description C=authors/tags.
\echo

SELECT left(title, 44) AS title,
       round(ts_rank(search_tsv,
             websearch_to_tsquery('corpus.hungarian_ci','Magyarország'))::numeric, 4) AS rank,
       CASE WHEN title ILIKE '%agyarország%' THEN 'in title'
            WHEN tags @> ARRAY['Magyarország'] THEN 'in tags'
            ELSE 'elsewhere' END AS where_from
FROM corpus.article
WHERE search_tsv @@ websearch_to_tsquery('corpus.hungarian_ci','Magyarország')
ORDER BY rank DESC;

\echo
\echo ===== 5. Searching the BODY - the semi-join, no third vector =====
\echo An article matches on its prose through EXISTS over its blocks.
\echo

SELECT a.outlet, left(a.title, 40) AS title, count(*) AS matching_blocks
FROM corpus.article a
JOIN corpus.content_block b ON b.article_id = a.id
                           AND b.extraction_id = a.current_extraction_id
WHERE b.text_tsv @@ websearch_to_tsquery('corpus.hungarian_ci','Magyarország')
GROUP BY a.id, a.outlet, a.title
ORDER BY matching_blocks DESC
LIMIT 8;

\echo
\echo ===== 6. The citable unit: block + xpath + highlighted text =====
\echo This is what a UI needs to jump to the passage.
\echo

SELECT b.block_index AS idx, b.block_type AS type, b.xpath,
       ts_headline('corpus.hungarian_ci', b.block_text,
                   websearch_to_tsquery('corpus.hungarian_ci','Orbán Viktor'),
                   'MaxFragments=1,MinWords=6,MaxWords=18,StartSel=[,StopSel=]') AS snippet
FROM corpus.content_block b
JOIN corpus.article a ON a.id = b.article_id
WHERE b.extraction_id = a.current_extraction_id
  AND b.text_tsv @@ websearch_to_tsquery('corpus.hungarian_ci','Orbán Viktor')
ORDER BY ts_rank(b.text_tsv,
                 websearch_to_tsquery('corpus.hungarian_ci','Orbán Viktor')) DESC
LIMIT 5;

\echo
\echo ===== 7. Exact tag filter vs full text - deliberately different answers =====
\echo

SELECT 'exact tag filter' AS method, count(*) AS articles
FROM corpus.article WHERE tags @> ARRAY['Magyarország']
UNION ALL
SELECT 'full-text search', count(*)
FROM corpus.article a
WHERE a.search_tsv @@ websearch_to_tsquery('corpus.hungarian_ci','Magyarország')
   OR EXISTS (SELECT 1 FROM corpus.content_block b
               WHERE b.article_id = a.id
                 AND b.extraction_id = a.current_extraction_id
                 AND b.text_tsv @@ websearch_to_tsquery('corpus.hungarian_ci','Magyarország'));

\echo
\echo -- The filter answers "tagged Magyarország". Full text answers "mentions it".
\echo -- Conflating those is why both operations exist.
\echo

\echo
\echo ===== 8. Where the stemmer bites =====
\echo

SELECT word, to_tsvector('corpus.hungarian_ci', word)::text AS lexeme,
       to_tsvector('corpus.hungarian_ci', word)
         @@ websearch_to_tsquery('corpus.hungarian_ci','Orbán') AS "found by 'Orbán'?"
FROM (VALUES ('Orbán'), ('Orbánt'), ('Orbánnak'), ('orra'), ('ori')) AS t(word);

\echo
\echo -- 'Orbán' over-stems to the 2-letter lexeme 'or', which 'orra' and 'ori'
\echo -- also produce; 'Orbánt' and 'Orbánnak' land on 'orban' instead, so the
\echo -- base form does NOT find them. This is snowball, not our schema.
\echo

\echo
\echo ===== 9. What the planner does (36 articles: seq scan, not the GIN index) =====
\echo

EXPLAIN (ANALYZE, COSTS OFF)
SELECT id FROM corpus.article
WHERE search_tsv @@ websearch_to_tsquery('corpus.hungarian_ci','Magyarország');

\echo
\echo -- At this size a seq scan is genuinely cheaper. To force the index and
\echo -- confirm it is usable:  SET enable_seqscan = off;  then re-run.
\echo

\timing off
