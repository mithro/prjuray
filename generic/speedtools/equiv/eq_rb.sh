#!/bin/bash
# eq_rb.sh <mem> die tag n [die tag n ...]: Collector.region_bits / unowned
# of this checkout vs $BASE on n designs per die (production tile grids).
#
# Equivalence scripts (all compare this checkout with $BASE, default
# generic-db; scratch in $EQ_SCRATCH, default <build>/equiv/<checkout>, see
# common.sh; heavy steps through vrun.sh):
#   eq_rb.sh      region_bits / unowned
#   eq_feat.sh    features.tile_features ({tile: features})
#   eq_check.sh   check.py log + FASM
#   eq_ev.sh      evalpred.py (snap.sh <arch> first, or set URAY_DB)
#   eq_cons.sh    consistency.py (snap.sh first)
#   eq_ab.sh      mkdb: diff -r of DBs cold / warm / one tile type feature
#                 edit / forced split tasks (ARCH, DIES, TAGS, EDIT_PFX, SUF)
#   budget_eq.sh  mkdb --sample-mem-budget vs none
. "$(dirname "$0")/common.sh"
export URAY_BUILD=$PROD
[ -d $S/old/generic ] || eq_tree_old
mem=$1; shift
while [ $# -ge 3 ]; do
    $W/generic/vrun.sh tp-eqrb $mem python3 $(dirname "$0")/eq_rb.py $1 $2 $3 > $S/eq/rb_$1.log 2>&1
    echo "$1 rc=$?: $(tail -n 2 $S/eq/rb_$1.log | head -n 1)"
    shift 3
done
