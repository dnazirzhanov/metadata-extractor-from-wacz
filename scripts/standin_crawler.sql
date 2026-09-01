-- =====================================================================
-- Crawler stand-in tables. NOT the crawler's schema — a minimal stand-in.
-- =====================================================================
-- The corpus schema has exactly one outbound foreign key,
-- corpus.article.url_hash -> urls(url_hash) ON DELETE RESTRICT. On milab2 that
-- table is the crawler's and already populated. On a development or throwaway
-- database it has to exist, or migration 002 fails outright.
--
-- These four tables carry only the columns the FK and the ingestion path
-- actually touch. They are a stand-in so the constraint can be EXERCISED rather
-- than avoided; they are not a copy of production DDL and must never be treated
-- as one.
--
-- public.videos exists here for one reason: it is the name collision that made
-- the corpus schema separate in the first place, so the dev database should
-- reproduce it.
-- =====================================================================

CREATE TABLE IF NOT EXISTS urls (
    url_hash TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, outlet TEXT NOT NULL,
    lastmod TIMESTAMPTZ, sitemap_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS archives (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    url_hash TEXT NOT NULL REFERENCES urls(url_hash) ON DELETE CASCADE,
    outlet TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'in_progress'
           CHECK (status IN ('in_progress','success','failed')),
    wacz_path TEXT, wacz_sha256 TEXT, wacz_size_bytes BIGINT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS videos (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY);

-- The crawler's own migration ledger. corpus.schema_migrations is deliberately
-- separate so version numbers cannot collide; this one exists to prove that.
CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY,
                                applied_at TIMESTAMPTZ DEFAULT now());
