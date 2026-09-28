#!/bin/bash
# dbg.sh <name> [VAR=val...]: rerun speed_pips1/s1 with pip debug + extra env
S=/tmp/claude-1000/sp_speed/speed
n=$1
shift
cd $S
VMEM=3G TMO=900 bash /home/tim/github/f4pga/prjuray/.claude/worktrees/agent-ae3b9b22dac6da105/generic/speedtools/rerun.sh /home/tim/github/f4pga/prjuray/build/designs/xa7s15/speed_pips1/${SEED:-s1} cov/$n NL_PIPS_DEBUG=1 NL_VIVADO_LOG=vivado.log "$@" > cov/$n.out 2>&1
echo "$n $(cat cov/$n/done) forcing $(grep -c forcing cov/$n/nl.log)"
grep -v "^err \|^properr\|^disconn\|^tying\|^forcing" cov/$n/nl.log | tail -6 | cut -c1-200
