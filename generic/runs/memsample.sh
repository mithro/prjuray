#!/bin/bash
# Sample memory of every scope in vivado.slice into a log every 20s.
S=/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/vivado.slice
while true; do
  ts=$(date +%T)
  for d in $S/*.scope; do
    [ -d "$d" ] || continue
    echo "$ts $(basename $d) cur $(( $(cat $d/memory.current) >> 20 ))M peak $(( $(cat $d/memory.peak) >> 20 ))M"
  done
  echo "$ts slice $(( $(cat $S/memory.current) >> 30 ))G"
  sleep 20
done
