# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
#
# Dump tile-type and site-type information for a part.
#   vivado -mode batch -source dump_types.tcl -tclargs <part> <out.txt>
# Output lines:
#   tiletype <type> <count> <example> <npips> <nsites> <wires>
#   sitetype <type> <example> <nbels> <nsitepips>
#   bel <sitetype> <bel> <beltype> <is_routing>
#   cfg <sitetype> <bel> <cfgname> <default> <values...>

set part [lindex $argv 0]
set out [lindex $argv 1]
create_project -in_memory -part $part
link_design -part $part
set fp [open $out w]
set tiles_by_type [dict create]
foreach tile [get_tiles] {
    dict lappend tiles_by_type [get_property TYPE $tile] $tile
}
set site_examples [dict create]
dict for {tt tiles} $tiles_by_type {
    set ex [lindex $tiles 0]
    set sites [get_sites -quiet -of_objects $ex]
    puts $fp "tiletype $tt [llength $tiles] $ex [get_property NUM_ARCS $ex] [llength $sites] [llength [get_wires -quiet -of_objects $ex]]"
    foreach t $tiles {
        foreach s [get_sites -quiet -of_objects $t] {
            foreach st [concat [list [get_property SITE_TYPE $s]] [get_property ALTERNATE_SITE_TYPES $s]] {
                if {![dict exists $site_examples $st]} { dict set site_examples $st $s }
            }
        }
        # only scan a few instances for site variety
        if {[incr scanned($tt)] > 8} break
    }
}
dict for {st s} $site_examples {
    # Alternate site types need the site to be switched to them.
    if {[get_property SITE_TYPE $s] ne $st} {
        if {[catch {set_property MANUAL_ROUTING $st $s} e]} {
            puts $fp "sitetype $st $s -1 -1 alt"
            continue
        }
    }
    set bels [get_bels -quiet -include_routing_bels -of_objects $s]
    puts $fp "sitetype $st $s [llength $bels] [llength [get_site_pips -quiet -of_objects $s]]"
    foreach b $bels {
        set bn [lindex [split $b /] end]
        puts $fp "bel $st $bn [get_property TYPE $b] [get_property IS_ROUTING $b]"
        foreach p [list_property $b CONFIG.*] {
            if {[string match *.VALUES $p] || [string match *.DEFAULT $p]} continue
            set name [string range $p 7 end]
            set def [get_property $p.DEFAULT $b]
            set vals [get_property $p.VALUES $b]
            puts $fp "cfg $st $bn $name {$def} {$vals}"
        }
    }
    catch {reset_property MANUAL_ROUTING $s}
}
close $fp
