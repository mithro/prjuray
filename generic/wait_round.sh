#!/bin/bash
# Wait until a run_designs.py round is usable (all designs started, at most
# --usable-at still running) or done, then print its marker (JSON).
#
#   generic/wait_round.sh <tag> [usable|done] [workdir]
#
# "usable" also returns when the round is done.  Chained steps (e.g.
# pipeline.py) can start on the finished designs at "usable"; the
# stragglers are picked up by the next rebuild (mkdb reuses the unchanged
# tasks).
set -u
tag=$1; kind=${2:-usable}
G=$(cd "$(dirname "$0")" && pwd)
wd=${3:-${URAY_BUILD:-$(cd "$G/.." && pwd)/build}/designs}
m=$wd/.rounds/$tag
while :; do
    if [ -f "$m.done" ]; then cat "$m.done"; echo; exit 0; fi
    if [ "$kind" = usable ] && [ -f "$m.usable" ]; then cat "$m.usable"; echo; exit 0; fi
    sleep 20
done
