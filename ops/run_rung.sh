#!/usr/bin/env bash
# One rung of the ladder: extract N, then load them, with the box sampled
# throughout and every phase timed separately.
#
#   ops/run_rung.sh 10000 12          N articles, W workers
#
# Extraction and ingestion are timed SEPARATELY and deliberately not overlapped.
# They contend for the same rotational device, and the question this rung has to
# answer is where throughput goes as the corpus grows - which is unanswerable if
# the two phases are mixed.
set -euo pipefail

N=${1:-10000}
WORKERS=${2:-12}
LOADERS=${LOADERS:-4}
STAGES=${STAGES:-content,screenshot}
STAGE=/mnt/hdd/c0cshf/p0-stage-20260911
OUT=$STAGE/out
RUN=$STAGE/rung-$N-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$RUN"
PY=~/causalia-article-extractor/.venv/bin/python
export CX_INGEST_DSN="host=127.0.0.1 port=55447 user=causalia password=stage dbname=causalia_stage"
export PYTHONPATH=$HOME/causalia-p0/src:$HOME/causalia-p0
cd ~/causalia-p0

psqlq() { docker exec cx-pg-stage psql -U causalia -d causalia_stage -tAc "$1"; }

echo "== rung: $N articles, $WORKERS workers, stages=$STAGES"
echo "== run dir: $RUN"

# Drain before the clock starts: a phase measured on top of the previous one's
# dirty pages is measuring the previous one.
drain() {
    sync
    local dirty=0
    for _ in $(seq 1 100); do
        dirty=$(awk '/^Dirty:/{print $2}' /proc/meminfo)
        [ "$dirty" -lt 65536 ] && break
        sleep 3
    done
    echo "   drained: dirty=${dirty}kB"
}

drain
# SKIP_DU: the sampler must not walk tens of thousands of article directories
# while it is sampling the thing that is writing them.
SKIP_DU=1 OUT_DIR=$OUT INTERVAL=10 ops/sample.sh > "$RUN/samples.jsonl" 2>/dev/null &
SAMPLER=$!
trap 'kill $SAMPLER 2>/dev/null || true' EXIT

wal_before=$(psqlq "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint")
db_before=$(psqlq "SELECT pg_database_size(current_database())")
out_before=$(du -sb "$OUT" | cut -f1)

per_worker=$(( (N + WORKERS - 1) / WORKERS ))
echo "== extraction: $WORKERS workers x $per_worker"
t0=$(date +%s.%N)
pids=()
for w in $(seq 1 "$WORKERS"); do
    $PY -m ingest.extract_worker --outlet mandiner.hu --output "$OUT" \
        --limit "$per_worker" --batch 8 --once --stages "$STAGES" \
        --log-level WARNING > "$RUN/extract-$w.log" 2>&1 &
    pids+=($!)
done
# WAIT FOR THE WORKERS, NOT FOR EVERY CHILD. A bare `wait` also waits for the
# sampler, which loops until it is killed - so the first version of this script
# sat at this line for as long as it was left alone after extraction finished,
# and never reached the ingestion phase.
wait "${pids[@]}"
t1=$(date +%s.%N)
extract_s=$(echo "$t1 - $t0" | bc)
echo "   extraction wall ${extract_s}s"

echo "== ingestion: $LOADERS parallel loaders"
# PARALLEL, because ingestion is round-trip-latency bound, not resource bound:
# a single loader leaves the box 96% idle and Postgres at 0.4 of one core, and
# four measured 52.32 articles/s against 13.79 - 3.8x, with no code change,
# because the ledger claim already makes them disjoint.
#
# Limited to this rung's N in total. The ledger may hold a larger backlog from
# earlier measurements, and loading that here would answer a different question.
drain
per_loader=$(( (N + LOADERS - 1) / LOADERS ))
t2=$(date +%s.%N)
lpids=()
for l in $(seq 1 "$LOADERS"); do
    $PY -m ingest.load --outlet mandiner.hu --output "$OUT" --once \
        --limit "$per_loader" --log-level WARNING > "$RUN/load-$l.log" 2>&1 &
    lpids+=($!)
done
# `|| true`: a loader exits 1 when any article failed, and under `set -e` that
# killed the script before it could write the summary - which is when the
# summary matters most. The failures are reported below either way.
wait "${lpids[@]}" || true
t3=$(date +%s.%N)
ingest_s=$(echo "$t3 - $t2" | bc)
echo "   ingestion wall ${ingest_s}s"

kill $SAMPLER 2>/dev/null || true
sleep 1

wal_after=$(psqlq "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint")
db_after=$(psqlq "SELECT pg_database_size(current_database())")
out_after=$(du -sb "$OUT" | cut -f1)

{
  echo "rung                $N articles, $WORKERS workers, stages=$STAGES"
  echo "extraction_s        $extract_s"
  echo "ingestion_s         $ingest_s"
  echo "wal_growth_mb       $(echo "scale=1; ($wal_after - $wal_before)/1048576" | bc)"
  echo "db_growth_mb        $(echo "scale=1; ($db_after - $db_before)/1048576" | bc)"
  echo "output_growth_mb    $(echo "scale=1; ($out_after - $out_before)/1048576" | bc)"
  echo "db_total_mb         $(echo "scale=1; $db_after/1048576" | bc)"
  echo "--- ledger ---"
  psqlq "SELECT extract_state||'  '||coalesce(quality,'-')||'  '||ingest_state||'  '||count(*)
           FROM corpus.extraction_task
          GROUP BY extract_state, quality, ingest_state ORDER BY 1"
  echo "--- the invariant ---"
  psqlq "SELECT 'articles '||(SELECT count(*) FROM corpus.article)
             || '  searchable '||(SELECT count(*) FROM corpus.searchable_article)
             || '  current '||(SELECT count(*) FROM corpus.article_extraction WHERE is_current)
             || '  blocks '||(SELECT count(*) FROM corpus.content_block)"
  psqlq "SELECT 'invalid_with_a_row '||(SELECT count(*) FROM corpus.extraction_task t
                  JOIN corpus.article a ON a.url_hash = t.url_hash WHERE t.quality='invalid')
             || '  searchable_without_capture '||(SELECT count(*) FROM corpus.searchable_article v
                  JOIN corpus.article_extraction e ON e.id = v.current_extraction_id
                 WHERE e.archive_row_id IS NULL OR e.wacz_sha256 IS NULL)
             || '  zero_block_current '||(SELECT count(*) FROM corpus.article_extraction
                 WHERE is_current AND content_block_count = 0)"
  echo "--- crawler tables untouched? ---"
  psqlq "SELECT 'urls '||(SELECT count(*) FROM urls)||'  archives '||(SELECT count(*) FROM archives)"
  echo "--- retries and trouble ---"
  psqlq "SELECT 'retried '||count(*) FILTER (WHERE attempts > 1)
             || '  quarantined '||count(*) FILTER (WHERE extract_state='quarantined')
             || '  extract_failed '||count(*) FILTER (WHERE extract_state='failed')
             || '  ingest_failed '||count(*) FILTER (WHERE ingest_state='failed')
           FROM corpus.extraction_task"
  echo "--- rates ---"
  echo "extract_per_s       $(echo "scale=2; $N / $extract_s" | bc)"
  echo "ingest_per_s        $(echo "scale=2; $N / $ingest_s" | bc)"
  echo "ingest_ms_article   $(echo "scale=2; $ingest_s * 1000 / $N" | bc)"
  echo "wal_kb_article      $(echo "scale=1; ($wal_after - $wal_before)/1024/$N" | bc)"
  echo "--- loader tails ---"
  grep -h "^== loaded" "$RUN"/load-*.log
} | tee "$RUN/summary.txt"
