#!/bin/bash
# sigint.sh: run sigint.tcl on the xcu25 checkpoint, SIGINT the Vivado binary 60 s into routing
S=/tmp/claude-1000/sp_speed/speed
D=$S/prof/cu25_s1
rm -f $D/ready.txt
VMEM=10G bash $S/viv.sh $D sig.log -source $S/sigint.tcl -tclargs design.dcp &
until [ -f $D/ready.txt ]; do sleep 2; done
sleep 60
p=$(cat $D/ready.txt)
echo "sending SIGINT to $p" > $D/sig_send.log
kill -INT $p >> $D/sig_send.log 2>&1
wait
grep "^SIG\|Common 17-344\|cancel\|nterrupt" $D/sig.log | head
