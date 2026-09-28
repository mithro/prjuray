#!/bin/bash
# abc.sh: speed-sched (stable subset) ci xcku025: base, LEAFPAIR, PARK, both; compared with base
S=/tmp/claude-1000/sp_speed/speed/park
C=/home/tim/github/f4pga/prjuray/.claude/worktrees/agent-ae3b9b22dac6da105/generic/python/ci_summary.py
bash $S/one.sh ss_base URAY_PARK=0 URAY_LEAFPAIR=0
bash $S/one.sh ss_leaf URAY_PARK=0 URAY_LEAFPAIR=1
bash $S/one.sh ss_park URAY_PARK=1 URAY_LEAFPAIR=0
bash $S/one.sh ss_both URAY_PARK=1 URAY_LEAFPAIR=1
for v in leaf park both; do
  echo "=== base -> $v"
  python3 $C compare $S/ss_base.summary.txt $S/ss_$v.summary.txt
done > $S/ss_cmp.txt 2>&1
echo DONE >> $S/ss_cmp.txt
