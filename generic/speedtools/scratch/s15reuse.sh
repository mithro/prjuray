#!/bin/bash
# keep-mode crash repro on xa7s15 (two r12 designs), plain vs param variant
cd /tmp/claude-1000/sp_speed/speed
B=/home/tim/github/f4pga/prjuray/build/designs/xa7s15/r12
VMEM=3G bash rtreuse.sh keep $B/s1 $B/s2 s15keep > rtr_s15k.out 2>&1 &
sleep 2
VMEM=3G bash rtreuse.sh keep $B/s1 $B/s2 s15bypass RTR_PARAM=physdb.placeStorage.bypassNewRoutedSiteCalls=1 > rtr_s15b.out 2>&1 &
wait
for m in s15keep s15bypass; do
  echo "== $m $(cat rtreuse/$m/done)"
  grep "^RTR" rtreuse/$m/run.log
  grep -h "^routed\|^done\|^written" rtreuse/$m/a/nl.log rtreuse/$m/b/nl.log | tr '\n' ' '
  echo
  grep -c "Abnormal" rtreuse/$m/run.log
done
