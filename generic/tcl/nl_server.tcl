# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
#
# Runs several generated fuzzing designs in one Vivado process, saving the
# Vivado start-up and most of the device load time of each design.
#
# Reads one request per line from stdin:
#   <design directory> <NL_BUDGET seconds>
# runs <design directory>/design.tcl there (as "vivado -source design.tcl"
# would) and answers on stdout with
#   NLDONE <design directory> <0 ok | 1 Tcl error>
# The process must be started with NL_VIVADO_LOG set to its -log file (the
# repair loop of netlist.tcl reads the errors back from it).

fconfigure stdout -buffering line
set nl_server_home [pwd]
puts "NLREADY"
while {[gets stdin req] >= 0} {
    if {[llength $req] < 1} continue
    lassign $req dir budget
    if {$budget ne ""} { set ::env(NL_BUDGET) $budget }
    puts "NLSTART $dir"
    set rc 0
    if {[catch {
        cd $dir
        uplevel #0 [list source design.tcl]
    } err]} {
        set rc 1
        puts "NLERROR $err"
    }
    # Close the design and the per design log (outside the design
    # directory: closing may leave scratch files in the current directory).
    global nl_logfp
    if {[info exists nl_logfp]} {
        catch {close $nl_logfp}
        unset nl_logfp
    }
    cd $nl_server_home
    catch {close_project}
    puts "NLDONE $dir $rc"
}
