#!/bin/bash
# eq_check.sh <die> <tag> <max> <mem> [jobs]: check.py of this checkout vs
# $BASE (default generic-db) on the production DB (or URAY_DB): log and
# --fasm must be identical.
. "$(dirname "$0")/common.sh"
[ -d $S/old/generic ] || eq_tree_old
O=$S/old
S=$S/eq
export URAY_BUILD=$PROD
die=$1; tag=$2; n=$3; mem=$4; jobs=${5:-1}
for v in new ref; do
    flag=; P=$W/generic/python/check.py; [ $v = ref ] && P=$O/generic/python/check.py
    /usr/bin/time -f "$v wall %e s maxrss %M KB" -o $S/${die}_$v.time \
        $W/generic/vrun.sh tp-eq-$v $mem python3 $P --die $die \
        --designs $URAY_BUILD/designs/$die/$tag --max $n --jobs $jobs \
        --fasm $S/${die}_$v.fasm $flag > $S/${die}_$v.log 2>&1
    cat $S/${die}_$v.time; grep vrun: $S/${die}_$v.log
done
grep -v '^vrun:' $S/${die}_new.log > $S/${die}_new.cmp
grep -v '^vrun:' $S/${die}_ref.log > $S/${die}_ref.cmp
if cmp $S/${die}_new.cmp $S/${die}_ref.cmp && cmp $S/${die}_new.fasm $S/${die}_ref.fasm; then
    echo "$die IDENTICAL log ($(wc -l < $S/${die}_new.cmp) lines) + fasm ($(wc -l < $S/${die}_new.fasm) lines)"
else
    echo "$die DIFFERENT"
fi
