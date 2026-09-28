#!/bin/bash
# Compare a candidate tile grid against the installed one by the undocumented
# bits of single die databases built with each (memory capped via vrun.sh).
#
#   generic/tg_eval.sh <variant> <die> <candidate tilegrid.json> [train tags] [check tag]
#
# Builds $URAY_BUILD/tilegrid_exp/dbeval/{inst_<die>,<variant>_<die>} (the
# installed baseline is built once and reused), runs check.py on 20 designs
# of the check tag with both (TG_CHECK_MEM / TG_CHECK_JOBS: check.py memory
# cap and jobs, default 8G / 6: check.py peaks at ~3.7 GB with 3 jobs on xcku025) and prints the distinct undocumented bits and
# the per tile type differences.  Defaults: train r1..r5, check r5.
set -u
variant=$1; die=$2; cand=$3; tags=${4:-r1,r2,r3,r4,r5}; ctag=${5:-r5}
G=$(cd "$(dirname "$0")" && pwd)
B=${URAY_BUILD:-$(cd "$G/.." && pwd)/build}
export URAY_BUILD=$B PYTHONHASHSEED=0
arch=$(python3 -c "import sys; sys.path.insert(0, '$G/python'); import dies; print(dies.load()['$die'].arch)")
mkdir -p "$B/logs"

one() {  # one <name> <tilegrid.json>
    local E=$B/tilegrid_exp/dbeval/$1
    [ -f "$E/check_$die.log" ] && return 0
    mkdir -p "$E/$arch/$die"
    cp "$2" "$E/$arch/$die/tilegrid.json"
    URAY_DB=$E "$G/vrun.sh" "tg-mkdb-$1" 8G python3 "$G/python/mkdb.py" --arch "$arch" \
        --dies "$die" --tag "$tags" --jobs 6 > "$B/logs/tg_mkdb_$1.log" 2>&1 ||
        { echo "mkdb $1 failed: $(tail -n 1 "$B/logs/tg_mkdb_$1.log")"; return 1; }
    # check.py exits 1 whenever there are undocumented bits: judge by its
    # summary line instead
    URAY_DB=$E "$G/vrun.sh" "tg-check-$1" ${TG_CHECK_MEM:-8G} python3 "$G/python/check.py" --die "$die" \
        --designs "$B/designs/$die/$ctag" --max 20 --jobs ${TG_CHECK_JOBS:-6} > "$E/check_$die.log.tmp" 2>&1
    grep -q '^unowned total' "$E/check_$die.log.tmp" ||
        { echo "check $1 failed: $(tail -n 1 "$E/check_$die.log.tmp")"; return 1; }
    mv "$E/check_$die.log.tmp" "$E/check_$die.log"
}

one "inst_$die" "$B/db/$arch/$die/tilegrid.json" || exit 1
one "${variant}_$die" "$cand" || exit 1
echo "== $die"
python3 "$G/python/check_cmp.py" "$B/tilegrid_exp/dbeval/inst_$die/check_$die.log" \
    "$B/tilegrid_exp/dbeval/${variant}_$die/check_$die.log" 12
