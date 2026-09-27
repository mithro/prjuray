#!/bin/bash
# Fast regression run ("CI"): does a code change alter the bit database or
# the check / prediction numbers of a small die?
#
#   generic/ci.sh [-u] [-t <tilegrid.json>] [-j <jobs>] [die...]
#
# Per die (default xa7s15; also xazu1eg, xcku025 are configured) it builds a
# single die bit database from fixed design tags with the current code
# (mkdb.py), checks fixed hold-out designs against it (check.py) and
# measures the per address prediction (evalpred.py), then compares the
# summary (python/ci_summary.py: database content hash and size, check and
# prediction totals, per tile type changes) with the golden summary.
# Exit status 0: identical to golden; 1: some value changed (read the
# table: fewer undocumented / missed bits is better); 2: a step failed.
#
# Everything lives under $URAY_BUILD/ci (never touches build/db):
#   ci/golden/<die>/{tilegrid.json,summary.txt,info}  pinned tile grid + golden
#   ci/work/<die>/                                     scratch tree, logs,
#                                                      persistent sample cache
# The tile grid is pinned in the golden directory (the production one when
# the golden is first made), so production tile grid updates do not move
# the baseline; -t runs with another tile grid instead (e.g. a candidate).
# -u makes the current results the golden ones (do this after merging a
# change whose numbers were reviewed).  Heavy steps run through vrun.sh
# (memory cap CI_MEM, default 8G).  Deterministic: PYTHONHASHSEED=0, fixed
# tags, and a design list frozen in the golden directory.
set -u
G=$(cd "$(dirname "$0")" && pwd)
B=${URAY_BUILD:-$(cd "$G/.." && pwd)/build}
update=0; tgopt=; jobs=16
while getopts "ut:j:" o; do
    case $o in
        u) update=1 ;;
        t) tgopt=$(realpath "$OPTARG") ;;
        j) jobs=$OPTARG ;;
        *) sed -n '2,25p' "$0"; exit 2 ;;
    esac
done
shift $((OPTIND - 1))
[ $# -eq 0 ] && set -- xa7s15
mem=${CI_MEM:-8G}
export PYTHONHASHSEED=0

# die -> "arch train_tags holdout_tag holdout_max"
conf() {
    case $1 in
        xa7s15) echo "Series7 r9,r10 r11 20" ;;
        xazu1eg) echo "UltraScalePlus r8,r9 r11 12" ;;
        xcku025) echo "UltraScale r8,r9 r11 8" ;;
        *) return 1 ;;
    esac
}

now() { date +%s.%N; }
el() { printf '%.0f' "$(echo "$(now) - $1" | bc)"; }

rc_all=0
for die in "$@"; do
    c=$(conf "$die") || { echo "ci: no configuration for $die"; exit 2; }
    read -r arch train hold hmax <<< "$c"
    GD=$B/ci/golden/$die
    X=$B/ci/work/$die
    mkdir -p "$GD" "$X"
    t0=$(now)
    # Pinned tile grid.
    if [ ! -f "$GD/tilegrid.json" ]; then
        cp "$B/db/$arch/$die/tilegrid.json" "$GD/tilegrid.json"
        echo "ci: $die: pinned the production tile grid"
    fi
    tg=${tgopt:-$GD/tilegrid.json}
    # Frozen design lists (designs added to a tag later are ignored).
    for t in $(echo "$train,$hold" | tr , ' '); do
        if [ ! -f "$GD/designs_$t.txt" ]; then
            ls -d "$B/designs/$die/$t"/s* | while read -r d; do
                [ -f "$d/bits.npz" ] && [ -f "$d/dump_v2" ] && basename "$d"
            done | sort -V > "$GD/designs_$t.txt"
        fi
    done
    # Scratch tree: meta, designs (symlinks of the frozen lists), tile grid.
    mkdir -p "$X/build/db/$arch/$die"
    [ -e "$X/build/meta" ] || ln -s "$B/meta" "$X/build/meta"
    for t in $(echo "$train,$hold" | tr , ' '); do
        rm -rf "$X/build/designs/$die/$t"
        mkdir -p "$X/build/designs/$die/$t"
        while read -r s; do
            ln -s "$B/designs/$die/$t/$s" "$X/build/designs/$die/$t/$s"
        done < "$GD/designs_$t.txt"
    done
    # Same content keeps the mtime (the sample cache stamp includes it).
    cmp -s "$tg" "$X/build/db/$arch/$die/tilegrid.json" ||
        cp "$tg" "$X/build/db/$arch/$die/tilegrid.json"
    for f in windows.json frames.json; do
        [ -f "$B/db/$arch/$f" ] && ! cmp -s "$B/db/$arch/$f" "$X/build/db/$arch/$f" &&
            cp -p "$B/db/$arch/$f" "$X/build/db/$arch/$f"
    done
    rm -f "$X/build/db/$arch"/{segbits,defaults,counts,unexplained}_* "$X/build/db/$arch/summary.json"
    XB=$X/build XD=$X/build/db
    t1=$(now)
    URAY_BUILD=$XB URAY_DB=$XD "$G/vrun.sh" "ci-mkdb-$die" "$mem" python3 "$G/python/mkdb.py" --arch "$arch" --dies "$die" \
        --tag "$train" --jobs "$jobs" --cache "$X/cache" > "$X/mkdb.log" 2>&1 ||
        { echo "ci: $die: mkdb failed, see $X/mkdb.log"; rc_all=2; continue; }
    tm=$(el "$t1"); t1=$(now)
    URAY_BUILD=$XB URAY_DB=$XD "$G/vrun.sh" "ci-check-$die" "$mem" python3 "$G/python/check.py" --die "$die" \
        --designs "$XB/designs/$die/$hold" --max "$hmax" --jobs 4 > "$X/check.log" 2>&1
    grep -q '^unowned total' "$X/check.log" ||
        { echo "ci: $die: check failed, see $X/check.log"; rc_all=2; continue; }
    tc=$(el "$t1"); t1=$(now)
    # evalpred on the same hold-out designs (first hmax).
    mkdir -p "$X/hold"
    rm -f "$X/hold"/s*
    head -n "$hmax" "$GD/designs_$hold.txt" | while read -r s; do
        ln -s "$B/designs/$die/$hold/$s" "$X/hold/$s"
    done
    URAY_BUILD=$XB URAY_DB=$XD "$G/vrun.sh" "ci-pred-$die" "$mem" python3 "$G/speedtools/evalpred.py" --jobs 4 --all \
        "$XD/$arch" "$die" "$X/hold" > "$X/evalpred.log" 2>&1 ||
        { echo "ci: $die: evalpred failed, see $X/evalpred.log"; rc_all=2; continue; }
    tp=$(el "$t1")
    python3 "$G/python/ci_summary.py" summarize "$XD/$arch" "$X/check.log" \
        "$X/evalpred.log" > "$X/summary.txt"
    echo "== ci $die ($arch; train $train, hold-out $hold x$hmax${tgopt:+, tile grid $tgopt}):" \
        "mkdb $(grep -o '# samples of .*' "$X/mkdb.log" | sed 's/# //') total ${tm} s," \
        "check ${tc} s, evalpred ${tp} s, all $(el "$t0") s"
    if [ $update = 1 ] || [ ! -f "$GD/summary.txt" ]; then
        cp "$X/summary.txt" "$GD/summary.txt"
        echo "$(date -Is) $(git -C "$G" rev-parse --short HEAD) tilegrid $(sha256sum "$tg" | cut -c1-16)" >> "$GD/info"
        echo "ci: $die: golden summary updated"
        grep -E '^(db\.(hash|features)|check\.(unowned|distinct)|pred\.(set|missed|false)) ' "$GD/summary.txt"
        continue
    fi
    python3 "$G/python/ci_summary.py" compare "$GD/summary.txt" "$X/summary.txt"
    r=$?
    [ $r -gt $rc_all ] && rc_all=$r
done
exit $rc_all
