#!/bin/bash
# gty.sh <name> [VAR=val...]: mkdb UltraScalePlus --types GTY_R,GTY_L on the GTY dies into park/<name>
# (own db dir; cache: hard-linked copy of the production one, so its
# replaced entries never touch production)
W=/home/tim/github/f4pga/prjuray/.claude/worktrees/agent-ae3b9b22dac6da105
B=/home/tim/github/f4pga/prjuray/build
S=/tmp/claude-1000/sp_speed/speed/park
n=$1
shift
for kv in "$@"; do export "$kv"; done
D=$S/$n
rm -rf $D
mkdir -p $D/UltraScalePlus
dies=xcau20p,xcku3p,xcu25
for d in ${dies//,/ }; do
  mkdir -p $D/UltraScalePlus/$d
  cp -p $B/db/UltraScalePlus/$d/tilegrid.json $D/UltraScalePlus/$d/
  if [ ! -d $S/gtycache/$d ]; then
    mkdir -p $S/gtycache
    cp -al $B/db/UltraScalePlus/cache/$d $S/gtycache/$d
  fi
done
for f in windows.json frames.json; do
  [ -f $B/db/UltraScalePlus/$f ] && cp -p $B/db/UltraScalePlus/$f $D/UltraScalePlus/
done
export PYTHONHASHSEED=0 URAY_BUILD=$B URAY_DB=$D
/usr/bin/time -v -o $D/time.log $W/generic/vrun.sh sp-gty ${VMEM:-6G} python3 $W/generic/python/mkdb.py --arch UltraScalePlus \
  --dies $dies --tag r4,r7,r9,r12reg,mem,gty1,hb_gty --jobs 2 --sample-mem-budget 6 --cache $S/gtycache --types GTY_R,GTY_L > $D/mkdb.log 2>&1
echo "EXIT $?" >> $D/mkdb.log
ls -la $D/UltraScalePlus >> $D/mkdb.log
