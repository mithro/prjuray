#!/bin/bash
# rdmon.sh <logname> <die> <tag> <ndesigns> [interval]: progress + ETA of an rd.sh run
L=/tmp/claude-1000/sp_speed/speed/$1.log
D=/home/tim/github/f4pga/prjuray/build/designs/$2/$3
N=$4
iv=${5:-60}
t0=$(date +%s)
while true; do
  n=$(find $D -mindepth 2 -maxdepth 2 -name run.stats | wc -l)
  el=$(( $(date +%s) - t0 ))
  eta="ETA unknown"
  if [ $n -gt 0 ]; then
    rem=$(( el * (N - n) / n ))
    eta="ETA $(( rem / 60 )) min ($(date -d @$(( $(date +%s) + rem )) +%H:%M))"
  fi
  st=$(find $D -mindepth 2 -maxdepth 2 -name run.stats -exec cat {} + | grep -o '"status": "[a-z_]*"' | sort | uniq -c | tr -s ' \n' ' ')
  echo "$(( el / 60 )) min: $n/$N done [$st] $eta"
  if grep -q "^EXIT" $L; then tail -2 $L; exit 0; fi
  sleep $iv
done
