#!/bin/bash
# After r12 Series7 medium: GTX volume round on xc7k70t, then the Series7 DB
# rebuild (r3-r12 + mmcm1 + GTX rounds), each in its own capped scope.
set -u
cd /home/tim/github/f4pga/prjuray
log=build/logs/chain_s7_17.log
echo "$(date +%T) waiting for r12_s7med" >> $log
until grep -qE "vrun:|Terminated" build/logs/r12_s7med.log; do sleep 30; done
echo "$(date +%T) gtx3 start" >> $log
generic/vrun.sh gtx3-k70t 30G python3 -u generic/python/run_designs.py \
    --die xc7k70t --tag gtx3 --seeds 1:60 --jobs 6 --reuse 2 --directive Quick \
    -- --focus '^(GTXE2_|IBUFDS_GTE2)' --density 0.02 > build/logs/gtx3_k70t.log 2>&1; rc=$?
echo "$(date +%T) gtx3 rc=$rc" >> $log
ts=$(date +%Y%m%d%H%M)
mkdir build/db/Series7.dbbak-$ts
find build/db/Series7 -maxdepth 1 -type f -exec cp -p {} build/db/Series7.dbbak-$ts/ \;
echo "$(date +%T) s7db-17 start (backup Series7.dbbak-$ts)" >> $log
generic/vrun.sh s7db-17 60G python3 -u generic/python/pipeline.py \
    --dies xa7s15,xa7a12t,xa7s25,xa7a15t,xa7z010,xc7z012s,xa7s50,xa7z020,xa7a100t,xa7s100,xa7z030,xc7k70t,xc7k160t \
    --tags r3,r4,r5,r7,r8,r9,r10,r11,r12,mmcm1,hb_gtx2,gtx3 --check-tags r12 \
    --skip-tilegrid --jobs 24 --sample-jobs 48 > build/logs/pipeline_s7_17.log 2>&1; rc=$?
echo "$(date +%T) s7db-17 rc=$rc" >> $log
