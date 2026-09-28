#!/bin/bash
# cid.sh <die> <name> [VAR=val...]: ci.sh <die> from the speed worktree; summary/check saved as park/<name>.*
W=/home/tim/github/f4pga/prjuray/.claude/worktrees/agent-ae3b9b22dac6da105
S=/tmp/claude-1000/sp_speed/speed/park
die=$1
n=$2
shift 2
for kv in "$@"; do export "$kv"; done
export URAY_BUILD=/home/tim/github/f4pga/prjuray/build
export CI_MEM=${CI_MEM:-12G}
X=$URAY_BUILD/ci/work/agent-ae3b9b22dac6da105/$die
cd $W
bash generic/ci.sh -j 8 $die > $S/$n.log 2>&1
echo "EXIT $?" >> $S/$n.log
cp $X/summary.txt $S/$n.summary.txt
cp $X/check.log $S/$n.check.log
cp $X/mkdb.log $S/$n.mkdb.log
