#!/bin/bash
# pj.sh: xcku025 BRAM,INT mkdb without / with --pair-jobs 8; identical output?
S=/tmp/claude-1000/sp_speed/speed/park
VMEM=14G TYPES=BRAM,INT bash $S/mk.sh pj0 URAY_PARK=0
VMEM=14G TYPES=BRAM,INT MKARGS="--pair-jobs 8" bash $S/mk.sh pj8 URAY_PARK=0
for v in pj0 pj8; do
  echo "== $v"
  grep "vrun\|EXIT\|elapsed" $S/$v/mkdb.log | tail -3
  grep -c "pairs" $S/$v/mkdb.log
done
diff -r -q -x mkdb.log -x '.mkdb_tasks' -x 'cache' $S/pj0/UltraScale $S/pj8/UltraScale && echo IDENTICAL
