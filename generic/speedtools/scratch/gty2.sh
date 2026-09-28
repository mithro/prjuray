#!/bin/bash
# gty2.sh: GTY mkdb, twin guard default (1000) vs off, concurrently on the same designs
S=/tmp/claude-1000/sp_speed/speed/park
VMEM=8G bash $S/gty.sh gty_on1000 > $S/gty_on1000.out 2>&1 &
VMEM=8G bash $S/gty.sh gty_off2 MKDB_TWIN_MAX=1000000000 > $S/gty_off2.out 2>&1 &
wait
cd $S
for v in on1000 off2; do
  echo "== $v"
  grep "^GTY\|EXIT" gty_$v/mkdb.log | cut -c1-200
  grep "Elapsed" gty_$v/time.log
  ls -la gty_$v/UltraScalePlus | grep "segbits_gty"
  grep -c ambiguous gty_$v/UltraScalePlus/unexplained_gty_r.txt gty_$v/UltraScalePlus/unexplained_gty_l.txt
done
