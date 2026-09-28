#!/bin/bash
# Bonded pad site lists for dies (build/meta/bonded/<die>.txt, read by
# features.py SiteKeys), one Vivado at a time under vrun.sh:
#   generic/dump_bonded.sh <die>...
# The part is the one of the die's tiles TSV (first line: "part <part> ...").
set -u
G=$(cd "$(dirname "$0")" && pwd)
B=${URAY_BUILD:-$(cd "$G/.." && pwd)/build}
mkdir -p "$B/meta/bonded" "$B/logs"
rc=0
for die in "$@"; do
    part=$(awk '$1 == "part" {print $2; exit}' "$B/meta/tiles/$die.tsv")
    if [ -z "$part" ]; then
        echo "$die: no part in $B/meta/tiles/$die.tsv"
        rc=2
        continue
    fi
    out=$B/meta/bonded/$die.txt
    log=$B/logs/bonded_$die.log
    "$G/vrun.sh" bonded-$die 3G bash -c \
        "source \${VIVADO_SETTINGS:-/opt/xilinx/Vivado/2025.2/settings64.sh} && \
         vivado -mode batch -nojournal -log $log.vivado \
         -source $G/tcl/dump_bonded.tcl -tclargs $part $out.tmp" \
        > "$log" 2>&1
    if [ -s "$out.tmp" ]; then
        mv "$out.tmp" "$out"
        echo "$die: $(wc -l < "$out") bonded pad sites"
    else
        echo "$die: failed, see $log"
        rc=1
    fi
done
exit $rc
