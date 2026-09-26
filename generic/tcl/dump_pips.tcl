# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
#
# Dump every PIP of every tile type (one example tile per type).
#   vivado -mode batch -source dump_pips.tcl -tclargs <part> <out.txt>
# Output lines: pip <tile type> <wire0> <wire1> <directional> <pseudo> <example pip>
set part [lindex $argv 0]
set out [lindex $argv 1]
create_project -in_memory -part $part
link_design -part $part
set fp [open $out w]
set seen [dict create]
foreach tile [get_tiles] {
    set tt [get_property TYPE $tile]
    if {[dict exists $seen $tt]} continue
    dict set seen $tt 1
    foreach pip [get_pips -quiet -of_objects $tile] {
        set name [lindex [split $pip /] 1]
        set rest [string range $name [expr {[string first . $name] + 1}] end]
        if {[string first "<<->>" $rest] >= 0} {
            set w [split [string map {"<<->>" "\t"} $rest] "\t"]
        } else {
            set w [split [string map {"->>" "\t" "->" "\t"} $rest] "\t"]
        }
        puts $fp "pip $tt [lindex $w 0] [lindex $w 1] [get_property IS_DIRECTIONAL $pip] [get_property IS_PSEUDO $pip] $pip"
    }
}
close $fp
