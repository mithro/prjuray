#!/bin/bash
# Rebuild all three DBs in parallel with the current generic-db: Series7 over
# all 14 dies (now incl. xc7a200t from its r12reg region designs), UltraScale+
# over all 13 dies, UltraScale over xcku025 + xcku035. Tile grids are the
# installed ones (--skip-tilegrid); pipeline --mem-budget bounds the sample
# phase and the check stage.
set -u
cd /home/tim/github/f4pga/prjuray
log=build/logs/rebuild_all5.log
if systemctl --user list-units --plain --no-legend 'prjuray-*db-5*' | grep -q .; then
    echo "$(date +%T) db-5 scopes already exist, not launching" >> $log
    exit 1
fi
ts=$(date +%Y%m%d%H%M)
for a in Series7 UltraScale UltraScalePlus; do
    mkdir build/db/$a.dbbak-$ts
    find build/db/$a -maxdepth 1 -type f -exec cp -p {} build/db/$a.dbbak-$ts/ \;
done
echo "$(date +%T) start (backups *.dbbak-$ts)" >> $log
generic/vrun.sh s7db-5 60G python3 -u generic/python/pipeline.py \
    --dies xa7s15,xa7a12t,xa7s25,xa7a15t,xa7z010,xc7z012s,xa7s50,xa7z020,xa7a100t,xa7s100,xa7z030,xc7k70t,xc7k160t,xc7a200t \
    --tags r3,r4,r5,r7,r8,r9,r10,r11,r12,mmcm1,hb_gtx2,gtx3,r12reg --check-tags r12,r12reg \
    --skip-tilegrid --jobs 24 --mem-budget 50 > build/logs/pipeline_s7_19.log 2>&1 &
p1=$!
generic/vrun.sh uspdb-5 50G python3 -u generic/python/pipeline.py \
    --dies xaau7p,xazu1eg,xaau10p,xazu2eg,xazu3teg,xcku3p,xczu4cg,xazu4ev,xcau20p,xck26,xazu7ev,xczu7cg,xcu25 \
    --tags r1,r3,r4,r7,r8,r9,r11,r12reg,r13d --check-tags r13d,r12reg \
    --skip-tilegrid --jobs 20 --mem-budget 40 > build/logs/pipeline_usp_7.log 2>&1 &
p2=$!
generic/vrun.sh usdb-5 30G python3 -u generic/python/pipeline.py \
    --dies xcku025,xcku035 --tags r3,r4,r8,r9,r11,r12reg,r13d \
    --check-tags r13d,r12reg --skip-tilegrid --jobs 12 --mem-budget 22 \
    > build/logs/pipeline_us_5.log 2>&1 &
p3=$!
wait $p1; rc1=$?
wait $p2; rc2=$?
wait $p3; rc3=$?
echo "$(date +%T) done s7 rc=$rc1 usp rc=$rc2 us rc=$rc3" >> $log
