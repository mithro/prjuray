# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
#
# Generic, architecture independent feature dump of a routed design.
#
# Usage (inside a Vivado session with the design open):
#   source dump_features.tcl ; dump_features <out.txt>
#
# Output lines:
#   pip <tile> <tile_type> <wire0> <wire1> <dir>     dir = 1 directional, 0 bidir
#   site <site> <site_type> <tile>
#   sp <site> <bel> <from_pin> <to_pin>             used site pip (routing mux)
#   cfg <site> <bel> <name> <value>                 physical BEL configuration

proc _df_cfg_props {bel} {
    global _df_prop_cache
    set t [get_property TYPE $bel]
    if {![info exists _df_prop_cache($t)]} {
        set props [list]
        foreach p [list_property $bel CONFIG.*] {
            if {[string match *.VALUES $p] || [string match *.DEFAULT $p]} continue
            lappend props $p
        }
        set _df_prop_cache($t) $props
    }
    return $_df_prop_cache($t)
}

# For the bidirectional PIPs of a net, the direction they are used in:
# returns a dict pip -> 1 (uphill to downhill) or 0 (reversed).  Walks the
# net's routing from the driver's node.
proc _df_bidir_dirs {net} {
    set res [dict create]
    set pips [get_pips -quiet -of_objects $net]
    if {[llength [get_pips -quiet -of_objects $net -filter {IS_DIRECTIONAL == 0}]] == 0} {
        return $res
    }
    set adj [dict create]
    foreach p $pips {
        set up [get_nodes -quiet -uphill -of_objects $p]
        set dn [get_nodes -quiet -downhill -of_objects $p]
        if {$up eq "" || $dn eq ""} continue
        dict lappend adj $up [list $p $dn 1]
        if {![get_property IS_DIRECTIONAL $p]} {
            dict lappend adj $dn [list $p $up 0]
        }
    }
    set start [get_nodes -quiet -of_objects [get_site_pins -quiet -of_objects [get_pins -quiet -of_objects $net -filter {DIRECTION == OUT}]]]
    if {$start eq ""} {
        # Constant nets etc.: start from nodes with no incoming pip.
        set incoming [dict create]
        dict for {n lst} $adj { foreach e $lst { dict set incoming [lindex $e 1] 1 } }
        set start [list]
        dict for {n lst} $adj { if {![dict exists $incoming $n]} { lappend start $n } }
    }
    set seen [dict create]
    set queue $start
    foreach n $start { dict set seen $n 1 }
    while {[llength $queue]} {
        set n [lindex $queue 0]
        set queue [lrange $queue 1 end]
        if {![dict exists $adj $n]} continue
        foreach e [dict get $adj $n] {
            lassign $e p m fwd
            if {![get_property IS_DIRECTIONAL $p] && ![dict exists $res $p]} {
                dict set res $p $fwd
            }
            if {![dict exists $seen $m]} {
                dict set seen $m 1
                lappend queue $m
            }
        }
    }
    return $res
}

# Direction of all used bidirectional PIPs (given the used PIP names): a
# dict pip -> 1 (uphill to downhill) or 0 (reversed).  The route tree
# printed by report_route_status names, for every PIP, the node it drives;
# nets it cannot be read from fall back to walking the net (_df_bidir_dirs).
proc _df_bidir_all {pips} {
    set res [dict create]
    set bp [list]
    foreach p $pips {
        if {[string first "<<->>" $p] >= 0} { lappend bp $p }
    }
    if {[llength $bp] == 0} { return $res }
    foreach net [get_nets -quiet -of_objects [get_pips -quiet $bp]] {
        if {[catch {report_route_status -of_objects $net -return_string} rep]} {
            set rep ""
        }
        set found [dict create]
        foreach {- node pip} [regexp -all -inline {(\S+) \(\s*\d+\)\s+(\S+<<->>\S+)} $rep] {
            dict set found $pip $node
        }
        set ok 1
        set part [dict create]
        foreach p [get_pips -quiet -of_objects $net -filter {IS_DIRECTIONAL == 0}] {
            if {![dict exists $found $p]} { set ok 0; break }
            set sl [string first / $p]
            set tile [string range $p 0 [expr {$sl - 1}]]
            set rest [string range $p [expr {[string first . $p $sl] + 1}] end]
            lassign [split [string map {"<<->>" "\t"} $rest] "\t"] w0 w1
            set n0 [get_nodes -quiet -of_objects [get_wires -quiet $tile/$w0]]
            set n1 [get_nodes -quiet -of_objects [get_wires -quiet $tile/$w1]]
            set to [dict get $found $p]
            if {$to eq $n1} {
                dict set part $p 1
            } elseif {$to eq $n0} {
                dict set part $p 0
            } else {
                set ok 0
                break
            }
        }
        if {!$ok} { set part [_df_bidir_dirs $net] }
        set res [dict merge $res $part]
    }
    return $res
}

# get_property over a list of objects, falling back to one query per object
# when values containing spaces (or empty values) break the returned list.
proc _df_props {prop objs} {
    set vals [get_property $prop $objs]
    if {[llength $vals] != [llength $objs]} {
        set vals [list]
        foreach o $objs { lappend vals [get_property $prop $o] }
    }
    return $vals
}

proc struct_diff {a b} {
    set s [dict create]
    foreach x $b { dict set s $x 1 }
    set out [list]
    foreach x $a { if {![dict exists $s $x]} { lappend out $x } }
    return $out
}

proc dump_features {out} {
    set fp [open $out w]
    # Design level bitstream options that change configuration frames.
    foreach p {BITSTREAM.CONFIG.UNUSEDPIN} {
        if {![catch {set v [get_property $p [current_design]]}]} {
            puts $fp "global [lindex [split $p .] end] $v"
        }
    }
    set t0 [clock milliseconds]
    set allpips [lsort -unique [get_pips -quiet -of_objects [get_nets -hierarchical -quiet]]]
    # Direction of used bidirectional PIPs.
    set bidir [_df_bidir_all $allpips]
    puts $fp "# t_bidir [expr {[clock milliseconds] - $t0}]"
    # Routing PIPs, including pseudo pips (LUT route-throughs etc).
    foreach pip $allpips {
        set sl [string first / $pip]
        set tile [string range $pip 0 [expr {$sl - 1}]]
        set name [string range $pip [expr {$sl + 1}] end]
        set dot [string first . $name]
        set tt [string range $name 0 [expr {$dot - 1}]]
        set rest [string range $name [expr {$dot + 1}] end]
        if {[string first "<<->>" $rest] >= 0} {
            set w [split [string map {"<<->>" "\t"} $rest] "\t"]
            set dir 0
            # Used in reverse: name it from the driving side.
            if {[dict exists $bidir $pip]} {
                if {[dict get $bidir $pip] == 0} {
                    set w [list [lindex $w 1] [lindex $w 0]]
                }
                set dir 2
            }
        } else {
            set w [split [string map {"->>" "\t" "->" "\t"} $rest] "\t"]
            set dir 1
        }
        puts $fp "pip $tile $tt [lindex $w 0] [lindex $w 1] $dir"
    }
    # Sites, used site pips and BEL configuration.
    puts $fp "# t_pips [expr {[clock milliseconds] - $t0}]"
    # Used sites, plus sites only used for routing (e.g. LUTs generating
    # constants, route-throughs), found through their connected site pins.
    set sites [get_sites -quiet -filter {IS_USED}]
    set sites [lsort -unique [concat $sites [get_sites -quiet -of_objects [get_site_pins -quiet -of_objects [get_nets -hierarchical -quiet]]]]]
    puts $fp "# t_sitelist [expr {[clock milliseconds] - $t0}]"
    # Vectorised queries (one Tcl call per property over many objects).
    if {[llength $sites]} {
        foreach s $sites st [_df_props SITE_TYPE $sites] {
            puts $fp "site $s $st -"
        }
        set sps [get_site_pips -quiet -of_objects $sites -filter {IS_USED}]
        if {[llength $sps]} {
            foreach sp $sps f [_df_props FROM_PIN $sps] to [_df_props TO_PIN $sps] {
                set n [split $sp /]
                set bel [lindex [split [lindex $n 1] :] 0]
                puts $fp "sp [lindex $n 0] $bel $f $to"
            }
        }
        # A carry input taken from the chain (CIN) is not a site pip: report
        # it as one (7-series PRECYINIT CIN, UltraScale CARRY8 CIN).
        foreach c [get_cells -quiet -hierarchical -filter {REF_NAME == CARRY4 || REF_NAME == CARRY8}] {
            set ci [get_pins -quiet $c/CI]
            if {$ci eq ""} continue
            set drv [get_pins -quiet -leaf -of_objects [get_nets -quiet -of_objects $ci] -filter {DIRECTION == OUT}]
            if {[llength $drv] != 1 || [get_property REF_NAME [get_cells -of_objects $drv]] ni {CARRY4 CARRY8}} continue
            set site [get_property SITE [get_cells $c]]
            if {$site eq ""} continue
            if {[get_property REF_NAME [get_cells $c]] eq "CARRY4"} {
                puts $fp "sp $site PRECYINIT CIN OUT"
            } else {
                puts $fp "sp $site CARRY8 CIN CI"
            }
        }
        set used [get_sites -quiet -filter {IS_USED}]
        set bels [get_bels -quiet -of_objects $used -filter {IS_USED}]
        # Routing-only sites: all their BELs (unconfigured ones are skipped).
        set ronly [struct_diff $sites $used]
        if {[llength $ronly]} {
            set bels [concat $bels [get_bels -quiet -of_objects $ronly]]
        }
        set bytype [dict create]
        foreach b $bels t [_df_props TYPE $bels] { dict lappend bytype $t $b }
        dict for {t bl} $bytype {
            foreach p [_df_cfg_props [lindex $bl 0]] {
                set name [string range $p 7 end]
                foreach b $bl v [_df_props $p $bl] {
                    if {$v eq "NOT CONFIGURED" || $v eq ""} continue
                    set n [split $b /]
                    puts $fp "cfg [lindex $n 0] [lindex $n end] $name [string map {" " "_"} $v]"
                }
            }
        }
    }
    puts $fp "# t_done [expr {[clock milliseconds] - $t0}]"
    close $fp
}
