# -tclargs <dcp>: does the router's "Build RT Design" repeat within a process?
set dcp [lindex $argv 0]
proc T {label script} {
    set t [clock milliseconds]
    uplevel 1 $script
    puts "RTB $label [expr {[clock milliseconds]-$t}]"
}
T open1 {open_checkpoint $dcp}
T unroute1 {route_design -unroute}
T route1 {route_design}
T unroute2 {route_design -unroute}
T route2 {route_design}
T close {close_project}
T open2 {open_checkpoint $dcp}
T unroute3 {route_design -unroute}
T route3 {route_design}
