#!/bin/bash
# Check several dies against the current bit database, a few in parallel.
#
#   generic/check_dies.sh <tag> <parallel dies> <jobs per die> <die>...
#   generic/vrun.sh check-med 30G generic/check_dies.sh r8 3 4 xa7s50 xa7z020
#
# Each die's report goes to build/logs/check_<die>.log.
set -u
if [ $# -lt 4 ]; then
    echo "usage: $0 <tag> <parallel dies> <jobs per die> <die>..." >&2
    exit 2
fi
tag=$1
par=$2
jobs=$3
shift 3
cd "$(dirname "$0")/.."
build=${URAY_BUILD:-build}
printf '%s\n' "$@" | xargs -P "$par" -I{} sh -c \
    "python3 -u generic/python/check.py --die {} --designs $build/designs/{}/$tag --max 20 --jobs $jobs > $build/logs/check_{}.log 2>&1; echo \"{} rc=\$?\""
