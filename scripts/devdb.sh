#!/usr/bin/env bash
# The standing DEVELOPMENT database. Not production, not the throwaway harness.
#
# There is no psql on this host and no sudo, so the dev database is a container
# and the isolation is structural rather than a matter of care:
#
#   * its own container name, its own named volume, its own port
#   * port 55433, deliberately NOT 55432 — that one belongs to
#     validate_ingestion.sh's throwaway, which is destroyed on every run
#   * database causalia_dev, so no DSN typed here can reach a database called
#     causalia
#
# The repo is bind-mounted read-only at its OWN absolute path so that
# migrate.sh's `psql -f <host path>` resolves to the same file inside the
# container. That is what lets the existing migration runner drive this database
# with no second migration system.
#
# Usage: scripts/devdb.sh [up|down|status|psql|reset|dsn]
set -uo pipefail

CONTAINER=cx-pg-dev
VOLUME=cx-pg-dev-data
IMAGE=postgres:16
PORT=55433
PGUSER=causalia
PGDB=causalia_dev
PGPASS=dev
REPO="$(cd "$(dirname "$0")/.." && pwd)"

DSN="host=127.0.0.1 port=$PORT user=$PGUSER password=$PGPASS dbname=$PGDB"
PSQL=(docker exec -i "$CONTAINER" psql -U "$PGUSER" -d "$PGDB" -v ON_ERROR_STOP=1 -q)

exists() { docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; }
running() { docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; }

wait_ready() {
    for _ in $(seq 1 60); do
        docker exec "$CONTAINER" pg_isready -U "$PGUSER" -d "$PGDB" >/dev/null 2>&1 && return 0
        sleep 1
    done
    echo "postgres did not become ready" >&2; return 1
}

case "${1:-up}" in
  up)
    if running; then
        echo "== $CONTAINER already running on 127.0.0.1:$PORT"
    elif exists; then
        echo "== starting existing $CONTAINER (volume $VOLUME preserved)"
        docker start "$CONTAINER" >/dev/null
    else
        echo "== creating $CONTAINER on 127.0.0.1:$PORT, volume $VOLUME"
        docker run -d --name "$CONTAINER" \
            -p "127.0.0.1:$PORT:5432" \
            -v "$VOLUME:/var/lib/postgresql/data" \
            -v "$REPO:$REPO:ro" \
            -e POSTGRES_PASSWORD="$PGPASS" \
            -e POSTGRES_USER="$PGUSER" \
            -e POSTGRES_DB="$PGDB" \
            "$IMAGE" >/dev/null || exit 1
    fi
    wait_ready || exit 1
    echo "== crawler stand-in tables (idempotent)"
    "${PSQL[@]}" < "$REPO/scripts/standin_crawler.sql" || exit 1
    echo "== ready"
    echo "   CX_DEV_DSN=\"$DSN\""
    echo "   CX_PSQL=\"docker exec -i $CONTAINER psql -U $PGUSER -d $PGDB\""
    ;;
  down)
    running && docker stop "$CONTAINER" >/dev/null && echo "== stopped $CONTAINER (volume kept)"
    running || echo "== not running"
    ;;
  status)
    exists || { echo "== $CONTAINER does not exist"; exit 0; }
    docker ps -a --filter "name=^${CONTAINER}$" \
        --format '== {{.Names}}  {{.Status}}  {{.Ports}}'
    running || exit 0
    "${PSQL[@]}" -tA -c "SELECT 'server: ' || version()"
    "${PSQL[@]}" -tA -c "SELECT 'corpus: ' || count(*) FILTER (WHERE table_type='BASE TABLE')
                         || ' tables, ' || count(*) FILTER (WHERE table_type='VIEW') || ' view'
                         FROM information_schema.tables WHERE table_schema = 'corpus'"
    "${PSQL[@]}" -tA -c "SELECT 'migrations: ' || string_agg(version, ',' ORDER BY version)
                         FROM corpus.schema_migrations" 2>/dev/null \
        || echo "migrations: (corpus schema not present)"
    ;;
  psql)
    shift
    exec docker exec -it "$CONTAINER" psql -U "$PGUSER" -d "$PGDB" "$@"
    ;;
  reset)
    # Destroys the dev database. Deliberately noisy and deliberately explicit:
    # nothing here is recoverable, and re-ingesting takes seconds.
    printf "This DESTROYS volume %s (the whole dev database). Type 'reset' to confirm: " "$VOLUME"
    read -r answer
    [ "$answer" = "reset" ] || { echo "aborted"; exit 1; }
    docker rm -f "$CONTAINER" >/dev/null 2>&1
    docker volume rm "$VOLUME" >/dev/null 2>&1
    echo "== removed container and volume; run 'up' for a clean database"
    ;;
  dsn) echo "$DSN" ;;
  *) echo "usage: $0 [up|down|status|psql|reset|dsn]" >&2; exit 2 ;;
esac
