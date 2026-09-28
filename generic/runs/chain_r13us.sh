#!/bin/bash
# After the large US+ region batch: r13 on the small US/US+ dies, split in a
# default-density arm (tag r13) and a --density 0.05 arm (tag r13d).
set -u
cd /home/tim/github/f4pga/prjuray
log=build/logs/chain_r13us.log
dies=xaau7p,xazu1eg,xcku025,xaau10p,xazu2eg
echo "$(date +%T) waiting for r12_uspl" >> $log
until grep -qE "vrun:|Terminated" build/logs/r12_uspl.log; do sleep 30; done
if systemctl --user list-units --plain --no-legend 'prjuray-r13us*' | grep -q .; then
    echo "$(date +%T) r13us scopes already exist, not launching" >> $log
    exit 1
fi
echo "$(date +%T) start" >> $log
generic/vrun.sh r13us-def 30G python3 -u generic/python/run_designs.py \
    --die $dies --tag r13 --seeds 1:10 --jobs 6 --reuse 1 \
    > build/logs/r13_us_def.log 2>&1 &
p1=$!
generic/vrun.sh r13us-d05 30G python3 -u generic/python/run_designs.py \
    --die $dies --tag r13d --seeds 11:20 --jobs 6 --reuse 1 -- --density 0.05 \
    > build/logs/r13_us_d05.log 2>&1 &
p2=$!
wait $p1; rc1=$?
wait $p2; rc2=$?
echo "$(date +%T) done def rc=$rc1 d05 rc=$rc2" >> $log
