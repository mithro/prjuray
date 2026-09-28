#!/bin/bash
# rerun.sh <design dir> <out dir> [VAR=value ...]
# Re-implement an existing design (its design.tcl) with this checkout's
# netlist.tcl in <out dir>, memory capped via vrun.sh (VMEM, default 6G) and
# killed after TMO seconds (default 2700).  Extra VAR=value arguments go to
# Vivado's environment (e.g. NL_ROUTE_DIRECTIVE=Quick).  Writes <out>/done
# with the exit status; wall/rss are in <out>/vivado.log.time.
set -u
src=$(realpath "$1")
out=$2
shift 2
W=$(cd "$(dirname "$0")/../.." && pwd)
mkdir -p "$out"
out=$(realpath "$out")
rm -f "$out/done"
sed "1s#^source .*netlist.tcl#source $W/generic/tcl/netlist.tcl#" "$src/design.tcl" > "$out/design.tcl"
cd "$out" || exit 1
source /opt/xilinx/Vivado/2025.2/settings64.sh
env "$@" timeout "${TMO:-2700}" "$W/generic/vrun.sh" sp-rerun "${VMEM:-6G}" \
    /usr/bin/time -v -o vivado.log.time \
    vivado -mode batch -nojournal -nolog -source design.tcl > vivado.log 2>&1
echo "rc $?" > done
