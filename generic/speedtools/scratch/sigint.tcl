# -tclargs <dcp>: can route_design be interrupted with SIGINT and the script go on?
open_checkpoint [lindex $argv 0]
route_design -unroute
set f [open ready.txt w]; puts $f [pid]; close $f
set t [clock seconds]
set rc [catch {route_design} e]
puts "SIG route rc $rc after [expr {[clock seconds]-$t}] s: [string range $e 0 300]"
puts "SIG status [llength [get_nets -quiet -hierarchical -filter {ROUTE_STATUS == ROUTED}]] routed nets"
set rc [catch {route_design} e]
puts "SIG second route rc $rc after [expr {[clock seconds]-$t}] s"
