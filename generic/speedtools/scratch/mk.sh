#!/bin/bash
# mk.sh <name> [VAR=val...]: mkdb xcku025 r8,r9 --types INT into park/<name>/UltraScale (ci scratch tree, own db dir)
W=/home/tim/github/f4pga/prjuray/.claude/worktrees/agent-ae3b9b22dac6da105
X=/home/tim/github/f4pga/prjuray/build/ci/work/agent-ae3b9b22dac6da105/xcku025
S=/tmp/claude-1000/sp_speed/speed/park
n=$1
shift
for kv in "$@"; do export "$kv"; done
D=$S/$n
rm -rf $D
mkdir -p $D/UltraScale/xcku025
cp $X/build/db/UltraScale/xcku025/tilegrid.json $D/UltraScale/xcku025/
for f in windows.json frames.json; do
  [ -f $X/build/db/UltraScale/$f ] && cp -p $X/build/db/UltraScale/$f $D/UltraScale/
done
export PYTHONHASHSEED=0 URAY_BUILD=$X/build URAY_DB=$D
$W/generic/vrun.sh sp-mk ${VMEM:-8G} python3 $W/generic/python/mkdb.py --arch UltraScale --dies xcku025 \
  --tag r8,r9 --jobs 8 --cache $X/cache --types ${TYPES:-INT} ${MKARGS:-} > $D/mkdb.log 2>&1
echo "EXIT $?" >> $D/mkdb.log
