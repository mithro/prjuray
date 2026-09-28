# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
#
# Bitstream option fuzzing: one small routed design, many bitstreams written
# with random BITSTREAM.* option values.  Used to document the configuration
# registers (and any frame bits the options change).
#   vivado -mode batch -source regfuzz.tcl -tclargs <part> <count> <seed>
# Writes r<N>.bit and r<N>.opts ("<property> <value>" lines, all options).
set part [lindex $argv 0]
set count [lindex $argv 1]
expr {srand([lindex $argv 2])}
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
foreach d [get_drc_checks] { catch {set_property SEVERITY Warning $d} }

# Options and their legal values.
set design [current_design]
set opts [dict create]
foreach p [list_property $design BITSTREAM.*] {
    if {[catch {set vals [list_property_value $p $design]}]} continue
    if {[llength $vals] < 2} continue
    # Options that change the file format rather than the configuration.
    if {[regexp {EXTMASTERCCLK_EN|ENCRYPT|KEY|HKEY|AUTHENTICATION|RSA|STARTKEY|COMPRESS|READBACK|DEBUGBITSTREAM|PERFRAMECRC|BITSTREAM.GENERAL.PERSIST|SEU|RS_PINS|ENCRYPTKEYSELECT|EFUSE|OBFUSCATE} $p]} continue
    dict set opts $p $vals
}
set fp [open options.txt w]
dict for {p vals} $opts { puts $fp "$p [join $vals |]" }
close $fp

set defaults [dict create]
dict for {p vals} $opts { dict set defaults $p [get_property $p $design] }

for {set i 0} {$i < $count} {incr i} {
    # Start from the defaults, then randomise a random subset.
    dict for {p v} $defaults { catch {set_property $p $v $design} }
    dict for {p vals} $opts {
        if {rand() < 0.4} {
            set v [lindex $vals [expr {int(rand() * [llength $vals])}]]
            catch {set_property $p $v $design}
        }
    }
    # Combinations bitgen rejects.
    if {[dict exists $opts BITSTREAM.STARTUP.STARTUPCLK] && [get_property BITSTREAM.STARTUP.STARTUPCLK $design] eq "USERCLK"} {
        set_property BITSTREAM.STARTUP.STARTUPCLK CCLK $design
    }
    if {[dict exists $opts BITSTREAM.CONFIG.SPI_BUSWIDTH] && [get_property BITSTREAM.CONFIG.SPI_BUSWIDTH $design] eq "NONE"} {
        catch {reset_property BITSTREAM.CONFIG.SPI_32BIT_ADDR $design}
    }
    set fp [open r$i.opts w]
    dict for {p vals} $opts { puts $fp "$p [get_property $p $design]" }
    close $fp
    if {[catch {write_bitstream -force r$i.bit} e]} {
        file delete -force r$i.opts
    }
}
