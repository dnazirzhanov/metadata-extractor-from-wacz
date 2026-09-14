#!/usr/bin/env bash
# Make the staging database realistic on the DB side.
#
# The 1k rung proved correctness against 1,200 crawler rows. A scaling
# measurement needs the real thing: mandiner's full urls and archives, so FK
# lookups resolve against a full-size index and the ingest numbers are
# comparable with the 2026-09-10 benchmark, which did the same.
#
# The 1,200 rows already staged are KEPT, not reloaded: corpus.extraction_task
# references their archives.id, and reloading would renumber them.
set -euo pipefail

STAGE=/mnt/hdd/c0cshf/p0-stage-20260911
TMP="$STAGE/tmp"
mkdir -p "$TMP"

src() { docker exec causalia-final-db-1 psql -U causalia -d causalia -q "$@"; }
dst() { docker exec -i cx-pg-stage psql -U causalia -d causalia_stage -q -v ON_ERROR_STOP=1 "$@"; }

echo "== dumping mandiner urls"
# SELECT * , not a column list: the crawler's urls table carries columns this
# repo's stand-in never needed (updated_at is NOT NULL, among others), and
# CREATE TABLE LIKE copies those constraints. Naming seven columns failed on the
# first row. Staging's DDL came from production, so the order matches.
src -c "\copy (SELECT * FROM urls WHERE outlet='mandiner.hu') TO STDOUT" > "$TMP/urls.tsv"
wc -l < "$TMP/urls.tsv"

echo "== dumping mandiner successful archives"
src -c "\copy (SELECT url_hash,outlet,status,wacz_path,wacz_sha256,wacz_size_bytes,doc_http_status,started_at,finished_at,browsertrix_version FROM archives WHERE outlet='mandiner.hu' AND status='success') TO STDOUT" > "$TMP/archives.tsv"
wc -l < "$TMP/archives.tsv"

echo "== loading urls"
dst -c "DROP TABLE IF EXISTS urls_in; CREATE UNLOGGED TABLE urls_in (LIKE urls)"
dst -c "\copy urls_in FROM STDIN" < "$TMP/urls.tsv"
dst -c "INSERT INTO urls SELECT * FROM urls_in ON CONFLICT (url_hash) DO NOTHING"

echo "== loading archives, skipping url_hashes already staged"
dst -c "DROP TABLE IF EXISTS archives_in; CREATE UNLOGGED TABLE archives_in (url_hash text, outlet text, status text, wacz_path text, wacz_sha256 text, wacz_size_bytes bigint, doc_http_status text, started_at timestamptz, finished_at timestamptz, browsertrix_version text)"
dst -c "\copy archives_in FROM STDIN" < "$TMP/archives.tsv"
dst -c "INSERT INTO archives (url_hash,outlet,status,wacz_path,wacz_sha256,wacz_size_bytes,doc_http_status,started_at,finished_at,browsertrix_version)
        SELECT i.* FROM archives_in i
         WHERE NOT EXISTS (SELECT 1 FROM archives a WHERE a.url_hash = i.url_hash)"

dst -c "DROP TABLE urls_in; DROP TABLE archives_in"
dst -c "ANALYZE urls"
dst -c "ANALYZE archives"
rm -f "$TMP/urls.tsv" "$TMP/archives.tsv"
dst -tAc "SELECT (SELECT count(*) FROM urls) AS urls, (SELECT count(*) FROM archives) AS archives"
