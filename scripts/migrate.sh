#!/usr/bin/env bash
# Apply the corpus migrations, in order, through psql.
#
# Deliberately a shell script and not a Python runner: the extractor package has
# no database dependency and there is a test asserting it opens no socket. The
# migrations are plain, idempotent SQL, and every environment that can reach this
# database already has psql - on milab2 that is inside the db container.
#
#   scripts/migrate.sh --status                 what has been applied
#   scripts/migrate.sh                          apply anything outstanding
#   scripts/migrate.sh --dry-run                list what would be applied
#
# Connection: standard libpq environment (PGHOST, PGPORT, PGUSER, PGPASSWORD,
# PGDATABASE), or set CX_PSQL to a full command. On milab2:
#
#   CX_PSQL="ssh $MILAB2 cd ~/causalia-final && \
#            docker compose exec -T db psql -U causalia -d causalia" \
#     scripts/migrate.sh --status
#
# Applying to a live database is a deliberate act. This script never runs
# unprompted and has no default host.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-apply}"
PSQL_CMD="${CX_PSQL:-psql}"

run() { $PSQL_CMD -v ON_ERROR_STOP=1 -q "$@"; }
query() { $PSQL_CMD -tA -c "$1" 2>/dev/null; }

applied() {
    query "SELECT version FROM corpus.schema_migrations ORDER BY version" || true
}

case "$MODE" in
  --status)
    echo "applied:"
    got="$(applied)"
    [ -n "$got" ] && echo "$got" | sed 's/^/   /' || echo "   (none - corpus schema not present)"
    echo "on disk:"
    for f in "$REPO"/migrations/[0-9]*.sql; do
        v="$(basename "$f" | cut -d_ -f1)"
        if echo "$got" | grep -qx "$v"; then echo "   $v  $(basename "$f")  applied"
        else echo "   $v  $(basename "$f")  PENDING"; fi
    done
    ;;
  --dry-run|apply)
    got="$(applied)"
    for f in "$REPO"/migrations/[0-9]*.sql; do
        v="$(basename "$f" | cut -d_ -f1)"
        if echo "$got" | grep -qx "$v"; then
            printf "   %-30s already applied\n" "$(basename "$f")"
            continue
        fi
        if [ "$MODE" = "--dry-run" ]; then
            printf "   %-30s WOULD APPLY\n" "$(basename "$f")"
            continue
        fi
        printf "   %-30s applying... " "$(basename "$f")"
        if out=$(run -f "$f" 2>&1); then echo "OK"
        else echo "FAILED"; echo "$out" | head -20; exit 1; fi
    done
    ;;
  *)
    echo "usage: $0 [--status|--dry-run]" >&2; exit 2 ;;
esac
