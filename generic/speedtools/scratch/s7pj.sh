#!/bin/bash
# s7pj.sh <name> [mkdb args...]: Series7 BRAM_L,BRAM_R mkdb (production designs, hard-linked cache copy)
W=/home/tim/github/f4pga/prjuray/.claude/worktrees/agent-ae3b9b22dac6da105
B=/home/tim/github/f4pga/prjuray/build
S=/tmp/claude-1000/sp_speed/speed/park
n=$1
shift
D=$S/$n
rm -rf $D
mkdir -p $D/Series7
dies=xa7s15,xa7a12t,xa7s25,xa7a15t,xa7z010,xc7z012s,xa7s50,xa7z020,xa7a100t,xa7s100,xa7z030,xc7k70t,xc7k160t
for d in ${dies//,/ }; do
  mkdir -p $D/Series7/$d
  cp -p $B/db/Series7/$d/tilegrid.json $D/Series7/$d/
  if [ ! -d $S/s7cache/$d ]; then
    mkdir -p $S/s7cache
    cp -al $B/db/Series7/cache/$d $S/s7cache/$d
  fi
done
for f in windows.json frames.json; do
  [ -f $B/db/Series7/$f ] && cp -p $B/db/Series7/$f $D/Series7/
done
export PYTHONHASHSEED=0 URAY_BUILD=$B URAY_DB=$D
/usr/bin/time -v -o $D/time.log $W/generic/vrun.sh sp-s7pj ${VMEM:-16G} python3 $W/generic/python/mkdb.py --arch Series7 \
  --dies $dies --tag r3,r4,r5,r7,r8,r9,r10,r11,r12,mmcm1,hb_gtx2,gtx3 --sample-mem-budget 13 --cache $S/s7cache \
  --types BRAM_L,BRAM_R "$@" > $D/mkdb.log 2>&1
echo "EXIT $?" >> $D/mkdb.log
