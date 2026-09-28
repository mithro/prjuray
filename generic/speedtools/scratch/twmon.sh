#!/bin/bash
# twmon.sh: progress of twinci.sh (6 ci runs) every minute
S=/tmp/claude-1000/sp_speed/speed/park
t0=$(date +%s)
while true; do
  n=$(find $S -maxdepth 1 -name 'tw_*.summary.txt' -newer $S/twinci.sh | wc -l)
  el=$(( $(date +%s) - t0 ))
  eta=unknown
  if [ $n -gt 0 ]; then
    rem=$(( el * (6 - n) / n ))
    eta="$(( rem / 60 )) min ($(date -d @$(( $(date +%s) + rem )) +%H:%M))"
  fi
  echo "$(( el / 60 )) min: $n/6 ci runs, ETA $eta"
  if grep -qs DONE $S/tw_cmp.txt; then
    grep "===\|^db.hash\|^db.features\|^check.undoc\|^check.distinct\|^pred.missed\|^pred.false" $S/tw_cmp.txt
    exit 0
  fi
  sleep 60
done
