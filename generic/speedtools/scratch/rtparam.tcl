# -tclargs <dcp> <param=value>...: route_design time on a checkpoint with
# hidden router params set.
set dcp [lindex $argv 0]
foreach pv [lrange $argv 1 end] {
    lassign [split $pv =] p v
    if {[catch {set_param $p $v} e]} { puts "RTP set_param $p failed: $e" } else { puts "RTP $p = [get_param $p]" }
}
proc T {label script} {
    set t [clock milliseconds]
    set rc [catch {uplevel 1 $script} e]
    puts "RTP $label [expr {[clock milliseconds]-$t}] ms rc $rc [string range $e 0 200]"
}
T open {open_checkpoint $dcp}
T unroute {route_design -unroute}
T route {route_design}
puts "RTP status [llength [get_nets -quiet -hierarchical -filter {ROUTE_STATUS == ROUTED}]] routed, [llength [get_nets -quiet -hierarchical -filter {ROUTE_STATUS == CONFLICTS}]] conflicts"
