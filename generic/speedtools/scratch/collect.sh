#!/bin/bash
# collect.sh: copy the scratch scripts worth keeping into speedtools/scratch
D=/home/tim/github/f4pga/prjuray/.claude/worktrees/agent-ae3b9b22dac6da105/generic/speedtools/scratch
S=/tmp/claude-1000/sp_speed/speed
mkdir -p $D
cp $S/park/ci.sh $D/park_ci.sh
for f in cid.sh one.sh ab.sh abc.sh mk.sh gty.sh gty2.sh s7pj.sh pj.sh twinci.sh twinci2.sh sbdiff.py assoc.sh bitassoc.py cimon.sh abmon.sh twmon.sh; do
  cp $S/park/$f $D/
done
cp $S/cov/dbg.sh $D/pips_dbg.sh
for f in rtreuse.tcl rtreuse.sh rtparam.tcl rtparam.sh sigint.tcl sigint.sh rtbuild.tcl rdmon.sh nltail.sh rh.sh s15reuse.sh psmon.sh pstop.sh bufgcnt.py r12sum.py; do
  cp $S/$f $D/
done
ls $D | wc -l
grep -l "2>/dev/null" $D/*
echo checked
