# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
#
# List every installed part.
#   vivado -mode batch -source dump_parts.tcl -tclargs <out.txt>
# Output lines:  <part> <family> <architecture> <device> <package> <speed> <license|->
# (LICENSE is Webpack or Full on newer families, empty = no licence needed)
set fp [open [lindex $argv 0] w]
foreach p [get_parts] {
    puts $fp [join [list $p [get_property FAMILY $p] \
        [get_property ARCHITECTURE $p] [get_property DEVICE $p] \
        [get_property PACKAGE $p] [get_property SPEED $p] \
        [expr {[get_property LICENSE $p] eq "" ? "-" : [get_property LICENSE $p]}]] " "]
}
close $fp
