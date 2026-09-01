#!/usr/bin/env bash
# Apply every migration to a THROWAWAY Postgres container, from scratch, twice.
#
# Nothing here touches milab2 or any real database. The container is created and
# destroyed by this script. Applying twice is the point: these migrations must be
# idempotent, so `psql -f` on an already-migrated database has to be a no-op.
#
# Usage: scripts/validate_migrations.sh
set -uo pipefail

CONTAINER=cx-pg-validate
IMAGE=postgres:16
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PSQL=(docker exec -i "$CONTAINER" psql -U causalia -d causalia -v ON_ERROR_STOP=1 -q)

cleanup() { docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo "== starting throwaway $IMAGE"
docker run -d --name "$CONTAINER" \
    -e POSTGRES_PASSWORD=validate -e POSTGRES_USER=causalia -e POSTGRES_DB=causalia \
    "$IMAGE" >/dev/null
for _ in $(seq 1 40); do
    docker exec "$CONTAINER" pg_isready -U causalia -d causalia >/dev/null 2>&1 && break
    sleep 1
done

# The crawler's tables, which corpus.article's FK depends on. Minimal stand-ins
# with the same column types and constraints as migrations 001-008 in
# causalia-final; enough to prove the FK resolves.
echo "== creating crawler stand-in tables (urls, archives, videos)"
"${PSQL[@]}" <<'SQL'
CREATE TABLE urls (
    url_hash TEXT PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    outlet TEXT NOT NULL,
    lastmod TIMESTAMPTZ,
    sitemap_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE archives (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    url_hash TEXT NOT NULL REFERENCES urls(url_hash) ON DELETE CASCADE,
    outlet TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'in_progress'
           CHECK (status IN ('in_progress','success','failed')),
    wacz_path TEXT, wacz_sha256 TEXT, wacz_size_bytes BIGINT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE videos (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY);
CREATE TABLE schema_migrations (version TEXT PRIMARY KEY,
                                applied_at TIMESTAMPTZ DEFAULT now());
SQL
[ $? -eq 0 ] || { echo "FAILED to create stand-in tables"; exit 1; }

apply_all() {
    local pass="$1" f
    for f in "$REPO"/migrations/[0-9]*.sql; do
        printf "   %-30s " "$(basename "$f")"
        if out=$("${PSQL[@]}" < "$f" 2>&1); then
            echo "OK"
        else
            echo "FAILED"
            echo "$out" | head -20
            return 1
        fi
    done
    return 0
}

echo "== pass 1: apply from scratch"
apply_all 1 || exit 1
echo "== pass 2: apply again (must be a no-op)"
apply_all 2 || exit 1

echo "== ledger"
"${PSQL[@]/-q/} " -c "SELECT version, applied_at FROM corpus.schema_migrations ORDER BY version;" \
    2>/dev/null || docker exec -i "$CONTAINER" psql -U causalia -d causalia \
    -c "SELECT version FROM corpus.schema_migrations ORDER BY version;"

echo "== objects created"
docker exec -i "$CONTAINER" psql -U causalia -d causalia -tA <<'SQL'
SELECT 'tables:   ' || count(*) FROM information_schema.tables
  WHERE table_schema='corpus' AND table_type='BASE TABLE';
SELECT 'views:    ' || count(*) FROM information_schema.views WHERE table_schema='corpus';
SELECT 'indexes:  ' || count(*) FROM pg_indexes WHERE schemaname='corpus';
SELECT 'ts_config:' || count(*) FROM pg_ts_config c JOIN pg_namespace n
  ON n.oid=c.cfgnamespace WHERE n.nspname='corpus';
SQL

echo "== rollback is complete"
docker exec -i cx-pg-validate psql -U causalia -d causalia -v ON_ERROR_STOP=1 -q \
    -c "DROP SCHEMA corpus CASCADE;" && echo "   DROP SCHEMA corpus CASCADE: OK"
docker exec -i cx-pg-validate psql -U causalia -d causalia -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" \
    | xargs -I{} echo "   public tables surviving: {} (expected 4)"

echo
echo "ALL MIGRATIONS VALID"
