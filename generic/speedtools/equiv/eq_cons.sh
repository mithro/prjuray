#!/bin/bash
# eq_cons.sh <die> <tag> <max> <mem>: consistency.py new vs old (snapshot DB)
. "$(dirname "$0")/common.sh"
[ -d $S/old/generic ] || eq_tree_old
E=$S/eq
export URAY_BUILD=$PROD URAY_DB=$S/dbsnap
die=$1; tag=$2; n=$3; mem=$4
for v in new old; do
    P=$W/generic/python/consistency.py; [ $v = old ] && P=$S/old/generic/python/consistency.py
    /usr/bin/time -f "$v wall %e s" -o $E/cons_${die}_$v.time $W/generic/vrun.sh tp-cons-$v $mem \
        python3 $P --die $die --designs $URAY_BUILD/designs/$die/$tag --max $n --min-occ 2 --min-viol 1 --share 0.001 --top 50 > $E/cons_${die}_$v.log 2>&1
    cat $E/cons_${die}_$v.time
    grep -v vrun: $E/cons_${die}_$v.log > $E/cons_${die}_$v.cmp
done
cmp $E/cons_${die}_new.cmp $E/cons_${die}_old.cmp && echo "$die consistency IDENTICAL ($(wc -l < $E/cons_${die}_new.cmp) lines, $(grep -c SUSPECT $E/cons_${die}_new.cmp) suspects)"
