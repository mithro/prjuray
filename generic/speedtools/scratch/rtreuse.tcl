# rtreuse.tcl: does route_design's "Build RT Design" survive when the next
# design is built inside the same in-memory design (no close_project)?
# env: RTR_W worktree, RTR_A / RTR_B design dirs (source), RTR_OUT scratch.
# RTR_MODE: close (as nl_server does now) | keep (clear cells, keep design)
source $::env(RTR_W)/generic/tcl/netlist.tcl
rename nl_init nl_init_orig
proc nl_init {part} {
    if {$::env(RTR_MODE) eq "keep" && [llength [get_designs -quiet]]} {
        global nl_logfp nl_vivado_log nl_logpos
        set nl_logfp [open nl.log w]
        nl_log "t_start [clock milliseconds]"
        foreach v {nl_props nl_was_reset nl_bank_std} {
            global $v
            unset -nocomplain $v
        }
        if {[file exists $nl_vivado_log]} { set nl_logpos [file size $nl_vivado_log] }
        set t [clock milliseconds]
        catch {route_design -unroute}
        catch {place_design -unplace}
        catch {delete_pblocks [get_pblocks -quiet]}
        set c [get_cells -quiet]
        if {[llength $c]} { remove_cell $c }
        set n [get_nets -quiet]
        if {[llength $n]} { catch {remove_net $n} }
        set p [get_ports -quiet]
        if {[llength $p]} { remove_port $p }
        if {[info exists ::env(RTR_PARAM)]} {
            foreach pv [split $::env(RTR_PARAM) ,] {
                lassign [split $pv =] k v
                puts "RTR set_param $k $v: [catch {set_param $k $v} e] $e"
            }
        }
        puts "RTR cleared in [expr {[clock milliseconds]-$t}] ms: [llength [get_cells -quiet]] cells left"
        nl_log "t_linked [clock milliseconds]"
        return
    }
    nl_init_orig $part
}
set ::env(NL_KEEP_LOGS) 1
foreach {tag src} [list a $::env(RTR_A) b $::env(RTR_B)] {
    set d $::env(RTR_OUT)/$tag
    file mkdir $d
    set f [open $src/design.tcl]; set txt [read $f]; close $f
    regsub {^source [^\n]*\n} $txt {} txt
    set f [open $d/design.tcl w]; puts -nonewline $f $txt; close $f
    cd $d
    set t [clock milliseconds]
    set rc [catch {uplevel #0 [list source design.tcl]} e]
    puts "RTR design $tag [expr {[clock milliseconds]-$t}] ms rc $rc [string range $e 0 200]"
    catch {close $::nl_logfp}
    if {$::env(RTR_MODE) eq "close"} { catch {close_project} }
}
