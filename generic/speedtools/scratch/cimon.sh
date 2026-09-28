#!/bin/bash
# cimon.sh <name> [interval]: ci.sh progress (mkdb log carries its own eta)
L=/tmp/claude-1000/sp_speed/speed/park/$1.log
M=/home/tim/github/f4pga/prjuray/build/ci/work/agent-ae3b9b22dac6da105/xcku025/mkdb.log
t0=$(date +%s)
while true; do
  echo "$(( ($(date +%s) - t0) / 60 )) min: ci [$(tail -1 $L | cut -c1-80)] mkdb [$(tail -1 $M | cut -c1-110)]"
  if grep -q "^EXIT" $L; then tail -25 $L; exit 0; fi
  sleep ${2:-300}
done
