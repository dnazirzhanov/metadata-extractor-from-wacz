-- ============================================================================
-- CAUSALIA SEARCH ENGINE - MENTOR DEMONSTRATION
-- ============================================================================
-- Every query in this file was executed against the evaluation database on
-- 2026-09-07 and the counts in the comments are the counts it returned. Nothing
-- here is hypothetical, and nothing here writes: the whole file is SELECTs.
--
-- TARGET DATABASE   cx-pg-eval - 1,008 articles, 10,589 content blocks,
--                   migration 023, 18 Hungarian news outlets, 2000-2026.
--
-- CONNECTING FROM INTELLIJ
--   The database listens on loopback on milab2, so open a tunnel first:
--
--       ssh -N -L 55435:127.0.0.1:55435 c0cshf@10.1.12.63
--
--   Then add a PostgreSQL data source:
--       host      127.0.0.1
--       port      55435
--       database  causalia_eval
--       user      causalia
--       password  eval
--
--   The same file also runs unchanged against cx-pg-d1 (port 55440, database
--   causalia_d1, password d1) - the 16,008-article random sample from the D1
--   staged ingest. Counts there are roughly 14x larger. Use eval for the demo:
--   the numbers below are its numbers.
--
-- ============================================================================
-- TEN-MINUTE RUNNING ORDER
-- ============================================================================
--   0:00  Demo 1   one word                     lexical retrieval
--   0:45  Demo 2   accents                      normalisation, and its limits
--   1:30  Demo 3   morphology                   Hungarian is agglutinative
--   2:15  Demo 4   two words                    article-level conjunction
--   3:15  Demo 5   phrase                       positional matching
--   4:15  Demo 6   FULL PARAGRAPH               <- slow down here
--   5:30  Demo 7   PARTIAL PARAGRAPH            <- and here
--   6:30  Demo 8   block -> exact passage        the evidence chain
--   7:30  Demo 9   selector                     where the evidence LIVES
--   8:00  Demo 9b  title-only search             field-scoped retrieval
--   8:15  Demo 10  filters compose
--   8:45  Demo 11  pagination
--   9:00  Demo 12  edge cases                   compounds and dashes
--   9:30  Demo 13  refused syntax               correctness over convenience
--
-- ============================================================================
-- WHAT EACH DEMO SHOWS, AND WHAT IT RETURNED
-- ============================================================================
--  #  Query                            Returned            Demonstrates
--  1  kormány                          94 articles         basic retrieval
--  2  kormany / kor / kór              94 / 26 / 1         one-way accent policy
--  3  kormánynak / kormanynak          94 / 57             stemming
--  4  kormány Ukrajna                  49 articles         doc-level AND
--     ...of those                      26 split across blocks
--  5  Orbán Viktor  (phrase)           178 of 179 blocks   adjacency
--     Viktor Orbán  (phrase)           0 of 179 blocks     order matters
--  6  <whole paragraph>                1 block             paragraph identity
--  7  <fragment of it>                 1 block             partial recall
--  8  block 35 detail                  1 row               the citable unit
--  9  derived selector                 start 61, end 90    exact evidence
-- 9b  kormány in title only           24 of 94 articles   weight-scoped search
-- 10  kormány + outlet + tag           4 articles          composition
-- 11  page 1 / page 2                  5 + 5, disjoint     pagination
-- 12  hyphen vs en dash                true / false        tokenisation
-- 13  kormány OR Ukrajna               0 articles          refused, not guessed
-- ============================================================================


-- ============================================================================
-- DEMO 1 - ONE WORD
-- ============================================================================
-- SAY: "The simplest thing. One Hungarian word, and the articles that contain
--       it anywhere - title, subtitle, tags, byline or body."
--
-- TECHNICAL: corpus.search_query() turns the text into a tsquery; the GIN index
-- on article.search_tsv answers it. Everything else in this file is a
-- refinement of this one line.
-- ============================================================================
SELECT count(*) AS articles                                    -- returned 94
  FROM corpus.article
 WHERE search_tsv @@ corpus.search_query('kormány');

SELECT id, outlet, published_at::date, title
  FROM corpus.article
 WHERE search_tsv @@ corpus.search_query('kormány')
 ORDER BY published_at DESC NULLS LAST, id
 LIMIT 5;


-- ============================================================================
-- DEMO 2 - ACCENTS, AND THE POLICY IS ONE-WAY
-- ============================================================================
-- SAY: "A journalist without a Hungarian keyboard types 'kormany'. They get the
--       same 94 articles. But the policy is deliberately asymmetric: typing the
--       accent NARROWS the search, typing without it does not."
--
-- TECHNICAL: the stored vector carries an accent-folded lexeme AND an
-- accent-preserving one (migration 016). An accent-free query matches both; an
-- accented query is held to the accented one (migration 017).
-- ============================================================================
SELECT corpus.search_query('kormany')::text AS accent_free,    -- folded + surface
       corpus.search_query('kormány')::text AS accented;       -- accent-sensitive

SELECT 'kormany' AS query, count(*) AS articles                -- returned 94
  FROM corpus.article WHERE search_tsv @@ corpus.search_query('kormany')
UNION ALL
SELECT 'kormány', count(*)                                     -- returned 94
  FROM corpus.article WHERE search_tsv @@ corpus.search_query('kormány');

-- The pair that shows why the asymmetry matters. 'kor' means age; 'kór' means
-- disease. They are different words, and the engine keeps them apart - but only
-- for the reader who typed the accent.
SELECT 'kor  (age)'     AS query, count(*) AS articles         -- returned 26
  FROM corpus.article WHERE search_tsv @@ corpus.search_query('kor')
UNION ALL
SELECT 'kór  (disease)', count(*)                              -- returned 1
  FROM corpus.article WHERE search_tsv @@ corpus.search_query('kór');

-- Stated as a property, independent of what happens to be in the corpus:
SELECT corpus.search_vector('a kor jó volt') @@ corpus.search_query('kór')  AS kor_does_not_answer_kór,   -- false
       corpus.search_vector('a kór terjedt') @@ corpus.search_query('kor')  AS kor_DOES_answer_kór;       -- true


-- ============================================================================
-- DEMO 3 - HUNGARIAN MORPHOLOGY
-- ============================================================================
-- SAY: "Hungarian glues its grammar onto the end of the word. 'kormánynak' is
--       'to the government'. The reader should not have to guess which of the
--       eighteen case endings the journalist happened to use."
--
-- TECHNICAL: the vector holds a stemmed lemma beside the literal surface form,
-- so an inflected query reaches the base word and vice versa. Migration 019
-- fixed the case where the accent is dropped AND the word is inflected -
-- 'kormanyrol' used to return nothing at all.
-- ============================================================================
SELECT 'kormány'     AS query, count(*) AS articles            -- returned 94
  FROM corpus.article WHERE search_tsv @@ corpus.search_query('kormány')
UNION ALL
SELECT 'kormánynak', count(*)                                  -- returned 94
  FROM corpus.article WHERE search_tsv @@ corpus.search_query('kormánynak')
UNION ALL
SELECT 'kormanynak  (no accents, inflected)', count(*)         -- returned 57
  FROM corpus.article WHERE search_tsv @@ corpus.search_query('kormanynak');

-- POINT OUT: 94 / 94 / 57. The inflected accented form is as good as the base
-- form. The accent-free inflected form is weaker but no longer zero - that was
-- migration 019, six hours of work for 57 articles that used to be invisible.


-- ============================================================================
-- DEMO 4 - TWO WORDS, AND WHY THE ARTICLE IS THE UNIT
-- ============================================================================
-- SAY: "Both words must appear in the article. They do NOT have to appear in
--       the same paragraph - and that distinction is worth 26 of these 49
--       articles."
--
-- TECHNICAL: migration 012. Each term is resolved by its own index-served UNION
-- over article metadata, content blocks and image captions; the terms are then
-- intersected by counting distinct ordinals. Before 012 the whole query was
-- handed to one vector, which forced every term into the same paragraph.
-- ============================================================================
WITH q AS (SELECT corpus.search_terms('kormány Ukrajna') AS terms),
     doc AS (
        SELECT m.article_id
          FROM q, unnest(q.terms) WITH ORDINALITY AS t(tsq, ord),
               LATERAL (
                   SELECT a2.id AS article_id FROM corpus.article a2
                    WHERE a2.search_tsv @@ t.tsq
                   UNION
                   SELECT b.article_id FROM corpus.content_block b
                     JOIN corpus.article a3 ON a3.id = b.article_id
                    WHERE b.extraction_id = a3.current_extraction_id
                      AND b.text_tsv @@ t.tsq
                   UNION
                   SELECT i.article_id FROM corpus.article_image i
                     JOIN corpus.article a4 ON a4.id = i.article_id
                    WHERE i.extraction_id = a4.current_extraction_id
                      AND i.caption_tsv @@ t.tsq
               ) m
         GROUP BY m.article_id
        HAVING count(DISTINCT t.ord) = (SELECT cardinality(terms) FROM q))
SELECT count(*) AS articles_matching_both                      -- returned 49
  FROM doc;

-- The number that makes the point: articles where the two words are real but
-- NO SINGLE PARAGRAPH contains both.
SELECT count(*) AS articles_with_terms_in_different_blocks     -- returned 26
  FROM (
    SELECT a.id
      FROM corpus.article a
     WHERE EXISTS (SELECT 1 FROM corpus.content_block b WHERE b.article_id=a.id
                     AND b.text_tsv @@ corpus.search_query('kormány'))
       AND EXISTS (SELECT 1 FROM corpus.content_block b WHERE b.article_id=a.id
                     AND b.text_tsv @@ corpus.search_query('Ukrajna'))
       AND NOT EXISTS (SELECT 1 FROM corpus.content_block b WHERE b.article_id=a.id
                     AND b.text_tsv @@ corpus.search_query('kormány Ukrajna'))
  ) s;

-- Article 29 is one of them: 'kormány' in one paragraph, 'Ukrajna' in four
-- others, and no paragraph holding both.
SELECT (SELECT count(*) FROM corpus.content_block b WHERE b.article_id=29
          AND b.text_tsv @@ corpus.search_query('kormány'))  AS blocks_with_kormány,   -- 1
       (SELECT count(*) FROM corpus.content_block b WHERE b.article_id=29
          AND b.text_tsv @@ corpus.search_query('Ukrajna'))  AS blocks_with_ukrajna,   -- 4
       (SELECT count(*) FROM corpus.content_block b WHERE b.article_id=29
          AND b.text_tsv @@ corpus.search_query('kormány Ukrajna')) AS blocks_with_both; -- 0


-- ============================================================================
-- DEMO 5 - PHRASE: A POSITIONAL QUESTION
-- ============================================================================
-- SAY: "Up to now I have been asking whether words OCCUR. Now I ask whether
--       they occur NEXT TO EACH OTHER, in order."
--
-- TECHNICAL, and this is the architecture worth drawing on the whiteboard:
--
--       GIN candidate filter          cheap, index-served, may over-produce
--                 |                   MUST NOT under-produce
--                 v
--       corpus.phrase_match()         exact, recomputes positional vectors
--                 |                   from the block text itself
--                 v
--       citation resolution
--
-- The stored vector cannot answer a positional question - its positions are an
-- artefact of how the vector is built, not document order. phrase_match
-- therefore recomputes vectors from block_text at query time. The filter is
-- 200-2,300x cheaper than phrase_match, which is the whole reason for the
-- two-stage design.
-- ============================================================================
SELECT 'Orbán Viktor' AS phrase,
       count(*) FILTER (WHERE text_tsv @@ corpus.search_query('Orbán Viktor'))
         AS candidate_blocks,                                  -- returned 179
       count(*) FILTER (WHERE text_tsv @@ corpus.search_query('Orbán Viktor')
                          AND corpus.phrase_match(block_text, 'Orbán Viktor'))
         AS phrase_verified                                    -- returned 178
  FROM corpus.content_block
UNION ALL
SELECT 'Viktor Orbán  (reversed)',
       count(*) FILTER (WHERE text_tsv @@ corpus.search_query('Viktor Orbán')),
                                                               -- returned 179
       count(*) FILTER (WHERE text_tsv @@ corpus.search_query('Viktor Orbán')
                          AND corpus.phrase_match(block_text, 'Viktor Orbán'))
                                                               -- returned 0
  FROM corpus.content_block;

-- POINT OUT: the candidate filter gives the SAME 179 blocks for both orderings,
-- because as a bag of words they are identical. The exact layer separates them
-- completely: 178 and 0. That is the two-stage architecture in one result.


-- ============================================================================
-- DEMO 6 - FULL PARAGRAPH SEARCH                        *** SLOW DOWN HERE ***
-- ============================================================================
-- SAY: "I am not giving it a keyword now. I am giving it an entire paragraph
--       that already exists somewhere in the archive, and asking the engine to
--       find where it came from."
--
-- The paragraph, from bama.hu, 18 November 2023, article 5,
-- 'Tízezrek demonstráltak a Hamász által fogvatartott túszokért Izraelben':
--
--   "New York vasútállomása, a Penn Station bejáratát zárták el a tűzszünetet
--    követelő tüntetők, akik között feltűnt és beszédet mondott Susan Sarandon,
--    Oscar-díjas színésznő."
--
-- TECHNICAL: corpus.search_query() splits the paragraph into one tsquery per
-- term and ANDs them all. Out of 10,589 blocks exactly one satisfies all of
-- them. The matching unit is the CONTENT BLOCK - the paragraph - not the
-- article.
-- ============================================================================
SELECT b.id AS block_id, b.article_id, b.block_index, b.block_type, b.xpath
  FROM corpus.content_block b
 WHERE b.text_tsv @@ corpus.search_query(
   'New York vasútállomása, a Penn Station bejáratát zárták el a tűzszünetet '
   'követelő tüntetők, akik között feltűnt és beszédet mondott Susan Sarandon, '
   'Oscar-díjas színésznő.')
   AND corpus.phrase_match(b.block_text,
   'New York vasútállomása, a Penn Station bejáratát zárták el a tűzszünetet '
   'követelő tüntetők, akik között feltűnt és beszédet mondott Susan Sarandon, '
   'Oscar-díjas színésznő.');
-- returned exactly 1 row: block 35, article 5, index 9, paragraph,
--                         /html/body/article/div/p[5]

-- A DEFECT THIS DEMO ITSELF FOUND, worth telling as a story:
-- the first paragraph I tried returned NOTHING. The cause was 'ket' with an
-- accent - "two", one of the commonest words in Hungarian - which could not
-- find itself. The index stores its two-character accented lemma; migration 010
-- refuses to put a lemma that short into a query because it is a collision
-- magnet; and the accent policy blocked the folded fallback. Every branch
-- declined.
--
-- Because search_query ANDs every term of a paragraph, ONE unmatchable word made
-- the whole paragraph unfindable. Migration 023 fixed it. This is the paragraph
-- that used to fail, and it now finds itself:
SELECT b.id AS block_id, b.article_id, b.xpath
  FROM corpus.content_block b
 WHERE b.id = 300
   AND b.text_tsv @@ corpus.search_query(b.block_text);        -- returns block 300

SELECT 'két'  AS word, corpus.search_query('két')::text AS query_now,
       corpus.search_vector('két évtizede')  @@ corpus.search_query('két')  AS finds_itself
UNION ALL
SELECT 'idén', corpus.search_query('idén')::text,
       corpus.search_vector('idén nyáron') @@ corpus.search_query('idén');
-- both true now, both false before 023. On this corpus the fix took 'két' from
-- 0 to 52 articles and 'idén' from 0 to 24.
--
-- SAY: "Building this demonstration found a real bug. The fix is migration 023
--       and its regression test is in the suite. I did not learn about it from
--       a user complaint - I learned about it from trying to demo it."


-- ============================================================================
-- DEMO 7 - PARTIAL PARAGRAPH SEARCH                     *** AND HERE ***
-- ============================================================================
-- SAY: "Now the realistic case. A journalist remembers one fragment of a
--       sentence. Not the headline, not the outlet, not the date. Just a few
--       words they half-remember."
--
-- TECHNICAL: the same two-stage pipeline, with fewer terms. Fewer terms means a
-- broader candidate set, and phrase_match then enforces that the fragment
-- appears contiguously.
-- ============================================================================
SELECT b.id AS block_id, b.article_id, b.block_index, b.xpath
  FROM corpus.content_block b
 WHERE b.text_tsv @@ corpus.search_query('tűzszünetet követelő tüntetők')
   AND corpus.phrase_match(b.block_text, 'tűzszünetet követelő tüntetők');
-- returned exactly 1 row: block 35 - the same paragraph, from three words

SELECT b.id AS block_id, b.article_id, b.xpath
  FROM corpus.content_block b
 WHERE b.text_tsv @@ corpus.search_query('a Penn Station bejáratát')
   AND corpus.phrase_match(b.block_text, 'a Penn Station bejáratát');
-- returned exactly 1 row: block 35 - a different fragment, same paragraph

-- POINT OUT: three words out of the middle of a paragraph resolve to that exact
-- paragraph, in one article out of a thousand. That is the transition from
-- SEARCH to EVIDENCE RETRIEVAL.


-- ============================================================================
-- DEMO 8 - THE MATCH IS A PARAGRAPH, NOT AN ARTICLE
-- ============================================================================
-- SAY: "Here is what the engine actually hands back. Not 'article 5 is
--       relevant' - the exact paragraph, its position in the document, and the
--       article it belongs to."
--
-- TECHNICAL: content_block is the citable unit. Article 5 has 109 blocks; the
-- search returned one of them.
-- ============================================================================
SELECT b.id            AS block_id,
       b.block_index,
       b.block_type,
       b.xpath,
       b.block_text,
       a.id            AS article_id,
       a.outlet,
       a.published_at::date,
       a.title,
       a.source_url,
       (SELECT count(*) FROM corpus.content_block x WHERE x.article_id = a.id)
                       AS blocks_in_this_article                -- 109
  FROM corpus.content_block b
  JOIN corpus.article a ON a.id = b.article_id
 WHERE b.id = 35;


-- ============================================================================
-- DEMO 9 - THE EVIDENCE CHAIN: WHERE THE PASSAGE LIVES
-- ============================================================================
-- SAY: "This is the part that makes Causalia different from a search box. The
--       result is not a link. It is a character range inside a named element of
--       an archived document, and it can be resolved again later."
--
-- TECHNICAL: the selector is built from the block's XPath plus the character
-- offsets of the matched fragment within block_text. quote_exact is the
-- authority - the XPath is positional and can drift if the page is re-extracted,
-- so the quote is what proves the citation still points at the same words.
-- ============================================================================
SELECT b.article_id,
       b.id                                              AS content_block_id,
       b.xpath                                           AS selector_xpath,
       position('tűzszünetet követelő tüntetők' in b.block_text) - 1
                                                         AS quote_start,   -- 61
       position('tűzszünetet követelő tüntetők' in b.block_text) - 1
         + length('tűzszünetet követelő tüntetők')       AS quote_end,     -- 90
       substring(b.block_text
                 from position('tűzszünetet követelő tüntetők' in b.block_text)
                 for  length('tűzszünetet követelő tüntetők'))
                                                         AS quote_exact,
       left(b.block_text,
            position('tűzszünetet követelő tüntetők' in b.block_text) - 1)
                                                         AS quote_prefix
  FROM corpus.content_block b
 WHERE b.id = 35;

-- The same shape, as the schema actually stores and serves it. This is the W3C
-- Web Annotation selector model, emitted by the corpus.passage_selector view.
SELECT p.article_id, p.content_block_id, p.resolution_status,
       jsonb_pretty(s.selector) AS selector
  FROM corpus.passage_reference p
  JOIN corpus.passage_selector s ON s.id = p.id;
-- returned 1 row - the evaluation corpus holds one stored citation, minted by
-- scripts/trace_citation.py. SAY: "The demo above DERIVES a selector live; this
-- is one that was stored and can be re-resolved against the archive."


-- ============================================================================
-- DEMO 9b - SEARCHING ONE FIELD: TITLE ONLY
-- ============================================================================
-- SAY: "The article vector is weighted - title is A, subtitle and description B,
--       authors and tags C. So I can ask a narrower question: not 'which
--       articles MENTION the government' but 'which articles are ABOUT it', as
--       judged by the headline."
--
-- TECHNICAL: ts_filter() keeps only the lexemes carrying the given weights, so
-- this is field-scoped retrieval with no extra column and no extra index. The
-- weights were assigned when the generated column was defined.
-- ============================================================================
SELECT 'anywhere in the article' AS scope, count(*) AS articles   -- returned 94
  FROM corpus.article WHERE search_tsv @@ corpus.search_query('kormány')
UNION ALL
SELECT 'title only  (weight A)', count(*)                         -- returned 24
  FROM corpus.article
 WHERE ts_filter(search_tsv, '{a}') @@ corpus.search_query('kormány')
UNION ALL
SELECT 'tags and byline only  (weight C)', count(*)               -- returned 14
  FROM corpus.article
 WHERE ts_filter(search_tsv, '{c}') @@ corpus.search_query('kormány');

SELECT id, outlet, published_at::date, title
  FROM corpus.article
 WHERE ts_filter(search_tsv, '{a}') @@ corpus.search_query('kormány')
 ORDER BY published_at DESC NULLS LAST, id
 LIMIT 3;

-- POINT OUT: 94 anywhere, 24 in the headline. The narrower question is usually
-- the one a researcher wants, and it costs no extra storage to ask.
--
-- NOT SEARCHABLE, and worth saying: outbound LINKS are not indexed at all.
-- corpus.article_link holds 1,183 rows with anchor text, context and their own
-- selector and quote columns - so a link is CITABLE but not FINDABLE. Same for
-- article_video (366 rows). Both are deliberate gaps, not oversights, and both
-- would need a migration to close.
SELECT count(*) AS links, count(anchor_text) AS with_anchor_text,
       count(quote_exact) AS with_quote,
       (SELECT count(*) FROM information_schema.columns
         WHERE table_schema='corpus' AND table_name='article_link'
           AND data_type='tsvector') AS tsvector_columns   -- 0: unsearchable
  FROM corpus.article_link;


-- ============================================================================
-- DEMO 10 - FILTERS COMPOSE WITH THE TEXT QUERY
-- ============================================================================
-- SAY: "Lexical retrieval and metadata constraints are independent, and they
--       compose. The filters are EXACT - they are predicates, never full text."
--
-- TECHNICAL: tags and authors are arrays with GIN indexes, so membership is an
-- index lookup, not a text match. That distinction matters: a full-text match on
-- a tag name would also fire on body prose, which is the wrong answer for a
-- filter.
-- ============================================================================
SELECT a.id, a.outlet, a.published_at::date, a.tags, a.title
  FROM corpus.article a
 WHERE a.search_tsv @@ corpus.search_query('kormány')
   AND a.outlet = 'feol.hu'
   AND a.tags @> ARRAY['Orbán Viktor']::text[]
 ORDER BY a.published_at DESC NULLS LAST, a.id;
-- returned 4 rows

-- Add a date range and a byline. Each predicate only ever narrows.
SELECT count(*) AS with_text_only                              -- 94
  FROM corpus.article WHERE search_tsv @@ corpus.search_query('kormány')
UNION ALL
SELECT count(*) FROM corpus.article
 WHERE search_tsv @@ corpus.search_query('kormány')
   AND published_at >= '2022-01-01' AND published_at < '2023-01-01'
UNION ALL
SELECT count(*) FROM corpus.article
 WHERE search_tsv @@ corpus.search_query('kormány')
   AND authors @> ARRAY['MW']::text[];                         -- 25


-- ============================================================================
-- DEMO 11 - PAGINATION
-- ============================================================================
-- SAY: "Page two exists, and it does not repeat or skip a row."
--
-- TECHNICAL: pagination is only meaningful over a total order, so the ORDER BY
-- ends with a.id as a deterministic last resort. Without it, articles sharing a
-- publication date sit in unspecified order and a page boundary can drop a row
-- or show it twice.
-- ============================================================================
SELECT 1 AS page, id, published_at::date, outlet, left(title,50) AS title
  FROM corpus.article
 WHERE search_tsv @@ corpus.search_query('kormány')
 ORDER BY published_at DESC NULLS LAST, id
 LIMIT 5 OFFSET 0;

SELECT 2 AS page, id, published_at::date, outlet, left(title,50) AS title
  FROM corpus.article
 WHERE search_tsv @@ corpus.search_query('kormány')
 ORDER BY published_at DESC NULLS LAST, id
 LIMIT 5 OFFSET 5;
-- the two pages returned disjoint id sets


-- ============================================================================
-- DEMO 12 - EDGE CASES THE TESTS ALREADY PIN
-- ============================================================================
-- SAY: "Hungarian compounds and dashes are where a search engine quietly gets
--       things wrong. These behaviours are decided on purpose and pinned by
--       tests, so the tokeniser cannot change the product's meaning by accident."
-- ============================================================================

-- Closed compounds: 'koronavírusteszt' answers 'koronavírus' on the SEARCH path
-- (migration 015, a surface prefix of 7+ characters) but NOT on the phrase path,
-- where the compound is a single token. Two different questions, two answers.
SELECT corpus.search_vector('a koronavírusteszt eredménye')
         @@ corpus.search_query('koronavírus')          AS compound_on_search_path,  -- true
       corpus.phrase_match('a koronavírusteszt eredménye', 'koronavírus')
                                                        AS compound_on_phrase_path;  -- false

-- A hyphen and an en dash are currently DIFFERENT words. This is a pinned
-- decision, not an accident - and one the team may choose to reverse.
SELECT corpus.phrase_match('az orosz-ukrán háború kitört', 'orosz-ukrán háború') AS hyphen,   -- true
       corpus.phrase_match('az orosz–ukrán háború kitört', 'orosz-ukrán háború') AS en_dash;  -- false

-- Migration 022: an accent-free query crossing a Hungarian function word still
-- reaches accented text. Before 022 every branch declined this.
SELECT corpus.phrase_match('a felek között van a vita', 'kozott van') AS crosses_a_stopword;  -- true


-- ============================================================================
-- DEMO 13 - AN UNSUPPORTED QUERY, REFUSED RATHER THAN GUESSED
-- ============================================================================
-- SAY: "I do not support boolean operators yet. What matters more is that I do
--       not silently reinterpret them as something else."
--
-- In SQL you can see WHY it would be wrong - 'or' becomes an ordinary required
-- word, and the conjunction becomes unsatisfiable:
SELECT corpus.search_query('kormány OR Ukrajna')::text AS what_it_would_mean;
--   'kormány':* & ( 'or' | 'or' ) & ( 'ukrajn' | 'ukrajna':* )

SELECT count(*) AS articles_returned                           -- returned 0
  FROM corpus.article
 WHERE search_tsv @@ corpus.search_query('kormány OR Ukrajna');

-- NOTE FOR THE DEMO: the REFUSAL lives one layer above SQL, in
-- scripts/search.py, which rejects operator-shaped input and exits with a
-- message. From the psql console you see the underlying behaviour; from the
-- application you get an error. Show the application behaviour if you have a
-- terminal to hand:
--
--     $ scripts/search.py "kormány -Ukrajna"
--     error: this query uses syntax the search engine does not implement:
--       - '-Ukrajna' does not exclude anything - the leading '-' is a term
--         separator, so this REQUIRES 'Ukrajna' instead of excluding it
--
-- SAY: "For a research system an explicit unsupported-query error is better than
--       confidently returning the wrong corpus. That '-' case is real: it used
--       to return exactly the articles the reader was trying to exclude."


-- ============================================================================
-- MENTOR QUESTIONS - SHORT ANSWERS
-- ============================================================================
--
-- Q. Why PostgreSQL rather than Elasticsearch?
-- A. The corpus is already relational - articles, extractions, blocks, images,
--    citations, with foreign keys that must hold. Full-text search in Postgres
--    means retrieval and evidence live in one transactional store, so a search
--    result and the passage it points at cannot drift apart. No second system to
--    keep in sync, and no reindexing pipeline. The deliberate bet is to find out
--    how far conventional search goes before adding anything.
--
-- Q. Why GIN?
-- A. It is the inverted index for tsvector: lexeme -> list of rows. Measured on
--    this corpus, the candidate filter answers in 0.15-7 ms while the exact
--    layer costs 300-3,000 ms over the same blocks. It is 200-2,300x cheaper
--    than what it protects.
--
-- Q. Why not trust GIN alone?
-- A. Because it answers a bag-of-words question. The positions in the stored
--    vector are an artefact of how the vector is assembled - lemma and surface
--    forms interleaved - so a positional operator over it can fabricate an
--    adjacency the text never contained.
--
-- Q. So why exact phrase matching?
-- A. corpus.phrase_match recomputes vectors from block_text at query time, where
--    positions are true document order. It is the correctness layer; GIN is the
--    speed layer. The invariant is that the filter may over-produce and must
--    never under-produce - and that is asserted by a test, and was verified
--    against an independent reference implementation over 120 phrases.
--
-- Q. How does Hungarian morphology work here?
-- A. Every word is indexed twice: a stemmed lemma and the literal surface form,
--    each accent-folded, plus an accent-preserving lemma. An inflected query
--    reaches the base form through the lemma; an accent-free query reaches the
--    accented text through the folded form; an accented query is held to the
--    accented lexeme so 'kór' does not answer 'kor'.
--
-- Q. How are citations represented?
-- A. W3C Web Annotation selectors: an XPath naming the element, refined by a
--    TextPositionSelector giving character offsets, plus the exact quote with
--    prefix and suffix. The quote is the authority - the XPath is positional and
--    drifts if the document is re-extracted, so re-resolution checks the quote.
--
-- Q. Why content_block rather than whole article text?
-- A. Because a citation has to point at a paragraph, not a document. The block
--    carries its own XPath, so a hit resolves to a location in the archived page
--    rather than to a URL. It is also what makes partial-paragraph search useful.
--
-- Q. How does partial-paragraph search work?
-- A. Identically to any other query - the fragment becomes a small conjunction,
--    the GIN filter produces candidate blocks, and phrase_match enforces that
--    the words are contiguous. Nothing special was built for it; it falls out of
--    the block being the unit of retrieval.
--
-- Q. And full-paragraph search?
-- A. The same, with more terms - which makes it more selective, not less. One
--    paragraph out of 10,589 satisfied all of them. It does not work for every
--    paragraph: see the 'két' caveat in Demo 6.
--
-- Q. What happens with unsupported operators?
-- A. The application refuses them with an explanation. Previously it searched
--    for them as ordinary words, which silently returned the wrong corpus - and
--    for '-' it returned precisely the articles the reader wanted excluded.
--
-- Q. What is the biggest scaling bottleneck?
-- A. The runtime phrase_match, and it is measured rather than guessed. It costs
--    ~20 microseconds per candidate block, and LIMIT does not bound it: the plan
--    is Limit <- Sort <- Bitmap Heap Scan, so the scan completes before the sort
--    and asking for three results costs what asking for all of them costs.
--
-- Q. What happens at millions of blocks?
-- A. Measured at two corpus sizes - 10,589 blocks and 146,558 - candidate counts
--    grow linearly and latency grows 9-21x for a 13.8x corpus. Extrapolated:
--    ~0.9 s at 500k blocks, ~1.9 s at 1M, ~19 s at 10M. The full archive is ~57M
--    blocks, which is ~107 s and therefore not usable interactively. That is the
--    open architectural question, and the honest answer is that the corpus is
--    not ingested yet precisely because of it.
--
-- ============================================================================
