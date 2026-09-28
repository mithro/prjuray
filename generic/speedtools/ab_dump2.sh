#!/bin/bash
# ab_dump2.sh <dir with design.dcp> <refdump.tcl> <newdump.tcl> <label>
# A/B of two dump_features.tcl versions on a routed checkpoint (written by a
# run with NL_DCP=1): dumps with both (single thread, memory capped through
# viv.sh), prints the dump times and compares the feature lines.
d=$1; ref=$2; new=$3; l=$4
T=$(dirname $(readlink -f $0))
bash $T/viv.sh $d r_$l.log -source $T/profdump2.tcl -tclargs $ref design.dcp 1 fr_$l.txt
bash $T/viv.sh $d n_$l.log -source $T/profdump2.tcl -tclargs $new design.dcp 1 fn_$l.txt
echo "== $d ref $(grep '^PROF total' $d/r_$l.log) new $(grep '^PROF total' $d/n_$l.log)"
python3 $T/cmpdump.py $d/fr_$l.txt $d/fn_$l.txt
echo "RC $?"
