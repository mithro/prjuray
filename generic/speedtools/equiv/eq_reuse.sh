#!/bin/bash
# eq_reuse.sh <arch> <dies> <tags> <edit site prefix>: mkdb sample chunk
# reuse after a derived feature edit (a new feature on the tiles with a site
# <prefix>) must give byte identical databases to a full re-encode
# (URAY_CHUNK_REUSE=0) and to $BASE with the same edit; prints the sample
# phase times.
#   eq_reuse.sh Series7 xa7s15,xa7a12t r9,r10 DSP48_X0Y0
set -u
. "$(dirname "$0")/common.sh"
[ -d $S/old/generic ] || eq_tree_old
arch=$1; dies=$2; tags=$3; pfx=$4
X=$S/reuse_$arch
rm -rf $X; mkdir -p $X
python3 $W/generic/speedtools/mksbuild.py $X/build $arch $dies $tags $PROD
mkdir -p $X/new $X/old
rsync -a --exclude __pycache__ $W/generic/ $X/new/generic/
rsync -a --exclude __pycache__ $S/old/generic/ $X/old/generic/
export URAY_BUILD=$X/build PYTHONHASHSEED=0
run() {  # run <name> <tree> <db> <cache> [env...]
    local name=$1 tree=$2 db=$3 cache=$4; shift 4
    [ -d $X/$db ] || { mkdir -p $X/$db; cp -r $X/build/db/$arch $X/$db/; }
    env "$@" URAY_DB=$X/$db /usr/bin/time -f "%e" -o $X/$name.time $W/generic/vrun.sh tp-reuse-$name 12G \
        python3 $X/$tree/generic/python/mkdb.py --arch $arch --dies $dies --tag $tags --jobs 8 \
        --cache $X/$cache > $X/$name.log 2>&1
    echo "$name rc=$? wall $(cat $X/$name.time) s; $(grep -h '# samples of' $X/$name.log | sed 's/# //'); $(grep -h 'tasks reused' $X/$name.log | sed 's/# //')"
}
same() {
    diff -r -x .mkdb_tasks $X/$1 $X/$2 > $X/diff_$1_$2.txt && echo "  $1 == $2 IDENTICAL" ||
        { echo "  $1 != $2 DIFFERENT ($(wc -l < $X/diff_$1_$2.txt) lines)"; head -n 4 $X/diff_$1_$2.txt; }
}
run cold new db0 c0
for t in new old; do
    python3 - $X/$t/generic/python/features.py $pfx <<'EOF'
import sys
p, pfx = sys.argv[1], sys.argv[2]
s = open(p).read()
a = '    leaf_clock_features(feats, sitekeys)\n'
assert s.count(a) == 1
s = s.replace(a, a + f"""    for tile, fs in feats.items():
        if any('.TYPE.' in f and f.startswith({pfx!r}) for f in fs):
            fs.add('EQTEST.EDIT')
""")
open(p, 'w').write(s)
EOF
done
for c in c1 c2 c3; do cp -r $X/c0 $X/$c; done
for db in d1 d2 d3; do cp -r $X/db0 $X/$db; done
run full new d2 c2 URAY_CHUNK_REUSE=0
run full_again new d3 c3 URAY_CHUNK_REUSE=0
run reuse new d1 c1
run base old dbase cbase
same d1 d2
same d1 dbase
