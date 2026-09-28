#!/bin/bash
# rebuild_all6: as rebuild_all5 with the e0a64d6 merges (PARK default on,
# MKDB_TWIN_MAX ambiguity guard, stable sample subset), the xcku025/xcku035
# HPIO_L/XIPHY_L tile grid fix, US+ gty1 designs, and --sample-jobs 40 (the
# --mem-budget still bounds concurrency).  Previously: rebuild all three DBs in
# parallel with the current generic-db: Series7 over
# all 14 dies (now incl. xc7a200t from its r12reg region designs), UltraScale+
# over all 13 dies, UltraScale over xcku025 + xcku035. Tile grids are the
# installed ones (--skip-tilegrid); pipeline --mem-budget bounds the sample
# phase and the check stage.
set -u
cd /home/tim/github/f4pga/prjuray
log=build/logs/rebuild_all6.log
if systemctl --user list-units --plain --no-legend 'prjuray-*db-6*' | grep -q .; then
    echo "$(date +%T) db-6 scopes already exist, not launching" >> $log
    exit 1
fi
ts=$(date +%Y%m%d%H%M)
for a in Series7 UltraScale UltraScalePlus; do
    mkdir build/db/$a.dbbak-$ts
    find build/db/$a -maxdepth 1 -type f -exec cp -p {} build/db/$a.dbbak-$ts/ \;
done
echo "$(date +%T) start (backups *.dbbak-$ts)" >> $log
generic/vrun.sh s7db-6 60G python3 -u generic/python/pipeline.py \
    --dies xa7s15,xa7a12t,xa7s25,xa7a15t,xa7z010,xc7z012s,xa7s50,xa7z020,xa7a100t,xa7s100,xa7z030,xc7k70t,xc7k160t,xc7a200t \
    --tags r3,r4,r5,r7,r8,r9,r10,r11,r12,mmcm1,hb_gtx2,gtx3,r12reg --check-tags r12,r12reg \
    --skip-tilegrid --jobs 24 --sample-jobs 40 --mem-budget 50 > build/logs/pipeline_s7_20.log 2>&1 &
p1=$!
generic/vrun.sh uspdb-6 50G python3 -u generic/python/pipeline.py \
    --dies xaau7p,xazu1eg,xaau10p,xazu2eg,xazu3teg,xcku3p,xczu4cg,xazu4ev,xcau20p,xck26,xazu7ev,xczu7cg,xcu25 \
    --tags r1,r3,r4,r7,r8,r9,r11,r12reg,r13d,gty1 --check-tags r13d,r12reg \
    --skip-tilegrid --jobs 20 --sample-jobs 40 --mem-budget 40 > build/logs/pipeline_usp_8.log 2>&1 &
p2=$!
generic/vrun.sh usdb-6 30G python3 -u generic/python/pipeline.py \
    --dies xcku025,xcku035 --tags r3,r4,r8,r9,r11,r12reg,r13d \
    --check-tags r13d,r12reg --skip-tilegrid --jobs 12 --sample-jobs 40 --mem-budget 22 \
    > build/logs/pipeline_us_6.log 2>&1 &
p3=$!
wait $p1; rc1=$?
wait $p2; rc2=$?
wait $p3; rc3=$?
echo "$(date +%T) done s7 rc=$rc1 usp rc=$rc2 us rc=$rc3" >> $log
