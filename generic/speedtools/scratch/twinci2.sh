#!/bin/bash
# twinci2.sh: ci with the twin guard at its default (1000) vs off, 3 dies
S=/tmp/claude-1000/sp_speed/speed/park
C=/home/tim/github/f4pga/prjuray/.claude/worktrees/agent-ae3b9b22dac6da105/generic/python/ci_summary.py
for die in xa7s15 xazu1eg xcku025; do
  bash $S/cid.sh $die tw2_${die}_on
  echo "=== $die off -> on(1000)"
  python3 $C compare $S/tw_${die}_off.summary.txt $S/tw2_${die}_on.summary.txt
done > $S/tw2_cmp.txt 2>&1
echo DONE >> $S/tw2_cmp.txt
