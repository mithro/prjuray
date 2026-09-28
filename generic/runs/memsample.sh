#!/bin/bash
# Sample memory of every scope in vivado.slice into a log every 20s.
# A scope can end between the glob and the reads; its failed cat is logged
# (stderr) and the scope skipped, instead of killing the sampler.
S=/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/vivado.slice
while true; do
  ts=$(date +%T)
  for d in $S/*.scope; do
    [ -d "$d" ] || continue
    cur=$(cat $d/memory.current) || continue
    peak=$(cat $d/memory.peak) || continue
    echo "$ts $(basename $d) cur $(( cur >> 20 ))M peak $(( peak >> 20 ))M"
  done
  echo "$ts slice $(( $(cat $S/memory.current) >> 30 ))G"
  sleep 20
done
