# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
#
# Dump configurable properties of every library primitive for a part.
#   vivado -mode batch -source dump_prims.tcl -tclargs <part> <out.txt>
# Output lines:
#   prim <name> <pins: dir:name ...>
#   prop <prim> <name> <type> {<default>} {<legal values>}
set part [lindex $argv 0]
set out [lindex $argv 1]
create_project -in_memory -part $part
link_design -part $part
set fp [open $out w]
set skip {NAME CLASS REF_NAME ORIG_REF_NAME PARENT PRIMITIVE_COUNT PRIMITIVE_GROUP PRIMITIVE_LEVEL PRIMITIVE_SUBGROUP PRIMITIVE_TYPE REF_LIB_NAME IS_BLACKBOX IS_DEBUGGABLE IS_MATCHED IS_ORIG_CELL IS_PRIMITIVE IS_REUSED IS_SEQUENTIAL LINE_NUMBER FILE_NAME STATUS IS_BEL_FIXED IS_LOC_FIXED IS_MACRO_MEMBER BEL LOC IS_MACRO}
set i 0
foreach lc [get_lib_cells] {
    set name [get_property NAME $lc]
    if {[catch {create_cell -reference $name c$i} e]} { puts $fp "noprim $name"; continue }
    set c [get_cells c$i]
    set pins [list]
    foreach p [get_pins -quiet -of_objects $c] {
        lappend pins "[get_property DIRECTION $p]:[get_property REF_PIN_NAME $p]"
    }
    puts $fp "prim $name [get_property PRIMITIVE_GROUP $c] [get_property PRIMITIVE_SUBGROUP $c] [join $pins { }]"
    foreach p [list_property $c] {
        if {[lsearch -exact $skip $p] >= 0} continue
        if {[catch {set v [get_property $p $c]}]} continue
        set vals ""
        catch {set vals [list_property_value $p $c]}
        puts $fp "prop $name $p {$v} {$vals}"
    }
    incr i
}
close $fp
