# Copyright 2020-2026 F4PGA Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# SPDX-License-Identifier: Apache-2.0
#
# Dump the tile grid of a part.
#   vivado -mode batch -source dump_tiles.tcl -tclargs <part> <out.tsv>
# Output lines:  tile <name> <type> <grid_x> <grid_y> <clock_region|-> <site:type,...|-> <int_x> <int_y>

set part [lindex $argv 0]
set out [lindex $argv 1]
create_project -in_memory -part $part
link_design -part $part
set fp [open $out w]
puts $fp "part $part [get_property DEVICE [get_parts $part]] [get_property ARCHITECTURE [get_parts $part]]"
foreach tile [get_tiles] {
    set sites [get_sites -quiet -of_objects $tile]
    set cr "-"
    set sl [list]
    foreach s $sites {
        lappend sl "$s:[get_property SITE_TYPE $s]"
        if {$cr eq "-"} {
            set c [get_property CLOCK_REGION $s]
            if {$c ne ""} { set cr $c }
        }
    }
    if {[llength $sl] == 0} { set sl "-" } else { set sl [join $sl ","] }
    puts $fp "tile $tile [get_property TYPE $tile] [get_property GRID_POINT_X $tile] [get_property GRID_POINT_Y $tile] $cr $sl [get_property INT_TILE_X $tile] [get_property INT_TILE_Y $tile]"
}
foreach cr [get_clock_regions] {
    puts $fp "cr $cr [get_property ROW_INDEX $cr] [get_property COLUMN_INDEX $cr] [get_property TOP_LEFT_TILE $cr] [get_property BOTTOM_RIGHT_TILE $cr]"
}
close $fp
