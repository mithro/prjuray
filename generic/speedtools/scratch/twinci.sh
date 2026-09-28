#!/bin/bash
# twinci.sh: ci xa7s15 / xazu1eg / xcku025 with the twin guard off (huge max) and on
S=/tmp/claude-1000/sp_speed/speed/park
C=/home/tim/github/f4pga/prjuray/.claude/worktrees/agent-ae3b9b22dac6da105/generic/python/ci_summary.py
for die in xa7s15 xazu1eg xcku025; do
  bash $S/cid.sh $die tw_${die}_off MKDB_TWIN_MAX=1000000000
  bash $S/cid.sh $die tw_${die}_on
  echo "=== $die off -> on"
  python3 $C compare $S/tw_${die}_off.summary.txt $S/tw_${die}_on.summary.txt
done > $S/tw_cmp.txt 2>&1
echo DONE >> $S/tw_cmp.txt
