#!/usr/bin/env bash
# What the deferred screenshot actually costs to collect later.
#
# The architecture note estimated it from the stage breakdown: read_archive is
# 45% of a full extraction's 908 ms, so a screenshot-only pass should be about
# 0.56 of an extraction pass. This measures it instead of estimating it, on the
# same box, at the same concurrency as the A/B, over archives whose screenshot
# stage was genuinely skipped.
set -euo pipefail
STAGE=/mnt/hdd/c0cshf/p0-stage-20260911
OUT=$STAGE/out
WORKERS=${WORKERS:-20}
PER_WORKER=${PER_WORKER:-100}
PY=~/causalia-article-extractor/.venv/bin/python
export CX_INGEST_DSN="host=127.0.0.1 port=55447 user=causalia password=stage dbname=causalia_stage"
export PYTHONPATH=$HOME/causalia-p0/src:$HOME/causalia-p0
cd ~/causalia-p0

sync
for _ in $(seq 1 60); do
    dirty=$(awk '/^Dirty:/{print $2}' /proc/meminfo)
    [ "$dirty" -lt 65536 ] && break
    sleep 3
done
echo "   drained: dirty=${dirty}kB"

before=$(du -sb $OUT | cut -f1)
t0=$(date +%s.%N)
for w in $(seq 1 "$WORKERS"); do
    $PY -m ingest.screenshot_worker --outlet mandiner.hu --output $OUT \
        --limit "$PER_WORKER" --batch 8 --once --log-level WARNING \
        > "$STAGE/backfill-$w.log" 2>&1 &
done
wait
elapsed=$(echo "$(date +%s.%N) - $t0" | bc)
after=$(du -sb $OUT | cut -f1)
n=$((WORKERS * PER_WORKER))
echo "== screenshot backfill"
echo "   archives      $n"
echo "   wall          ${elapsed}s"
echo "   archives/s    $(echo "scale=3; $n / $elapsed" | bc)"
echo "   bytes written $(echo "scale=2; ($after - $before) / 1048576" | bc) MB"
echo "   MB/article    $(echo "scale=3; ($after - $before) / 1048576 / $n" | bc)"
docker exec cx-pg-stage psql -U causalia -d causalia_stage -tAc \
  "SELECT screenshot_state, count(*) FROM corpus.extraction_task GROUP BY 1 ORDER BY 1"
