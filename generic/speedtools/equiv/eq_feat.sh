#!/bin/bash
# eq_feat.sh <base features.py> die tag n [die tag n ...]: tile_features of
# this checkout vs <base features.py> (e.g. $S/old/generic/python/features.py
# after eq_tree_old; its directory needs clockgen_tables.json).
. "$(dirname "$0")/common.sh"
export URAY_BUILD=$PROD
base=$1; shift
while [ $# -ge 3 ]; do
    $W/generic/vrun.sh tp-eqf 4G python3 $(dirname "$0")/eq_feat.py $base $1 $2 $3 > $S/eq/feat_$1.log 2>&1
    echo "$1: $(tail -n 2 $S/eq/feat_$1.log | head -n 1)"
    shift 3
done
