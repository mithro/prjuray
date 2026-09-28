#!/bin/bash
# assoc.sh <park 0|1> <bit> [exclude regex] [max designs]
cd /tmp/claude-1000/sp_speed/speed/park
export URAY_BUILD=/home/tim/github/f4pga/prjuray/build/ci/work/agent-ae3b9b22dac6da105/xcku025/build
export URAY_DB=$URAY_BUILD/db
export URAY_PARK=$1
nice -n 19 ionice -c2 -n7 python3 bitassoc.py xcku025 r8,r9 INT "$2" "${3:-^NOMATCH$}" "${4:-40}"
