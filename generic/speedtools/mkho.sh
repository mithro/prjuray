#!/bin/bash
# mkho.sh <label> <python dir>: hold-out test on the 2 die snapshot: mkdb on r2-r5,
# check.py on r7 (not used for the database); memory capped
label=$1; pdir=$2; shift 2
S=${SP:-/tmp/speedtools}
SB=$S/sb2
out=$SB/db/Series7
W=$(cd $(dirname $0)/../.. && pwd)
mkdir -p $S/mkho
rm -f $out/segbits_* $out/defaults_* $out/counts_* $out/unexplained_* $out/summary.json
cd $pdir || exit 1
URAY_BUILD=$SB $W/generic/vrun.sh sp-mkdb ${VMEM:-6G} python3 mkdb.py --arch Series7 --dies xa7s15,xa7a12t --tag r2,r3,r4,r5 --jobs 2 --cache $S/mkcache_sb2 "$@" > $S/mkho/$label.log 2>&1
echo "EXIT $?" >> $S/mkho/$label.log
rm -rf $S/mkho/$label.out
mkdir -p $S/mkho/$label.out
cp $out/segbits_* $out/defaults_* $out/counts_* $out/unexplained_* $out/summary.json $S/mkho/$label.out/
for d in xa7s15 xa7a12t; do
  URAY_BUILD=$SB $W/generic/vrun.sh sp-check ${VMEM:-6G} python3 check.py --die $d --designs $SB/designs/$d/r7 --max 40 --jobs 2 > $S/mkho/$label.check_$d 2>&1
done
