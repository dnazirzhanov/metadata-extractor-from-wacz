#!/usr/bin/env bash
# Sample the box and the database while a rung runs. One JSON object per line.
#
# THREE MEASUREMENT RULES, each learned by being wrong on this hardware:
#   * DISCARD THE FIRST SAMPLE of iostat and of the CPU counter. On a host with
#     31 days of uptime that sample is a month-long average - it has read 34%
#     idle on a box whose true instantaneous idle was 9.4%. Both loops below
#     start at interval 2 and drop the first report.
#   * %util IS NOT SATURATION on a queued rotational device. aqu-sz and r_await
#     are the honest ones, so they are what is recorded.
#   * CPU is %user + %nice. The archiver's workers run under nice, and reading
#     %user alone shows a saturated box as 0.1% busy.
set -uo pipefail
INTERVAL=${INTERVAL:-10}
DEV=${DEV:-sdb}
OUT_DIR=${OUT_DIR:-/mnt/hdd/c0cshf/p0-stage-20260911/out}
DSN_CONTAINER=${DSN_CONTAINER:-cx-pg-stage}
DB=${DB:-causalia_stage}

psqlq() { docker exec "$DSN_CONTAINER" psql -U causalia -d "$DB" -tAc "$1" 2>/dev/null; }

while true; do
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    # iostat: two reports, keep the second - the first is since boot.
    #
    # Columns are located BY HEADER NAME, not by position. sysstat has moved
    # them between versions (aqu-sz used to be avgqu-sz and sat elsewhere), and
    # a hardcoded index silently reports the wrong metric rather than failing -
    # which is how a first attempt at this recorded aqu-sz as 0.00 on a disk
    # that was clearly queueing.
    read -r rkbs wkbs aqu rawait wawait util <<<"$(
        iostat -dx "$DEV" 1 2 | awk -v d="$DEV" '
            /^Device/ { for (i = 1; i <= NF; i++) col[$i] = i; next }
            $1 == d {
                r = $(col["rkB/s"]); w = $(col["wkB/s"]);
                a = (("aqu-sz" in col) ? $(col["aqu-sz"]) : $(col["avgqu-sz"]));
                ra = $(col["r_await"]); wa = $(col["w_await"]); u = $(col["%util"]);
            }
            END { print r+0, w+0, a+0, ra+0, wa+0, u+0 }')"

    # vmstat: same trick, second sample is the live one.
    read -r us sy id wa <<<"$(vmstat 1 2 | tail -1 | awk '{print $13, $14, $15, $16}')"

    dirty=$(awk '/^Dirty:/{print $2}' /proc/meminfo)
    writeback=$(awk '/^Writeback:/{print $2}' /proc/meminfo)

    pg_cpu=$(docker stats --no-stream --format '{{.CPUPerc}}' "$DSN_CONTAINER" 2>/dev/null | tr -d '%')
    pg_mem=$(docker stats --no-stream --format '{{.MemUsage}}' "$DSN_CONTAINER" 2>/dev/null | awk '{print $1}')

    wal=$(psqlq "SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint")
    dbsize=$(psqlq "SELECT pg_database_size(current_database())")
    conns=$(psqlq "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()")
    read -r extracted ingested failed quarantined retried <<<"$(psqlq "
        SELECT count(*) FILTER (WHERE extract_state='extracted')
            || ' ' || count(*) FILTER (WHERE ingest_state='ingested')
            || ' ' || count(*) FILTER (WHERE extract_state='failed' OR ingest_state='failed')
            || ' ' || count(*) FILTER (WHERE extract_state='quarantined')
            || ' ' || count(*) FILTER (WHERE attempts > 1)
          FROM corpus.extraction_task")"
    # du -sb walks every article directory. At 25,000 of them on a rotational
    # disk that is real I/O, and during an ingest measurement the sampler then
    # competes with the thing it is measuring. SKIP_DU=1 for any run where the
    # number being measured is the database's.
    if [ "${SKIP_DU:-0}" = "1" ]; then out_bytes=0; else
        out_bytes=$(du -sb "$OUT_DIR" 2>/dev/null | cut -f1); fi

    printf '{"ts":"%s","r_kb_s":%s,"w_kb_s":%s,"aqu_sz":%s,"r_await":%s,"w_await":%s,"util":%s,' \
        "$ts" "${rkbs:-0}" "${wkbs:-0}" "${aqu:-0}" "${rawait:-0}" "${wawait:-0}" "${util:-0}"
    printf '"cpu_busy":%s,"cpu_iowait":%s,"cpu_idle":%s,"dirty_kb":%s,"writeback_kb":%s,' \
        "$(( ${us:-0} + ${sy:-0} ))" "${wa:-0}" "${id:-0}" "${dirty:-0}" "${writeback:-0}"
    printf '"pg_cpu":%s,"pg_mem":"%s","wal_bytes":%s,"db_bytes":%s,"pg_conns":%s,' \
        "${pg_cpu:-0}" "${pg_mem:-0}" "${wal:-0}" "${dbsize:-0}" "${conns:-0}"
    printf '"extracted":%s,"ingested":%s,"failed":%s,"quarantined":%s,"retried":%s,"out_bytes":%s}\n' \
        "${extracted:-0}" "${ingested:-0}" "${failed:-0}" "${quarantined:-0}" "${retried:-0}" "${out_bytes:-0}"

    sleep "$INTERVAL"
done
