#!/bin/bash
# park/ci.sh <name> [VAR=val...]: ci.sh xcku025 from the speed worktree (current branch)
W=/home/tim/github/f4pga/prjuray/.claude/worktrees/agent-ae3b9b22dac6da105
n=$1
shift
for kv in "$@"; do export "$kv"; done
export URAY_BUILD=/home/tim/github/f4pga/prjuray/build
export CI_MEM=${CI_MEM:-11G}
cd $W
bash generic/ci.sh -j 8 xcku025 > /tmp/claude-1000/sp_speed/speed/park/$n.log 2>&1
echo "EXIT $?" >> /tmp/claude-1000/sp_speed/speed/park/$n.log
