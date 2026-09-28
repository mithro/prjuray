# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
#
# Minimal design (one FF + LUT ring) for a part: writes baseline.bit,
# baseline_crc.bit (per-frame CRC, carries explicit FAR writes) and design.dcp.
#   vivado -mode batch -source baseline.tcl -tclargs <part>
set part [lindex $argv 0]
create_project -in_memory -part $part
link_design -part $part
create_cell -reference FDRE f0
create_cell -reference LUT1 l0
set_property INIT 2'h1 [get_cells l0]
create_cell -reference GND g0
create_cell -reference VCC v0
create_net gn; create_net vn; create_net q0; create_net d0
connect_net -net gn -objects [list [get_pins g0/G] [get_pins f0/C] [get_pins f0/R]]
connect_net -net vn -objects [list [get_pins v0/P] [get_pins f0/CE]]
connect_net -net q0 -objects [list [get_pins f0/Q] [get_pins l0/I0]]
connect_net -net d0 -objects [list [get_pins l0/O] [get_pins f0/D]]
place_design
route_design
write_checkpoint -force design.dcp
write_bitstream -force baseline.bit
set_property BITSTREAM.GENERAL.PERFRAMECRC YES [current_design]
write_bitstream -force baseline_crc.bit
