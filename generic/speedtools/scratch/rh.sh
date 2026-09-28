#!/bin/bash
# rh.sh <die> <tag>... : rarehits for rounds of a die
W=/home/tim/github/f4pga/prjuray/.claude/worktrees/agent-ae3b9b22dac6da105
B=/home/tim/github/f4pga/prjuray/build/designs
die=$1
shift
args=()
for t in "$@"; do
  if [ -d "$t" ]; then args+=("$t"); else args+=("$B/$die/$t"); fi
done
python3 $W/generic/speedtools/rarehits.py ${DB:-/home/tim/github/f4pga/prjuray/build/db/Series7} "${args[@]}"
