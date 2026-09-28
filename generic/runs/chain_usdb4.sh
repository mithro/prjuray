#!/bin/bash
# After r13's --density 0.05 arm: first US+ DB over all 13 US+ dies (4 small +
# 9 large, region designs r12reg) and US DB over xcku025 + xcku035. The large
# dies' tile grids were built by the tile-grid agent with --max-matches /
# --learn-exclude, so --skip-tilegrid is required.
set -u
cd /home/tim/github/f4pga/prjuray
log=build/logs/chain_usdb4.log
echo "$(date +%T) waiting for r13 d05 arm" >> $log
until grep -qE "vrun:|Terminated" build/logs/r13_us_d05.log; do sleep 30; done
if systemctl --user list-units --plain --no-legend 'prjuray-uspdb-4*' 'prjuray-usdb-4*' | grep -q .; then
    echo "$(date +%T) db-4 scopes already exist, not launching" >> $log
    exit 1
fi
ts=$(date +%Y%m%d%H%M)
for a in UltraScale UltraScalePlus; do
    mkdir build/db/$a.dbbak-$ts
    find build/db/$a -maxdepth 1 -type f -exec cp -p {} build/db/$a.dbbak-$ts/ \;
done
echo "$(date +%T) start (backups *.dbbak-$ts)" >> $log
generic/vrun.sh uspdb-4 50G python3 -u generic/python/pipeline.py \
    --dies xaau7p,xazu1eg,xaau10p,xazu2eg,xazu3teg,xcku3p,xczu4cg,xazu4ev,xcau20p,xck26,xazu7ev,xczu7cg,xcu25 \
    --tags r1,r3,r4,r7,r8,r9,r11,r12reg,r13d --check-tags r13d,r12reg \
    --skip-tilegrid --jobs 20 --mem-budget 40 > build/logs/pipeline_usp_6.log 2>&1 &
p1=$!
generic/vrun.sh usdb-4 30G python3 -u generic/python/pipeline.py \
    --dies xcku025,xcku035 --tags r3,r4,r8,r9,r11,r12reg,r13d \
    --check-tags r13d,r12reg --skip-tilegrid --jobs 12 --mem-budget 22 \
    > build/logs/pipeline_us_4.log 2>&1 &
p2=$!
wait $p1; rc1=$?
wait $p2; rc2=$?
echo "$(date +%T) done usp rc=$rc1 us rc=$rc2" >> $log
