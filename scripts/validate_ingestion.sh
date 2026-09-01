#!/usr/bin/env bash
# End-to-end schema validation: apply the migrations to a throwaway Postgres,
# ingest real extractor output, and assert the extractor's guarantees still hold
# once the data is in the database.
#
# Nothing here touches milab2 or any real database.
#
# Usage: scripts/validate_ingestion.sh [<extraction-output-dir>]
set -uo pipefail

CONTAINER=cx-pg-ingest
IMAGE=postgres:16
PORT=55432
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SAMPLE="${1:-$REPO/../../tmp/final}"
PY="$REPO/.venv/bin/python"

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

[ -d "$SAMPLE" ] || { echo "no such sample directory: $SAMPLE" >&2; exit 1; }

echo "== throwaway $IMAGE on 127.0.0.1:$PORT"
docker run -d --name "$CONTAINER" -p "$PORT:5432" \
    -e POSTGRES_PASSWORD=validate -e POSTGRES_USER=causalia -e POSTGRES_DB=causalia \
    "$IMAGE" >/dev/null
for _ in $(seq 1 40); do
    docker exec "$CONTAINER" pg_isready -U causalia -d causalia >/dev/null 2>&1 && break
    sleep 1
done

PSQL=(docker exec -i "$CONTAINER" psql -U causalia -d causalia -v ON_ERROR_STOP=1 -q)

echo "== crawler stand-in tables"
"${PSQL[@]}" <<'SQL'
CREATE TABLE urls (
    url_hash TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, outlet TEXT NOT NULL,
    lastmod TIMESTAMPTZ, sitemap_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE archives (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    url_hash TEXT NOT NULL REFERENCES urls(url_hash) ON DELETE CASCADE,
    outlet TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'in_progress'
           CHECK (status IN ('in_progress','success','failed')),
    wacz_path TEXT, wacz_sha256 TEXT, wacz_size_bytes BIGINT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE videos (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY);
CREATE TABLE schema_migrations (version TEXT PRIMARY KEY,
                                applied_at TIMESTAMPTZ DEFAULT now());
SQL

echo "== migrations"
for f in "$REPO"/migrations/[0-9]*.sql; do
    printf "   %-30s " "$(basename "$f")"
    if out=$("${PSQL[@]}" < "$f" 2>&1); then echo "OK"
    else echo "FAILED"; echo "$out" | head -20; exit 1; fi
done

echo "== ingesting $SAMPLE"
CX_VALIDATE_DSN="host=127.0.0.1 port=$PORT user=causalia password=validate dbname=causalia" \
    "$PY" "$REPO/scripts/validate_ingestion.py" "$SAMPLE"
