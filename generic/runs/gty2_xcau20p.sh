#!/bin/bash
# GTY-focused volume round (gty2: with 9e8aa53, GT outputs never drive GT inputs,
# so every GT cell survives to the bitstream; gty1 was stopped at 15/60) on xcau20p (smallest US+ die with GTYE4). The US+
# GTY_R DB was degenerate (33k GTYE4 features all claiming the same ~3300 bits)
# for lack of GTY samples; like the Series7 gtx3 round, low-density designs
# with GT channels/commons give many GT samples per design.
set -u
cd /home/tim/github/f4pga/prjuray
generic/vrun.sh gty2-au20p 40G python3 -u generic/python/run_designs.py \
    --die xcau20p --tag gty2 --seeds 1:60 --jobs 6 --reuse 1 \
    -- --focus '^(GTYE4_|IBUFDS_GTE4)' --density 0.02 \
    > build/logs/gty2_xcau20p.log 2>&1
rc=$?
echo "$(date +%T) gty2 rc=$rc" >> build/logs/gty2_xcau20p.log
