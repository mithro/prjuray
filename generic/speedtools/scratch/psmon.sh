#!/bin/bash
# One line per 60 s: phase / last overlap count of each pstest run, ETA vs the
# ~6-10 min an unhung region design takes; exits when both are done.
S=/tmp/claude-1000/sp_speed/speed/pstest
t0=$(date +%s)
while true; do
  line="$(( ($(date +%s) - t0) / 60 )) min:"
  alldone=1
  for d in s4_psfix s8_psfix; do
    L=$S/$d/vivado.log
    if [ -f $S/$d/done ]; then
      line="$line $d DONE($(cat $S/$d/done); $(tail -1 $S/$d/nl.log));"
      continue
    fi
    alldone=0
    ph=$(grep "^Phase [0-9]" $L | tail -1 | cut -c1-40)
    ov=$(grep -c "Nodes with overlaps" $L)
    last=$(grep "Nodes with overlaps" $L | tail -1 | tr -dc 0-9)
    line="$line $d [$ph] overlap-lines $ov last $last;"
  done
  echo "$line ETA ~$(( 10 - ($(date +%s) - t0) / 60 )) min if not hung (finish ~$(date -d @$((t0 + 600)) +%H:%M))"
  [ $alldone = 1 ] && exit 0
  sleep 60
done
