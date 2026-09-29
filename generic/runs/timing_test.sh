#!/bin/bash
# Timing test data (not a full run): tcl/dump_timing.tcl + python/timing.py
# on the three CI dies (one per architecture), every speed grade, with the
# Vivado given by URAY_VIVADO_SETTINGS (default 2025.2).
#   generic/runs/timing_test.sh [<vivado version>]
set -u
cd /home/tim/github/f4pga/prjuray
ver=${1:-2025.2}
# GRADES=fuzz redumps only the fuzz part (tile/site timing); the other
# speed grades' model tables are kept from an earlier run.
grades=${GRADES:-all}
# DIES limits the dies (default: the three CI dies).
dies=${DIES:-xa7s15 xazu1eg xcku025}
export URAY_VIVADO_SETTINGS=/opt/xilinx/Vivado/$ver/settings64.sh
log=build/logs/timing_test_$ver.log
echo "$(date +%T) start $ver" >> $log
generic/vrun.sh timing-$ver 24G env grades=$grades dies="$dies" bash -c '
    for die in $dies; do
        python3 -u generic/python/timing.py dump $die --grades $grades --jobs 4 &&
        python3 -u generic/python/timing.py json $die || echo "$die FAILED"
    done' >> $log 2>&1
echo "$(date +%T) done $ver rc=$?" >> $log
