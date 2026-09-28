#!/bin/bash
# one.sh <name> [VAR=val...]: one ci.sh xcku025 run, summary compared with golden, saved
S=/tmp/claude-1000/sp_speed/speed/park
X=/home/tim/github/f4pga/prjuray/build/ci/work/agent-ae3b9b22dac6da105/xcku025
n=$1
shift
CI_MEM=16G bash $S/ci.sh $n "$@"
cp $X/summary.txt $S/$n.summary.txt
cp $X/check.log $S/$n.check.log
rm -rf $S/db_$n
cp -a $X/build/db/UltraScale $S/db_$n
grep "^check.undocumented\|^check.distinct\|^pred.missed\|^pred.false\|^db.features\|^== ci\|EXIT" $S/$n.log
