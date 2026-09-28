#!/bin/bash
# budget_eq.sh: mkdb with and without --sample-mem-budget on a scratch
# build (xa7s15+xa7a12t r9,r10), fresh caches: diff -r of the DBs.
. "$(dirname "$0")/common.sh"
X=$S/budget
python3 $W/generic/speedtools/mksbuild.py $X/build Series7 xa7s15,xa7a12t r9,r10 $PROD
export URAY_BUILD=$X/build PYTHONHASHSEED=0
for v in nob bud; do
    rm -rf $X/db_$v $X/cache_$v; mkdir -p $X/db_$v/Series7
    for d in xa7s15 xa7a12t; do mkdir -p $X/db_$v/Series7/$d; cp -p $X/build/db/Series7/$d/tilegrid.json $X/db_$v/Series7/$d/; done
    opt=; [ $v = bud ] && opt="--sample-jobs 16 --sample-mem-budget 9"
    URAY_DB=$X/db_$v $W/generic/vrun.sh tp-bud 12G python3 $W/generic/python/mkdb.py --arch Series7 \
        --dies xa7s15,xa7a12t --tag r9,r10 --jobs 8 --cache $X/cache_$v $opt > $X/mkdb_$v.log 2>&1
    echo "$v rc=$? $(grep -h 'sample phase\|samples of' $X/mkdb_$v.log | tr '\n' ' ')"
done
diff -r -x .mkdb_tasks $X/db_nob $X/db_bud > $X/diff_bud.txt && echo "budget run IDENTICAL" || echo "DIFFERENT $(wc -l < $X/diff_bud.txt)"
