#!/bin/bash
# budget_eq.sh: mkdb with and without --sample-mem-budget on a scratch
# build (xa7s15+xa7a12t r9,r10), fresh caches: diff -r of the DBs; then a
# second cold budget run starting from the persisted worker peaks.
. "$(dirname "$0")/common.sh"
X=$S/budget
python3 $W/generic/speedtools/mksbuild.py $X/build Series7 xa7s15,xa7a12t r9,r10 $PROD
export URAY_BUILD=$X/build PYTHONHASHSEED=0
for v in nob bud bud2; do
    rm -rf $X/db_$v; mkdir -p $X/db_$v/Series7
    [ $v = bud2 ] || rm -rf $X/cache_$v
    [ $v = bud2 ] && { rm -rf $X/cache_bud2; mkdir -p $X/cache_bud2; cp $X/cache_bud/worker_peaks.json $X/cache_bud2/; }
    for d in xa7s15 xa7a12t; do mkdir -p $X/db_$v/Series7/$d; cp -p $X/build/db/Series7/$d/tilegrid.json $X/db_$v/Series7/$d/; done
    opt=; [ $v != nob ] && opt="--sample-jobs 16 --sample-mem-budget 9"
    URAY_DB=$X/db_$v $W/generic/vrun.sh tp-bud 12G python3 $W/generic/python/mkdb.py --arch Series7 \
        --dies xa7s15,xa7a12t --tag r9,r10 --jobs 8 --cache $X/cache_$v $opt > $X/mkdb_$v.log 2>&1
    echo "$v rc=$? $(grep -h 'sample phase\|samples of' $X/mkdb_$v.log | tr '\n' ' ')"
done
cat $X/cache_bud/worker_peaks.json
for v in bud bud2; do
    diff -r -x .mkdb_tasks $X/db_nob $X/db_$v > $X/diff_$v.txt && echo "$v IDENTICAL" || echo "$v DIFFERENT $(wc -l < $X/diff_$v.txt)"
done
