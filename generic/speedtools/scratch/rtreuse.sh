#!/bin/bash
# rtreuse.sh <mode close|keep> [a dir] [b dir] [name] [VAR=val...]: two designs in one Vivado
S=/tmp/claude-1000/sp_speed/speed
B=/home/tim/github/f4pga/prjuray/build/designs/xcu25/r12reg
export RTR_W=/home/tim/github/f4pga/prjuray/.claude/worktrees/agent-ae3b9b22dac6da105
export RTR_MODE=$1
export RTR_A=${2:-$B/s3} RTR_B=${3:-$B/s4}
export RTR_OUT=$S/rtreuse/${4:-$1}
shift 4
for kv in "$@"; do export "$kv"; done
rm -rf $RTR_OUT
mkdir -p $RTR_OUT
export NL_VIVADO_LOG=$RTR_OUT/vivado.log
cd $RTR_OUT
source /opt/xilinx/Vivado/2025.2/settings64.sh
$RTR_W/generic/vrun.sh sp-rtr ${VMEM:-6G} /usr/bin/time -v -o time.log \
    vivado -mode batch -nojournal -log vivado.log -source $S/rtreuse.tcl > run.log 2>&1
echo "rc $?" > done
