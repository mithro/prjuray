#!/bin/bash
# viv.sh <dir> <log> <vivado args...>   (memory capped via generic/vrun.sh)
cd "$1" || exit 1
log=$2
shift 2
source /opt/xilinx/Vivado/2025.2/settings64.sh
exec $(dirname $(readlink -f $0))/../vrun.sh sp-viv ${VMEM:-6G} /usr/bin/time -v -o "$log.time" vivado -mode batch -nojournal -nolog "$@" > "$log" 2>&1
