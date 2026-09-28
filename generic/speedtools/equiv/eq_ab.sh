#!/bin/bash
# eq_ab.sh: mkdb v3 cache + task reuse vs generic-db mkdb, byte-identical DBs.
#   old cold / new cold / new warm / feature edit (one tile type) / forced split
set -u
. "$(dirname "$0")/common.sh"
X=$S/ab${SUF:-}
arch=${ARCH:-Series7}; dies=${DIES:-xa7s15,xa7a12t}; tags=${TAGS:-r9,r10}; jobs=8; mem=10G
rm -rf $X; mkdir -p $X
python3 $W/generic/speedtools/mksbuild.py $X/build $arch $dies $tags $PROD
# code trees: old = generic-db, new = this worktree (uncommitted state)
mkdir -p $X/old $X/new
git -C $W archive $BASE generic | tar -x -C $X/old
rsync -a --exclude __pycache__ $W/generic/ $X/new/generic/
export URAY_BUILD=$X/build PYTHONHASHSEED=0
run() {  # run <name> <tree> <db> <cache> [env...]
    local name=$1 tree=$2 db=$3 cache=$4; shift 4
    if [ ! -d $X/$db ]; then mkdir -p $X/$db; cp -r $X/build/db/$arch $X/$db/; fi
    env "$@" URAY_DB=$X/$db /usr/bin/time -f "%e" -o $X/$name.time $W/generic/vrun.sh tp-ab-$name $mem \
        python3 $X/$tree/generic/python/mkdb.py --arch $arch --dies $dies --tag $tags --jobs $jobs \
        --cache $X/$cache > $X/$name.log 2>&1
    echo "$name rc=$? wall $(cat $X/$name.time) s; $(grep -h '# samples of' $X/$name.log | sed 's/# //'); $(grep -h 'tasks reused' $X/$name.log | sed 's/# //') $(grep -o 'peak [0-9]* ([0-9]* GiB)' $X/$name.log | tail -n 1)"
}
cmpdb() {  # cmpdb <db a> <db b>
    if diff -r -x .mkdb_tasks $X/$1 $X/$2 > $X/diff_$1_$2.txt; then
        echo "  $1 == $2 IDENTICAL ($(ls $X/$2/$arch | wc -l) entries)"
    else
        echo "  $1 != $2 DIFFERENT ($(wc -l < $X/diff_$1_$2.txt) diff lines)"; head -n 5 $X/diff_$1_$2.txt
    fi
}
run old_cold old db_old c_old
run new_cold new db_new c_new
cmpdb db_old db_new
run new_warm new db_new c_new
cmpdb db_old db_new
# feature code edit touching one tile type (DSP): a new feature on used DSP48 sites
for t in old new; do
    python3 - $X/$t/generic/python/features.py ${EDIT_PFX:-DSP48_X0Y0} <<'EOF'
import sys
p = sys.argv[1]
pfx = sys.argv[2]
s = open(p).read()
a = '    leaf_clock_features(feats, sitekeys)\n'
assert s.count(a) == 1
s = s.replace(a, a + f'''    PFX = {pfx!r}
    for tile, fs in feats.items():
        if any('.TYPE.' in f and f.startswith(PFX) for f in fs):
            fs.add('EQTEST.DSP0_USED')
''')
open(p, 'w').write(s)
EOF
done
run old_edit old db_old_e c_old
run new_edit new db_new c_new
cmpdb db_old_e db_new
# forced split tasks
run old_split old db_old_s c_old MKDB_SPLIT_COST=1e9 MKDB_PAIR_CHUNK=20
run new_split new db_new_s c_new MKDB_SPLIT_COST=1e9 MKDB_PAIR_CHUNK=20
cmpdb db_old_s db_new_s
run new_split_warm new db_new_s c_new MKDB_SPLIT_COST=1e9 MKDB_PAIR_CHUNK=20
cmpdb db_old_s db_new_s
grep -c "split into" $X/new_split.log
