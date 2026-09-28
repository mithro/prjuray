# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
# Resource counts of every part:  part lut ff dsp bram gt io
set fp [open [lindex $argv 0] w]
foreach p [get_parts] {
    set r [list $p]
    foreach k {LUT_ELEMENTS FLIPFLOPS DSP BLOCK_RAMS GB_TRANSCEIVERS IO_PIN_COUNT ULTRA_RAMS} {
        set v [get_property -quiet $k $p]
        if {$v eq ""} { set v 0 }
        lappend r $v
    }
    puts $fp [join $r " "]
}
close $fp
