# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
#
# Bonded I/O pad sites of a part: one site name per line.
#   vivado -mode batch -source dump_bonded.tcl -tclargs <part> <out file>
# Unbonded pads keep their power-on configuration (no pull resistor
# programming for unused pads), so features.py only gives bonded pads the
# pull features.
lassign $argv part out
create_project -in_memory -part $part
link_design -part $part
set f [open $out w]
foreach s [lsort [get_sites -quiet -of_objects [get_package_pins -quiet]]] {
    puts $f $s
}
close $f
