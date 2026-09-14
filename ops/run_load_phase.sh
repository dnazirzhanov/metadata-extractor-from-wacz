#!/usr/bin/env bash
# The ingestion half of a rung, measured on its own.
#
# Extraction and ingestion contend for the same rotational device, so they are
# timed separately: a combined number cannot answer where throughput goes as the
# corpus grows, which is the only question a rung exists to answer.
set -euo pipefail
N=${1:-10000}
STAGE=/mnt/hdd/c0cshf/p0-stage-20260911
OUT=$STAGE/out
RUN=${2:-$(ls -dt $STAGE/rung-* | head -1)}
PY=~/causalia-article-extractor/.venv/bin/python
export CX_INGEST_DSN="host=127.0.0.1 port=55447 user=causalia password=stage dbname=causalia_stage"
export PYTHONPATH=$HOME/causalia-p0/src:$HOME/causalia-p0
cd ~/causalia-p0

psqlq() { docker exec cx-pg-stage psql -U causalia -d causalia_stage -tAc "$1"; }

# Drain writeback before the clock starts. The extraction phase leaves ~8 GB of
# dirty pages behind, and an ingest measured on top of that is measuring the
# previous phase - the same trap that made the 2026-09-10 concurrency sweep
# report 32 workers as faster than 24.
sync
for _ in $(seq 1 100); do
    dirty=$(awk '/^Dirty:/{print $2}' /proc/meminfo)
    [ "$dirty" -lt 65536 ] && break
    sleep 3
done
echo "   drained: dirty=${dirty}kB"

# SKIP_DU: the sampler must not walk 25,000 article directories while the
# number being measured is the database's.
SKIP_DU=1 OUT_DIR=$OUT INTERVAL=10 ops/sample.sh > "$RUN/samples-load.jsonl" 2>/dev/null &
SAMPLER=$!
trap 'kill $SAMPLER 2>/dev/null || true' EXIT

wal_before=$(psqlq "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint")
db_before=$(psqlq "SELECT pg_database_size(current_database())")

echo "== ingestion of $N"
t0=$(date +%s.%N)
$PY -m ingest.load --outlet mandiner.hu --output "$OUT" --once --limit "$N" \
    --log-level WARNING > "$RUN/load.log" 2>&1 || true
t1=$(date +%s.%N)
ingest_s=$(echo "$t1 - $t0" | bc)

kill $SAMPLER 2>/dev/null || true
wal_after=$(psqlq "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint")
db_after=$(psqlq "SELECT pg_database_size(current_database())")

{
  echo "ingestion_s       $ingest_s"
  echo "articles_per_s    $(echo "scale=2; $N / $ingest_s" | bc)"
  echo "ms_per_article    $(echo "scale=2; $ingest_s * 1000 / $N" | bc)"
  echo "wal_growth_mb     $(echo "scale=1; ($wal_after - $wal_before)/1048576" | bc)"
  echo "db_growth_mb      $(echo "scale=1; ($db_after - $db_before)/1048576" | bc)"
  echo "db_total_mb       $(echo "scale=1; $db_after/1048576" | bc)"
  echo "--- corpus ---"
  psqlq "SELECT 'articles '||(SELECT count(*) FROM corpus.article)
             || '  searchable '||(SELECT count(*) FROM corpus.searchable_article)
             || '  blocks '||(SELECT count(*) FROM corpus.content_block)
             || '  current '||(SELECT count(*) FROM corpus.article_extraction WHERE is_current)"
  echo "--- crawler tables untouched? ---"
  psqlq "SELECT 'urls '||(SELECT count(*) FROM urls)||'  archives '||(SELECT count(*) FROM archives)"
  echo "--- ledger ---"
  psqlq "SELECT extract_state||'  '||coalesce(quality,'-')||'  '||ingest_state||'  '||count(*)
           FROM corpus.extraction_task GROUP BY extract_state, quality, ingest_state
           ORDER BY 1"
  echo "--- trouble ---"
  psqlq "SELECT 'retried '||count(*) FILTER (WHERE attempts > 1)
             || '  quarantined '||count(*) FILTER (WHERE extract_state='quarantined')
             || '  extract_failed '||count(*) FILTER (WHERE extract_state='failed')
             || '  ingest_failed '||count(*) FILTER (WHERE ingest_state='failed')
           FROM corpus.extraction_task"
  echo "--- load tail ---"
  tail -2 "$RUN/load.log"
} | tee "$RUN/summary-load.txt"
