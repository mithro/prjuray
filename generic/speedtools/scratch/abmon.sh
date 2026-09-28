#!/bin/bash
# abmon.sh <tag>: progress of ab.sh every 5 min (2 ci runs, ~12-15 min each)
S=/tmp/claude-1000/sp_speed/speed/park
M=/home/tim/github/f4pga/prjuray/build/ci/work/agent-ae3b9b22dac6da105/xcku025/mkdb.log
t0=$(date +%s)
while true; do
  el=$(( ($(date +%s) - t0) / 60 ))
  st="p0 running"
  [ -f $S/$1_p0.summary.txt ] && st="p0 done, p1 running"
  echo "$el min: $st (ETA ~$(( 30 - el )) min, $(date -d @$(( t0 + 1800 )) +%H:%M)); mkdb: $(tail -1 $M | cut -c1-90)"
  if grep -q DONE $S/$1_cmp.txt 2>&1; then cat $S/$1_cmp.txt; exit 0; fi
  sleep 300
done
