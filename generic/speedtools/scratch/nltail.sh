#!/bin/bash
# nltail.sh <die> <tag> [n]: last lines of each design's nl.log (noise filtered) + status
D=/home/tim/github/f4pga/prjuray/build/designs/$1/$2
for s in $D/s*; do
  echo "== $(basename $s) $(grep -o '"status": "[a-z_ ]*"' $s/run.stats 2>&1)"
  grep -v "^err \|^properr\|^disconn\|^tying" $s/nl.log 2>&1 | tail -${3:-4} | cut -c1-220
  [ -f $s/gen.err ] && tail -3 $s/gen.err
done
