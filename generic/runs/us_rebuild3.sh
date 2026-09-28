#!/bin/bash
# Rebuild the UltraScale (xcku025) and UltraScale+ small-die DBs in parallel,
# each in its own capped scope, after backing up the current DB files.
set -u
cd /home/tim/github/f4pga/prjuray
log=build/logs/us_rebuild3.log
ts=$(date +%Y%m%d%H%M)
for a in UltraScale UltraScalePlus; do
    mkdir build/db/$a.dbbak-$ts
    find build/db/$a -maxdepth 1 -type f -exec cp -p {} build/db/$a.dbbak-$ts/ \;
done
echo "$(date +%T) start (backups *.dbbak-$ts)" >> $log
generic/vrun.sh uspdb-3 50G python3 -u generic/python/pipeline.py \
    --dies xaau7p,xazu1eg,xaau10p,xazu2eg --tags r1,r3,r4,r8,r9,r11 \
    --check-tags r11 --skip-tilegrid --jobs 20 --sample-jobs 12 \
    > build/logs/pipeline_usp_5.log 2>&1 &
p1=$!
generic/vrun.sh usdb-3 30G python3 -u generic/python/pipeline.py \
    --dies xcku025 --tags r3,r4,r8,r9,r11 --check-tags r11 --skip-tilegrid \
    --jobs 12 --sample-jobs 6 > build/logs/pipeline_us_3.log 2>&1 &
p2=$!
wait $p1; rc1=$?
wait $p2; rc2=$?
echo "$(date +%T) done usp rc=$rc1 us rc=$rc2" >> $log
