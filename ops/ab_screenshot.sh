#!/usr/bin/env bash
# A/B: what the screenshot stage costs, measured on this box, today.
#
# The 2026-09-10 benchmark measured 15.206 -> 22.434 archives/s (1.48x) at 24
# workers over 3,000 archives per condition. This replicates it through the
# production worker at a smaller scale, to confirm the stage flag delivers the
# gain rather than assuming it.
#
# METHOD, and why each part is there:
#   * The two conditions claim from the SAME ledger, so their slices are
#     disjoint by construction - the claim guarantees it. The earlier benchmark
#     had to arrange that by hand with pre-drawn pools.
#   * WRITEBACK IS DRAINED BETWEEN CONDITIONS. On this box a benchmark that
#     writes gigabytes and does not drain measures its own predecessor: the
#     first sweep of 2026-09-10 reported 32 workers at 1.82x of 24, which is
#     physically impossible, because 16 GB of dirty pages were still flushing.
#   * WITH-SCREENSHOT RUNS FIRST, so any residual writeback penalises the
#     content-only condition and the measured gain is conservative.
set -euo pipefail

STAGE=/mnt/hdd/c0cshf/p0-stage-20260911
OUT=$STAGE/out
WORKERS=${WORKERS:-8}
PER_WORKER=${PER_WORKER:-250}
PY=~/causalia-article-extractor/.venv/bin/python
export CX_INGEST_DSN="host=127.0.0.1 port=55447 user=causalia password=stage dbname=causalia_stage"
export PYTHONPATH=$HOME/causalia-p0/src:$HOME/causalia-p0
cd ~/causalia-p0

drain() {
    sync
    for _ in $(seq 1 60); do
        dirty=$(awk '/^Dirty:/{print $2}' /proc/meminfo)
        writeback=$(awk '/^Writeback:/{print $2}' /proc/meminfo)
        [ "$dirty" -lt 65536 ] && [ "$writeback" -lt 8192 ] && break
        sleep 3
    done
    echo "   drained: dirty=${dirty}kB writeback=${writeback}kB"
}

condition() {
    local name=$1 stages=$2
    echo "== $name ($stages)"
    drain
    local before_bytes started elapsed after_bytes
    before_bytes=$(du -sb $OUT | cut -f1)
    started=$(date +%s.%N)
    for w in $(seq 1 "$WORKERS"); do
        $PY -m ingest.extract_worker --outlet mandiner.hu --output $OUT \
            --limit "$PER_WORKER" --batch 8 --once --stages "$stages" \
            --log-level WARNING > "$STAGE/ab-$name-$w.log" 2>&1 &
    done
    wait
    elapsed=$(echo "$(date +%s.%N) - $started" | bc)
    after_bytes=$(du -sb $OUT | cut -f1)
    local n=$((WORKERS * PER_WORKER))
    echo "   archives      $n"
    echo "   wall          ${elapsed}s"
    echo "   archives/s    $(echo "scale=3; $n / $elapsed" | bc)"
    echo "   bytes written $(echo "scale=2; ($after_bytes - $before_bytes) / 1048576" | bc) MB"
    echo "   MB/article    $(echo "scale=3; ($after_bytes - $before_bytes) / 1048576 / $n" | bc)"
}

condition with-screenshot "content,screenshot"
condition content-only "content"
echo "== ledger"
docker exec cx-pg-stage psql -U causalia -d causalia_stage -tAc \
  "SELECT screenshot_state, count(*) FROM corpus.extraction_task GROUP BY 1 ORDER BY 1"
