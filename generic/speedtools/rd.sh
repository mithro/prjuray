#!/bin/bash
# rd.sh <logname> <run_designs args...>  (memory capped via vrun, VMEM default 6G)
log=${SP:-/tmp/speedtools}/$1.log
shift
W=$(cd $(dirname $0)/../.. && pwd)
cd $W/generic/python || exit 1
export URAY_BUILD=${URAY_BUILD:-$W/build}
$W/generic/vrun.sh sp-rd ${VMEM:-6G} python3 run_designs.py "$@" > "$log" 2>&1
echo "EXIT $?" >> "$log"
