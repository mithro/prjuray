#!/bin/bash
# UltraScale part of rebuild_all6 again: usdb-6 was OOM-killed at its 30G cap
# 3 min into the sample phase (8 workers from a stale 2.03 GiB known peak;
# worker memory grew with PARK features).  Retry with 4 sample workers.
set -u
cd /home/tim/github/f4pga/prjuray
log=build/logs/rebuild_us7.log
if systemctl --user list-units --plain --no-legend 'prjuray-usdb-7*' | grep -q .; then
    echo "$(date +%T) usdb-7 scope already exists, not launching" >> $log
    exit 1
fi
echo "$(date +%T) start" >> $log
generic/vrun.sh usdb-7 30G python3 -u generic/python/pipeline.py \
    --dies xcku025,xcku035 --tags r3,r4,r8,r9,r11,r12reg,r13d \
    --check-tags r13d,r12reg --skip-tilegrid --jobs 12 --sample-jobs 4 --mem-budget 22 \
    > build/logs/pipeline_us_7.log 2>&1
rc=$?
echo "$(date +%T) done us rc=$rc" >> $log
