#!/bin/bash
# eq_ev.sh <arch> <die> <tag> <show type> <mem> [jobs]: new evalpred vs old
# (both print every type and every missed bit); sorted outputs must match.
# Old code before d266e1e: set EVOPT="--max-owners 3".  URAY_DB: the
# database root (default: snapshot from snap.sh).
. "$(dirname "$0")/common.sh"
[ -d $S/old/generic ] || eq_tree_old
O=$S/old
sed 's/key=lambda x: -x\[1\]\[.missed.\])\[:30\]:/key=lambda x: -x[1]["missed"]):/; s/missed_bits.most_common(40)/missed_bits.most_common()/' $O/generic/speedtools/evalpred.py > $O/generic/speedtools/evalpred_all.py
# an old evalpred with --all prints everything itself
OLDOPT=; grep -q -- "'--all'" $O/generic/speedtools/evalpred.py && OLDOPT="--all --jobs ${6:-1}"
E=$S/eq
export URAY_BUILD=$PROD
export URAY_DB=${URAY_DB:-$S/dbsnap}
arch=$1; die=$2; tag=$3; show=$4; mem=$5; jobs=${6:-1}
R=$URAY_BUILD/designs/$die/$tag
/usr/bin/time -f "new wall %e s" -o $E/ev_${die}_new.time $W/generic/vrun.sh tp-ev-new $mem \
    python3 $W/generic/speedtools/evalpred.py $EVOPT --missed $show --jobs $jobs --all $URAY_DB/$arch $die $R > $E/ev_${die}_new.log 2>&1
cat $E/ev_${die}_new.time; grep vrun: $E/ev_${die}_new.log
/usr/bin/time -f "old wall %e s" -o $E/ev_${die}_old.time $W/generic/vrun.sh tp-ev-old $mem \
    python3 $O/generic/speedtools/evalpred_all.py $OLDOPT --missed $show $URAY_DB/$arch $die $R > $E/ev_${die}_old.log 2>&1
cat $E/ev_${die}_old.time; grep vrun: $E/ev_${die}_old.log
grep -v vrun: $E/ev_${die}_new.log | sort > $E/ev_${die}_new.cmp
grep -v vrun: $E/ev_${die}_old.log | sort > $E/ev_${die}_old.cmp
if cmp $E/ev_${die}_new.cmp $E/ev_${die}_old.cmp; then
    echo "$die IDENTICAL (sorted, $(wc -l < $E/ev_${die}_new.cmp) lines)"; head -n 1 $E/ev_${die}_new.log
else
    echo "$die DIFFERENT"; diff $E/ev_${die}_old.cmp $E/ev_${die}_new.cmp | head -n 20
fi
