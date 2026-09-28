#!/bin/bash
# After merging us-residue a7381f7 (INT global node muxes parked, URAY_GPARK)
# on top of c68193c (PARK: constant-net wires are not live); chain_c68193c
# was stopped 5 min into its rebuild for this:
# CI compare (review the log), golden update, then rebuild the US DB (4 sample
# workers; usdb-6 OOMed at 8) and the US+ DB with the gty1 + gty2 GT rounds.
# US+ at 20 sample workers: at 40 its scope peak reached the 50G cap.
# Series7 is unaffected (xa7s15 CI must be identical; checked below).
set -u
cd /home/tim/github/f4pga/prjuray
log=build/logs/chain_a7381f7.log
if systemctl --user list-units --plain --no-legend 'prjuray-usdb-9*' 'prjuray-uspdb-8*' | grep -q .; then
    echo "$(date +%T) db scopes already exist, not launching" >> $log
    exit 1
fi
generic/ci.sh xa7s15 xazu1eg xcku025 > build/logs/ci_a7381f7.log 2>&1
echo "$(date +%T) ci compare rc=$?" >> $log
if ! grep -A20 "^== ci xa7s15" build/logs/ci_a7381f7.log | grep -q "CI: identical to golden"; then
    echo "$(date +%T) xa7s15 not identical, stopping before golden update" >> $log
    exit 1
fi
generic/ci.sh -u xa7s15 xazu1eg xcku025 >> build/logs/ci_a7381f7.log 2>&1
echo "$(date +%T) ci update rc=$?" >> $log
ts=$(date +%Y%m%d%H%M)
for a in UltraScale UltraScalePlus; do
    mkdir build/db/$a.dbbak-$ts
    find build/db/$a -maxdepth 1 -type f -exec cp -p {} build/db/$a.dbbak-$ts/ \;
done
echo "$(date +%T) rebuild start (backups *.dbbak-$ts)" >> $log
generic/vrun.sh usdb-9 30G python3 -u generic/python/pipeline.py \
    --dies xcku025,xcku035 --tags r3,r4,r8,r9,r11,r12reg,r13d \
    --check-tags r13d,r12reg --skip-tilegrid --jobs 12 --sample-jobs 4 --mem-budget 22 \
    > build/logs/pipeline_us_9.log 2>&1 &
p1=$!
generic/vrun.sh uspdb-8 50G python3 -u generic/python/pipeline.py \
    --dies xaau7p,xazu1eg,xaau10p,xazu2eg,xazu3teg,xcku3p,xczu4cg,xazu4ev,xcau20p,xck26,xazu7ev,xczu7cg,xcu25 \
    --tags r1,r3,r4,r7,r8,r9,r11,r12reg,r13d,gty1,gty2 --check-tags r13d,r12reg \
    --skip-tilegrid --jobs 20 --sample-jobs 20 --mem-budget 40 > build/logs/pipeline_usp_10.log 2>&1 &
p2=$!
wait $p1; rc1=$?
wait $p2; rc2=$?
echo "$(date +%T) done us rc=$rc1 usp rc=$rc2" >> $log
