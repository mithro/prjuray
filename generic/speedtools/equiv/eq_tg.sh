#!/bin/bash
# eq_tg.sh <die> <tags> <mem>: tilegrid.py --evidence (activity evidence and
# activity tile grid) of this checkout vs $BASE: evidence.json and the tile
# grid must be byte identical.  TGOPT: extra options of the new run (e.g.
# --jobs 8).
. "$(dirname "$0")/common.sh"
[ -d $S/old/generic ] || eq_tree_old
export URAY_BUILD=$PROD
die=$1; tags=$2; mem=$3
roots=$(echo "$tags" | tr , '\n' | sed "s|^|$PROD/designs/$die/|" | paste -sd,)
E=$S/eq/tg_$die
mkdir -p $E
for v in new old; do
    T=$W; [ $v = old ] && T=$S/old
    /usr/bin/time -f "$v wall %e s" -o $E/$v.time $W/generic/vrun.sh tp-tg-$v $mem \
        python3 $T/generic/python/tilegrid.py --die $die --designs $roots $([ $v = new ] && echo "$TGOPT") \
        --evidence $E/evidence_$v.json --out $E/tilegrid_$v.json > $E/$v.log 2>&1
    echo "$(cat $E/$v.time) $(grep -o 'peak .*' $E/$v.log)"
done
if cmp $E/evidence_old.json $E/evidence_new.json && cmp $E/tilegrid_old.json $E/tilegrid_new.json; then
    echo "$die evidence + tile grid IDENTICAL"
else
    echo "$die DIFFERENT"
fi
