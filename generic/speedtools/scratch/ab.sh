#!/bin/bash
# ab.sh <tag>: ci.sh xcku025 with URAY_PARK=0 then 1 (current worktree code), summaries saved
S=/tmp/claude-1000/sp_speed/speed/park
X=/home/tim/github/f4pga/prjuray/build/ci/work/agent-ae3b9b22dac6da105/xcku025
for p in 0 1; do
  CI_MEM=16G bash $S/ci.sh $1_p$p URAY_PARK=$p
  cp $X/summary.txt $S/$1_p$p.summary.txt
  cp $X/check.log $S/$1_p$p.check.log
  rm -rf $S/db_$1_p$p
  cp -a $X/build/db/UltraScale $S/db_$1_p$p
done
python3 /home/tim/github/f4pga/prjuray/.claude/worktrees/agent-ae3b9b22dac6da105/generic/python/ci_summary.py compare $S/$1_p0.summary.txt $S/$1_p1.summary.txt > $S/$1_cmp.txt 2>&1
echo "DONE" >> $S/$1_cmp.txt
