#!/bin/bash
# rtparam.sh <name> <param=value>... : run rtparam.tcl on the xcu25 dcp in prof/rtp_<name>
S=/tmp/claude-1000/sp_speed/speed
name=$1
shift
D=$S/prof/rtp_$name
mkdir -p $D
ln -sf $S/prof/cu25_s1/design.dcp $D/design.dcp
VMEM=6G bash $S/viv.sh $D rtp.log -source $S/rtparam.tcl -tclargs design.dcp "$@"
echo "rc $?" > $D/done
