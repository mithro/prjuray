#!/bin/bash
# Run a memory-heavy command (Vivado, run_designs.py, pipeline.py, mkdb,
# check.py --jobs N, ...) in its own transient systemd scope inside the
# shared vivado.slice, with a hard memory cap.  Under memory pressure only
# this scope is OOM-killed, never the calling shell / tmux pane.
#
#   generic/vrun.sh <name> <MemoryMax> <command...>
#   generic/vrun.sh r8-s7small 60G python3 generic/python/run_designs.py ...
#
# On exit prints "vrun: <unit> peak <bytes> (<GiB>) status <rc>".  When the cap
# is hit systemd stops the whole scope (exit 143, journal says oom-kill, no
# peak line).  Treat that as a retryable failure: lower --jobs and rerun,
# don't just raise the cap.
set -u
if [ $# -lt 3 ]; then
    echo "usage: $0 <name> <MemoryMax> <command...>" >&2
    exit 2
fi
name=$1
cap=$2
shift 2
unit="prjuray-${name}-$(date +%s)"
exec systemd-run --user --scope --quiet --slice=vivado.slice --unit="$unit" \
    -p MemoryMax="$cap" -p MemorySwapMax=0 -- \
    bash -c '
        "$@"
        rc=$?
        cg=/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)
        peak=$(cat "$cg/memory.peak")
        echo "vrun: '"$unit"' peak $peak ($((peak >> 30)) GiB) status $rc"
        exit $rc
    ' vrun "$@"
