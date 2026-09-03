-- A guided tour of how the corpus actually stores and searches articles.
--
-- The eval database on milab2 has no bind mount, so pipe this in on stdin
-- rather than using psql -f:
--
--   ssh $MILAB2 'docker exec -i cx-pg-eval psql -U causalia -d causalia_eval' \
--       < scripts/search_demo.sql
--
-- Against the local dev database, where devdb.sh DOES bind-mount the repo:
--
--   ./scripts/devdb.sh psql -f "$PWD/scripts/search_demo.sql"
--
-- Everything below is READ-ONLY. Nothing here writes, and nothing here needs
-- the extractor, a .wacz or the filesystem.
--
-- Rewritten for migrations 007 (lemma+surface vectors), 008 (caption search)
-- and 009 (author, date and phrase). An earlier version of this file taught
-- websearch_to_tsquery over corpus.hungarian_ci; both were replaced and neither
-- is what the vectors use any more.

\pset pager off
\timing off

\echo
\echo =====================================================================
\echo ===== 1. Where the bytes live: pointers, never blobs
\echo =====================================================================
\echo A row never holds a file. It holds a RELATIVE path plus enough metadata
\echo to serve the file without opening it. Proof, not documentation:

SELECT count(*) AS binary_columns_in_corpus
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'corpus' AND a.attnum > 0 AND NOT a.attisdropped
  AND format_type(a.atttypid, a.atttypmod) IN ('bytea', 'oid');

SELECT count(*) AS large_objects FROM pg_largeobject;

\echo
\echo -- The whole database, against the bytes it indexes:

SELECT pg_size_pretty(pg_database_size(current_database())) AS database_on_disk,
       pg_size_pretty(sum(byte_size))                       AS files_pointed_at,
       count(*)                                             AS artifact_rows
FROM corpus.article_artifact;

\echo
\echo -- Three homes, by kind. readability.html and the screenshot are
\echo -- document-level singletons; images and videos carry their own domain
\echo -- metadata, so they are NOT artifact rows.

SELECT kind, media_type, count(*), pg_size_pretty(sum(byte_size)) AS bytes
FROM corpus.article_artifact
GROUP BY kind, media_type ORDER BY kind, count(*) DESC;

\echo
\echo -- One article, every file it owns. Note every path is RELATIVE: the same
\echo -- corpus is mounted at a different absolute path on milab2, on milab4 and
\echo -- over sshfs, so an absolute path would encode one machine view only.

WITH one AS (
    SELECT a.id, a.title FROM corpus.article a
    JOIN corpus.article_image i ON i.article_id = a.id
    JOIN corpus.article_video v ON v.article_id = a.id
    WHERE i.file_path IS NOT NULL AND v.file_path IS NOT NULL
    LIMIT 1)
SELECT 'artifact: ' || kind AS what, file_path, media_type
  FROM corpus.article_artifact WHERE article_id = (SELECT id FROM one)
UNION ALL
SELECT 'image:    ' || local_ref, file_path, media_type
  FROM corpus.article_image WHERE article_id = (SELECT id FROM one)
UNION ALL
SELECT 'video:    ' || local_ref, file_path, platform
  FROM corpus.article_video WHERE article_id = (SELECT id FROM one)
ORDER BY what;

\echo
\echo =====================================================================
\echo ===== 2. What the analyzer does to text
\echo =====================================================================
\echo Search never compares strings, it compares LEXEMES. Since 007 every word
\echo contributes TWO of them, and the pair is the whole trick:
\echo
\echo     LEMMA    stem with accents INTACT, then fold accents off the lemma
\echo     SURFACE  fold accents off the word, no stemming at all
\echo
\echo The lemma side carries inflection; the surface side carries the spelling
\echo of a keyboard with no ekezet. Neither can be broken by the other.

SELECT to_tsvector('corpus.hungarian_lemma',  'Orbán Viktor kormányban') AS lemma_side,
       to_tsvector('corpus.hungarian_surface','Orbán Viktor kormányban') AS surface_side;

\echo
\echo -- And the union that is actually stored:

SELECT corpus.search_vector('Orbán Viktor kormányban') AS stored_vector;

\echo
\echo -- READ THE POSITIONS ABOVE CAREFULLY. The lemma half is numbered in
\echo -- ALPHABETICAL order, because it is built by flattening a tsvector through
\echo -- tsvector_to_array, which sorts. The surface half is then renumbered
\echo -- above it by the || operator. Section 8 shows why that matters.

\echo
\echo =====================================================================
\echo ===== 3. What your QUERY turns into
\echo =====================================================================
\echo Queries go through corpus.search_query(), NOT websearch_to_tsquery: the
\echo vectors hold two lexemes per word, so a query must be expanded into the
\echo matching per-term alternation and ANDed across terms.

SELECT q, corpus.search_query(q) AS expanded
FROM (VALUES ('Orbán'), ('Orbánnak'), ('kormányban'), ('Magyarország')) v(q);

\echo
\echo -- ...and here is a REAL DEFECT, still open. Strip the accent and the
\echo -- snowball stemmer reads the trailing -ban of "Orban" as the inessive
\echo -- case suffix and strips it, leaving a two-letter lexeme:

SELECT q, corpus.search_query(q) AS expanded
FROM (VALUES ('Orbán'), ('Orban')) v(q);

\echo
\echo -- The lexeme "or" is not a word, it is a collision magnet. How many
\echo -- articles does that single lexeme drag in?

SELECT count(DISTINCT a.id) AS articles_matching_bare_or
FROM corpus.article a
WHERE EXISTS (SELECT 1 FROM corpus.content_block b
               WHERE b.article_id = a.id
                 AND b.extraction_id = a.current_extraction_id
                 AND b.text_tsv @@ 'or'::tsquery);

\echo
\echo -- So the accented and unaccented spellings of the SAME name do not return
\echo -- the same corpus. Accent-insensitivity is what the design promised here.

SELECT q,
  (SELECT count(DISTINCT a.id) FROM corpus.article a
    WHERE a.search_tsv @@ corpus.search_query(q)
       OR EXISTS (SELECT 1 FROM corpus.content_block b
                   WHERE b.article_id = a.id
                     AND b.extraction_id = a.current_extraction_id
                     AND b.text_tsv @@ corpus.search_query(q))) AS articles
FROM (VALUES ('Orbán'), ('Orban')) v(q);

\echo
\echo =====================================================================
\echo ===== 4. The weights, which are why a title beats a tag
\echo =====================================================================
\echo article.search_tsv is setweight A=title B=subtitle/description C=authors/tags.

SELECT left(title, 52) AS title,
       round(ts_rank(search_tsv, corpus.search_query('Orbán Viktor'))::numeric, 4) AS rank
FROM corpus.article
WHERE search_tsv @@ corpus.search_query('Orbán Viktor')
ORDER BY rank DESC LIMIT 5;

\echo
\echo =====================================================================
\echo ===== 5. Searching the BODY - a semi-join, not a third vector
\echo =====================================================================
\echo There is deliberately no article-level body vector. An article matches on
\echo its prose through EXISTS over its blocks, which avoids duplicating the
\echo whole corpus text into a second GIN index.

SELECT a.outlet, left(a.title, 46) AS title, count(b.id) AS matching_blocks
FROM corpus.article a
JOIN corpus.content_block b ON b.article_id = a.id
                           AND b.extraction_id = a.current_extraction_id
WHERE b.text_tsv @@ corpus.search_query('kormány')
GROUP BY a.id, a.outlet, a.title
ORDER BY matching_blocks DESC LIMIT 5;

\echo
\echo =====================================================================
\echo ===== 6. Image captions - discoverable, deliberately NOT citable
\echo =====================================================================
\echo block_text is NULL for image blocks by design, so before 008 every caption
\echo was invisible to search. It got its own vector on article_image.
\echo A caption match tells you WHICH article and WHICH image, and stops there:
\echo a caption has no selector, so it can be ranked but never cited.

SELECT a.outlet, i.local_ref, left(i.caption, 58) AS caption
FROM corpus.article_image i
JOIN corpus.article a ON a.id = i.article_id
WHERE i.caption_tsv @@ corpus.search_query('kormány')
  AND i.extraction_id = a.current_extraction_id
LIMIT 5;

\echo
\echo =====================================================================
\echo ===== 7. The citable unit: block + xpath + the exact characters
\echo =====================================================================
\echo This is what a frontend needs to jump to a passage. The xpath addresses
\echo readability.html - the file whose path section 1 showed - and the offsets
\echo are over normalize_text() of the element it selects.

SELECT b.block_index, b.block_type, b.xpath,
       ts_headline('corpus.hungarian_surface', b.block_text,
                   corpus.search_query('Orbán Viktor'),
                   'MaxFragments=1,MinWords=6,MaxWords=18,StartSel=<<,StopSel=>>') AS passage
FROM corpus.content_block b
JOIN corpus.article a ON a.id = b.article_id
WHERE b.extraction_id = a.current_extraction_id
  AND b.text_tsv @@ corpus.search_query('Orbán Viktor')
ORDER BY ts_rank(b.text_tsv, corpus.search_query('Orbán Viktor')) DESC
LIMIT 4;

\echo
\echo -- A stored citation verifies in pure SQL, with no DOM: the slice of
\echo -- block_text at the stored offsets must equal quote.exact.

SELECT p.id, p.resolution_status,
       substring(b.block_text FROM p.quote_start + 1
                 FOR p.quote_end - p.quote_start) = p.quote_exact AS slice_agrees
FROM corpus.passage_reference p
JOIN corpus.content_block b ON b.id = p.content_block_id
LIMIT 5;

\echo
\echo =====================================================================
\echo ===== 8. PHRASE search, and why <-> on the stored vector is wrong
\echo =====================================================================
\echo Section 2 showed the stored vector interleaves an alphabetically-ordered
\echo lemma half with a renumbered surface half. Ask it a positional question
\echo and it will lie. "Viktor Orban" contains the word viktor exactly ONCE:

SELECT corpus.search_vector('Viktor Orbán')                         AS the_vector,
       corpus.search_vector('Viktor Orbán') @@ 'viktor <-> viktor'::tsquery
                                                                    AS naive_says_yes,
       corpus.phrase_match('Viktor Orbán', 'Viktor Viktor')         AS phrase_match_says;

\echo
\echo -- corpus.phrase_match (009) recomputes DOCUMENT-ORDER vectors for the row
\echo -- and ORs the two configurations, because they fail on opposite inputs:

SELECT label,
       corpus.phrase_match(txt, needle) AS is_phrase
FROM (VALUES
   ('exact order',      'Orbán Viktor Brüsszelben tárgyalt', 'Orbán Viktor'),
   ('reversed order',   'Viktor Orbán ma beszélt',           'Orbán Viktor'),
   ('the false match',  'Viktor Orbán ma beszélt',           'Viktor Viktor'),
   ('accent-free query','Orbán Viktor Brüsszelben tárgyalt', 'orban viktor'),
   ('inflected text',   'Orbán Viktornak mondta',            'Orbán Viktor'),
   ('words far apart',  'Orbán ma valamit mondott Viktor',   'Orbán Viktor')
) t(label, txt, needle);

\echo
\echo -- Used correctly it is a RECHECK, never the primary matcher:
\echo -- search_query() runs first on the GIN index, phrase_match() filters the
\echo -- survivors. The difference between the two is the point of the feature.

SELECT needle,
  (SELECT count(*) FROM corpus.content_block b JOIN corpus.article a ON a.id = b.article_id
    WHERE b.extraction_id = a.current_extraction_id
      AND b.text_tsv @@ corpus.search_query(needle))                  AS bag_of_words,
  (SELECT count(*) FROM corpus.content_block b JOIN corpus.article a ON a.id = b.article_id
    WHERE b.extraction_id = a.current_extraction_id
      AND b.text_tsv @@ corpus.search_query(needle)
      AND corpus.phrase_match(b.block_text, needle))                  AS actual_phrase
FROM (VALUES ('Orbán Viktor'), ('magyar kormány'), ('Európai Unió'),
             ('orosz-ukrán háború')) v(needle);

\echo
\echo =====================================================================
\echo ===== 9. Exact filters: tag, author, section, date
\echo =====================================================================
\echo These are PREDICATES, not full text, and the difference is not cosmetic.
\echo A weight-C match in search_tsv also fires on tags and on body prose, so a
\echo byline filter built on it returns articles the person did not write:

SELECT 'exact byline'   AS method, count(*) FROM corpus.article
  WHERE authors @> ARRAY['MTI']::text[]
UNION ALL
SELECT 'full-text',                 count(*) FROM corpus.article
  WHERE search_tsv @@ corpus.search_query('MTI');

\echo
\echo -- Same argument for tags. The filter answers "tagged X"; full text
\echo -- answers "mentions X". Conflating them is why both operations exist.

SELECT 'exact tag'  AS method, count(*) FROM corpus.article
  WHERE tags @> ARRAY['Ukrajna']::text[]
UNION ALL
SELECT 'full-text',            count(*) FROM corpus.article
  WHERE search_tsv @@ corpus.search_query('Ukrajna');

\echo
\echo -- Date range, served by article_outlet_published_idx.

SELECT date_trunc('year', published_at)::date AS year, count(*)
FROM corpus.article
WHERE published_at >= '2024-01-01' AND published_at < '2027-01-01'
GROUP BY 1 ORDER BY 1;

\echo
\echo -- Beware: a date filter silently hides rows the SOURCE dated wrongly.
\echo -- published_at_raw is kept verbatim precisely so this is provable rather
\echo -- than mysterious - these are real 2004-era articles:

SELECT outlet, published_at, published_at_raw, left(title, 38) AS title
FROM corpus.article WHERE published_at < '1990-01-01' LIMIT 4;

\echo
\echo =====================================================================
\echo ===== 10. Media coverage: what the capture actually holds
\echo =====================================================================
\echo is_available and is_archived are the honest columns. Note that
\echo is_archived=true with file_path=NULL is a VALID state: an adaptive-stream
\echo ladder is held in the capture but cannot be reassembled into one file.

SELECT platform,
       count(*)                                        AS embedded,
       count(*) FILTER (WHERE is_archived)             AS bytes_in_capture,
       count(*) FILTER (WHERE file_path IS NOT NULL)   AS playable_file
FROM corpus.article_video
GROUP BY platform ORDER BY embedded DESC;

\echo
\echo -- Images: available means the capture held the bytes.

SELECT is_available,
       count(*)                                       AS images,
       count(*) FILTER (WHERE file_path IS NOT NULL)  AS with_file,
       count(*) FILTER (WHERE caption IS NOT NULL)    AS with_caption,
       count(*) FILTER (WHERE credit IS NOT NULL)     AS with_credit
FROM corpus.article_image GROUP BY is_available;

\echo
\echo -- Screenshots: media_type is how the rule prefer the raster capture
\echo -- over the webp fallback is expressed as data, not as a filename rule.

SELECT media_type, count(*), pg_size_pretty(avg(byte_size)::bigint) AS avg_size
FROM corpus.article_artifact WHERE kind = 'screenshot'
GROUP BY media_type ORDER BY count(*) DESC;

\echo
\echo =====================================================================
\echo ===== 11. Corpus shape, for orientation
\echo =====================================================================

SELECT (SELECT count(*) FROM corpus.article)          AS articles,
       (SELECT count(DISTINCT outlet) FROM corpus.article) AS outlets,
       (SELECT count(*) FROM corpus.content_block)    AS blocks,
       (SELECT count(*) FROM corpus.article_image)    AS images,
       (SELECT count(*) FROM corpus.article_video)    AS videos,
       (SELECT count(*) FROM corpus.article_link)     AS links,
       (SELECT count(*) FROM corpus.article_artifact) AS artifacts;

\echo
\echo -- Blocks that can never match anything: image and video blocks carry no
\echo -- prose by design. 008 is why their captions are still reachable.

SELECT block_type, count(*),
       count(*) FILTER (WHERE text_tsv = ''::tsvector) AS empty_vector
FROM corpus.content_block GROUP BY block_type ORDER BY count(*) DESC;

\echo
\echo ===== end of tour =====
\echo
