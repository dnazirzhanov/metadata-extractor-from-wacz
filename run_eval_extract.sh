#!/usr/bin/env bash
# Pilot extraction for the large-corpus evaluation. Read-only over the archive:
# writes only into $EVAL/out, which is outside PAGES_ROOT.
set -uo pipefail
cd ~/causalia-article-extractor
EVAL=/mnt/hdd/c0cshf/causalia-eval-20260902
PAGES=/mnt/hdd/c0cshf/causalia/pages
LIMIT=56
echo "started $(date -Is)  extractor $(.venv/bin/causalia-extractor --version)"
rc_worst=0
for o in $(ls "$PAGES" | grep '\.' | sort); do
    echo "=== $o"
    nice -n 19 ionice -c3 .venv/bin/causalia-extractor extract \
        --input "$PAGES" --outlet "$o" --limit "$LIMIT" \
        --output "$EVAL/out" --log-level WARNING
    rc=$?
    echo "--- $o exit=$rc"
    [ $rc -gt $rc_worst ] && rc_worst=$rc
done
echo "finished $(date -Is)  worst exit=$rc_worst"
