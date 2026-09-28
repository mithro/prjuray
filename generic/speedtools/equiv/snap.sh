#!/bin/bash
# snap.sh <arch>: snapshot the production DB (no caches) into $S/dbsnap/<arch>
. "$(dirname "$0")/common.sh"
mkdir -p $S/dbsnap
rsync -a --delete --exclude cache --exclude 'counts_*' --exclude 'unexplained_*' \
    $PROD/db/$1/ $S/dbsnap/$1/
du -sh $S/dbsnap/$1
