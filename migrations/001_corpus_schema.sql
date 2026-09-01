-- =====================================================================
-- 001  The corpus schema, its migration ledger, and Hungarian search
-- =====================================================================
-- Everything this project adds lives in the `corpus` schema, never in
-- `public`. Three reasons, all concrete:
--
--   * `public.videos` already exists (the yt-dlp backfill table). A separate
--     schema makes that class of collision impossible rather than something to
--     remember.
--   * `corpus` can be granted, dumped and restored independently of the
--     crawler's tables.
--   * `DROP SCHEMA corpus CASCADE` is a complete, safe rollback while this is
--     still being developed. Nothing in `public` is touched by it.
--
-- The ledger is `corpus.schema_migrations`, deliberately NOT the crawler's
-- `public.schema_migrations`. Two independent version sequences cannot collide,
-- and applying these files never writes to a table the crawler reads.
--
-- Idempotent: every statement is IF NOT EXISTS or guarded, so `psql -f` twice
-- is a no-op. Applied by scripts/migrate.sh, or by hand with psql.
-- =====================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS corpus;

CREATE TABLE IF NOT EXISTS corpus.schema_migrations (
    version     text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Hungarian full-text search
-- ---------------------------------------------------------------------
-- Measured on this database before choosing: the stock `hungarian` config
-- stems real agglutination correctly, which is the whole ballgame for this
-- corpus.
--
--   to_tsvector('hungarian', 'Zrínyire a japánok is felnéznek, az 1566-os
--                             csatában tanúsított hősiessége')
--     -> 'zríny':1 'japán':3 'is':4 'felnéz':5 '1566':7 'os':8 'csat':9
--        'tanúsítot':10 'hősiesség':11
--
--   to_tsvector('simple', 'Zrínyire a japánok is felnéznek')
--     -> 'zrínyire':1 'a':2 'japánok':3 'is':4 'felnéznek':5     -- no stemming
--
-- But `hungarian` PRESERVES ACCENTS ('zríny', not 'zriny'), so a journalist on
-- a non-Hungarian keyboard typing "Zrinyi" would match nothing. `unaccent` in
-- front of the stemmer fixes that. It is available on this image and requires
-- superuser, which the `causalia` role has (checked: rolsuper = t).
CREATE EXTENSION IF NOT EXISTS unaccent;

-- CREATE TEXT SEARCH CONFIGURATION has no IF NOT EXISTS, hence the guard.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_ts_config c
                   JOIN pg_namespace n ON n.oid = c.cfgnamespace
                   WHERE c.cfgname = 'hungarian_ci' AND n.nspname = 'corpus') THEN
        EXECUTE 'CREATE TEXT SEARCH CONFIGURATION corpus.hungarian_ci
                 ( COPY = pg_catalog.hungarian )';
        EXECUTE 'ALTER TEXT SEARCH CONFIGURATION corpus.hungarian_ci
                 ALTER MAPPING FOR hword, hword_part, word
                 WITH unaccent, hungarian_stem';
    END IF;
END
$$;

-- ---------------------------------------------------------------------
-- An IMMUTABLE way to flatten text[] for the search vectors
-- ---------------------------------------------------------------------
-- `array_to_string` is declared STABLE, not IMMUTABLE, because in general it
-- depends on the element type's output function. A STORED generated column
-- requires a strictly IMMUTABLE expression, so using it directly fails with
-- "generation expression is not immutable" - found by applying these migrations
-- to a throwaway database rather than by reading the docs.
--
-- Narrowed to text[] with a constant separator the operation genuinely is
-- deterministic: text's output function is the identity. So this wrapper is a
-- correct IMMUTABLE declaration for this concrete type, not a lie to get past
-- the planner. It must not be generalised to anyarray.
CREATE OR REPLACE FUNCTION corpus.text_array_to_string(arr text[])
RETURNS text
LANGUAGE sql
IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT array_to_string(arr, ' ') $$;

COMMENT ON FUNCTION corpus.text_array_to_string(text[]) IS
    'IMMUTABLE text[] flattener for use in STORED generated tsvector columns. '
    'array_to_string is only STABLE. Correct for text[]; do not widen to anyarray.';

-- ---------------------------------------------------------------------
-- A WARNING that belongs in the schema, not just the design doc
-- ---------------------------------------------------------------------
-- `corpus.hungarian_ci` is referenced BY NAME from the STORED generated
-- tsvector columns in 002 and 003. A stored generated column is NOT recomputed
-- when the configuration changes, so `ALTER TEXT SEARCH CONFIGURATION` after
-- data exists silently desynchronises every vector from its own text.
--
-- Changing it is therefore a migration, not a tweak: drop the generated
-- columns, alter the config, re-add the columns, rebuild the GIN indexes.
COMMENT ON SCHEMA corpus IS
    'Causalia extracted article corpus. Search config corpus.hungarian_ci is '
    'referenced by stored generated columns - altering it requires rebuilding '
    'them (see migrations/001).';

INSERT INTO corpus.schema_migrations (version) VALUES ('001')
    ON CONFLICT (version) DO NOTHING;

COMMIT;
