#!/bin/bash
# Vivado 2026.1 compatibility check of the generic flow: a small design
# round on the smallest die of each architecture with 2026.1 (tag v261a),
# then check.py of those bitstreams against the production databases, which
# were built from Vivado 2025.2 designs.  Similar undocumented / unowned
# numbers to the 2025.2 hold-out designs mean that 2026.1 encodes the same
# bits for the same features.
#   generic/runs/v2026_1_check.sh [dies...]
set -u
cd /home/tim/github/f4pga/prjuray
dies=${*:-xa7s15}
export URAY_VIVADO_SETTINGS=/opt/xilinx/Vivado/2026.1/settings64.sh
log=build/logs/v2026_1_check.log
for die in $dies; do
    echo "$(date +%T) $die designs" >> $log
    generic/vrun.sh v261-$die 24G python3 -u generic/python/run_designs.py \
        --die $die --tag v261a --seeds 1:8 --jobs 4 --reuse 4 \
        > build/logs/v261a_$die.log 2>&1
    echo "$(date +%T) $die designs rc=$?" >> $log
    generic/vrun.sh v261chk-$die 16G python3 -u generic/python/check.py \
        --die $die --designs build/designs/$die/v261a --max 20 --jobs 4 \
        > build/logs/check_v261a_$die.log 2>&1
    echo "$(date +%T) $die check rc=$?" >> $log
done
