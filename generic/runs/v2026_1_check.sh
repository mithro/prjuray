#!/bin/bash
# Vivado 2026.1 compatibility check of the generic flow.  The same small
# design round (same seeds and generator settings) is run with Vivado 2025.2
# (tag v252a) and 2026.1 (tag v261a), and both are checked with check.py
# against the production databases, which were built from 2025.2 designs.
# Equal undocumented / unowned numbers per seed mean that 2026.1 encodes the
# same bits for the same features; compare with
#   paste <(grep -o 'unowned.*' build/logs/check_v252a_<die>.log) \
#         <(grep -o 'unowned.*' build/logs/check_v261a_<die>.log)
#   generic/runs/v2026_1_check.sh [dies...]      (VERSIONS="2025.2 2026.1")
set -u
cd /home/tim/github/f4pga/prjuray
dies=${*:-xa7s15}
log=build/logs/v2026_1_check.log
for die in $dies; do
    for ver in ${VERSIONS:-2025.2 2026.1}; do
        tag=v${ver//./}a
        tag=${tag/v20/v}          # 2025.2 -> v252a, 2026.1 -> v261a
        export URAY_VIVADO_SETTINGS=/opt/xilinx/Vivado/$ver/settings64.sh
        # A round of this tag and die still running (started elsewhere):
        # wait for it instead of starting a second one.
        while systemctl --user list-units --plain --no-legend "prjuray-${tag}-$die-*" | grep -q .; do
            sleep 60
        done
        if ! grep -q "round $tag done" build/logs/${tag}_$die.log 2>&1; then
            echo "$(date +%T) $die $ver designs" >> $log
            generic/vrun.sh ${tag}-$die 24G python3 -u generic/python/run_designs.py \
                --die $die --tag $tag --seeds 1:8 --jobs 4 --reuse 4 \
                > build/logs/${tag}_$die.log 2>&1
            echo "$(date +%T) $die $ver designs rc=$?" >> $log
        fi
        generic/vrun.sh ${tag}chk-$die 16G python3 -u generic/python/check.py \
            --die $die --designs build/designs/$die/$tag --max 20 --jobs 4 \
            > build/logs/check_${tag}_$die.log 2>&1
        echo "$(date +%T) $die $ver check rc=$?" >> $log
    done
done
