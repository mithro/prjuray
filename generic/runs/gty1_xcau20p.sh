#!/bin/bash
# GTY-focused volume round on xcau20p (smallest US+ die with GTYE4). The US+
# GTY_R DB was degenerate (33k GTYE4 features all claiming the same ~3300 bits)
# for lack of GTY samples; like the Series7 gtx3 round, low-density designs
# with GT channels/commons give many GT samples per design.
set -u
cd /home/tim/github/f4pga/prjuray
generic/vrun.sh gty1-au20p 40G python3 -u generic/python/run_designs.py \
    --die xcau20p --tag gty1 --seeds 1:60 --jobs 6 --reuse 1 \
    -- --focus '^(GTYE4_|IBUFDS_GTE4)' --density 0.02 \
    > build/logs/gty1_xcau20p.log 2>&1
rc=$?
echo "$(date +%T) gty1 rc=$rc" >> build/logs/gty1_xcau20p.log
