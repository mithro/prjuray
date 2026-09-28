# -tclargs <dump tcl> <dcp> <maxThreads> <out>
source [lindex $argv 0]
if {[catch {open_checkpoint [lindex $argv 1]} e]} { puts "OPENERR $e" }
set_param general.maxThreads [lindex $argv 2]
set t [clock milliseconds]
dump_features [lindex $argv 3]
puts "PROF total [expr {[clock milliseconds]-$t}]"
