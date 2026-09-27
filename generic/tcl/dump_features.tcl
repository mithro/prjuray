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
#                                                    (also IS_<pin>_INVERTED of
#                                                    placed cells, on the site
#                                                    pin the cell pin reaches)

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

# The nets whose routing is dumped: the signal nets plus one ground and one
# power net.  All constant nets (thousands where Vivado inserted constants)
# share the routing of the global ground/power net, so querying all of them
# returns that routing once per net (up to hundreds of millions of PIPs:
# "max size for a Tcl value exceeded").
proc _df_route_nets {} {
    set names [get_property NAME [get_nets -hierarchical -quiet -filter {TYPE != GROUND && TYPE != POWER}]]
    foreach t {GROUND POWER} {
        set ns [get_nets -hierarchical -quiet -filter "TYPE == $t"]
        if {[llength $ns]} { lappend names [get_property NAME [lindex $ns 0]] }
    }
    # A collection again (commands taking -of_objects reject plain names).
    return [get_nets -quiet $names]
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
    if {[llength $objs] == 0} { return [list] }
    set vals [get_property $prop $objs]
    if {[llength $vals] != [llength $objs]} {
        set vals [list]
        foreach o $objs { lappend vals [get_property $prop $o] }
    }
    return $vals
}

# Used site pips of the given sites, as a list of {site bel from_pin to_pin}.
# IS_USED costs ~0.3 ms per site pip and a slice has 140-300 of them, so in
# slices only the site pips named by the site's SITE_PIPS property are
# queried (one call per slice).  SITE_PIPS is only filled in for manually
# routed sites, so the slices are made manually routed and back.  The site
# pips SITE_PIPS gets wrong or leaves out, or which the manual routing
# round trip changes (inverters, output muxes: 7-series *OUTMUX:O6,
# UltraScale+ OUTMUX?:D6, and 7-series xUSED:0), are queried for all
# slices beforehand (_df_sp_pre).  The
# design's site routing is changed: call last.  NL_SP_CHECK compares this
# with the exact query.  (Collections are only handled by Vivado commands:
# iterating over them in Tcl converts every object to text, which is slow.)
set _df_sp_pre {*OUTMUX*:* *INV:* *USED:*}
proc _df_used_site_pips {sites} {
    global _df_sp_pre
    set res [list]
    set so [get_sites -quiet $sites]
    set slices [filter -quiet $so {SITE_TYPE =~ SLICE*}]
    set others [filter -quiet $so {SITE_TYPE !~ SLICE*}]
    set groups [list]
    if {[llength $others]} {
        lappend groups [get_site_pips -quiet -of_objects $others -filter {IS_USED}]
    }
    set ok 1
    if {[llength $slices]} {
        set pre [get_site_pips -quiet -of_objects $slices $_df_sp_pre]
        if {[llength $pre]} { lappend groups [filter -quiet $pre {IS_USED}] }
        foreach t [lsort -unique [_df_props SITE_TYPE $slices]] {
            if {[catch {set_property MANUAL_ROUTING $t [filter $slices "SITE_TYPE == $t"]}]} {
                set ok 0
            }
        }
        if {$ok} {
            set names [_df_props NAME $slices]
            set types [_df_props SITE_TYPE $slices]
            set named [_df_props SITE_PIPS $slices]
        }
        catch {reset_property MANUAL_ROUTING $slices}
        if {!$ok} {
            set f [join [lmap p $_df_sp_pre { set p "NAME !~ \"$p\"" }] " && "]
            lappend groups [get_site_pips -quiet -of_objects $slices -filter "IS_USED && $f"]
        }
    }
    foreach g $groups {
        foreach n [_df_props NAME $g] f [_df_props FROM_PIN $g] to [_df_props TO_PIN $g] {
            set n [split $n /]
            lappend res [list [lindex $n 0] [lindex [split [lindex $n 1] :] 0] $f $to]
        }
    }
    if {!$ok || [llength $slices] == 0} { return $res }
    # One query per slice for the site pips SITE_PIPS names.  The output pin
    # of each routing BEL is remembered; the input pin is in the name.
    global _df_to_pin
    foreach s $names t $types v $named {
        set pats [list]
        foreach x $v {
            set done 0
            foreach p $_df_sp_pre {
                if {[string match $p $x]} { set done 1 }
            }
            if {!$done} { lappend pats $s/$x }
        }
        if {[llength $pats] == 0} continue
        # (-filter would test every site pip of the site, not just these.)
        set g [get_site_pips -quiet -of_objects [get_sites $s] $pats]
        if {[llength $g] == 0} continue
        set g [filter -quiet $g {IS_USED}]
        if {[llength $g] == 0} continue
        set gn [_df_props NAME $g]
        set need 0
        foreach n $gn {
            set bel [lindex [split [lindex [split $n /] 1] :] 0]
            if {![info exists _df_to_pin($t/$bel)]} { set need 1 }
        }
        if {$need} {
            foreach n $gn to [_df_props TO_PIN $g] {
                set bel [lindex [split [lindex [split $n /] 1] :] 0]
                set _df_to_pin($t/$bel) $to
            }
        }
        foreach n $gn {
            lassign [split [lindex [split $n /] 1] :] bel pin
            lappend res [list $s $bel $pin $_df_to_pin($t/$bel)]
        }
    }
    return $res
}

# Elements only in a and only in b.
proc _df_list_diff {a b} {
    set da [dict create]
    set db [dict create]
    foreach x $a { dict set da $x 1 }
    foreach x $b { dict set db $x 1 }
    set oa [list]
    set ob [list]
    dict for {x -} $da { if {![dict exists $db $x]} { lappend oa $x } }
    dict for {x -} $db { if {![dict exists $da $x]} { lappend ob $x } }
    return [list $oa $ob]
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
    set allpips [lsort -unique [get_pips -quiet -of_objects [_df_route_nets]]]
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
    set sites [lsort -unique [concat $sites [get_sites -quiet -of_objects [get_site_pins -quiet -of_objects [_df_route_nets]]]]]
    puts $fp "# t_sitelist [expr {[clock milliseconds] - $t0}]"
    # Vectorised queries (one Tcl call per property over many objects).
    if {[llength $sites]} {
        foreach s $sites st [_df_props SITE_TYPE $sites] {
            puts $fp "site $s $st -"
        }
        # The BELs are kept as Vivado collections throughout: get_property
        # on a plain list of BEL names looks every name up (several times
        # slower).  BELs without configuration (NUM_CONFIGS == 0, cheap to
        # test) are dropped before the costly IS_USED test.
        set used [get_sites -quiet -filter {IS_USED}]
        set groups [list [get_bels -quiet -of_objects $used -filter {NUM_CONFIGS > 0 && IS_USED}]]
        # Routing-only sites: all their BELs (unconfigured ones are skipped).
        set ronly [struct_diff $sites $used]
        if {[llength $ronly]} {
            lappend groups [get_bels -quiet -of_objects [get_sites -quiet $ronly] -filter {NUM_CONFIGS > 0}]
        }
        foreach bels $groups {
            if {[llength $bels] == 0} continue
            foreach t [lsort -unique [_df_props TYPE $bels]] {
                set bl [filter -quiet $bels "TYPE == $t"]
                set names [_df_props NAME $bl]
                foreach p [_df_cfg_props [lindex $bl 0]] {
                    set name [string range $p 7 end]
                    foreach b $names v [_df_props $p $bl] {
                        if {$v eq "NOT CONFIGURED" || $v eq ""} continue
                        set n [split $b /]
                        puts $fp "cfg [lindex $n 0] [lindex $n end] $name [string map {" " "_"} $v]"
                    }
                }
            }
        }
        puts $fp "# t_cfg [expr {[clock milliseconds] - $t0}]"
        # Used site pips last: the fast query changes the site routing of
        # the design (see _df_used_site_pips).
        if {[info exists ::env(NL_SP_EXACT)] || [info exists ::env(NL_SP_CHECK)]} {
            set sps [get_site_pips -quiet -of_objects $sites -filter {IS_USED}]
            set used_sps [list]
            foreach n [_df_props NAME $sps] f [_df_props FROM_PIN $sps] to [_df_props TO_PIN $sps] {
                set n [split $n /]
                lappend used_sps [list [lindex $n 0] [lindex [split [lindex $n 1] :] 0] $f $to]
            }
            if {[info exists ::env(NL_SP_CHECK)]} {
                # Self check of the fast query (after the exact one: the
                # fast one changes the design); the exact result is dumped.
                puts $fp "# t_sp_exact [expr {[clock milliseconds] - $t0}]"
                lassign [_df_list_diff $used_sps [_df_used_site_pips $sites]] miss extra
                set cat [dict create]
                foreach x $miss { dict incr cat "missing:[lindex $x 1]:[lindex $x 2]" }
                foreach x $extra { dict incr cat "extra:[lindex $x 1]:[lindex $x 2]" }
                puts $fp "# sp_check missing [llength $miss] extra [llength $extra] $cat [lrange $miss 0 5] [lrange $extra 0 5]"
            }
        } else {
            set used_sps [_df_used_site_pips $sites]
        }
        foreach x $used_sps {
            puts $fp "sp [join $x { }]"
        }
        # A carry input taken from the chain (CIN) is not a site pip: report
        # it as one (7-series PRECYINIT CIN, UltraScale CARRY8 CIN).
        # (Vectorised: the CI pins on nets driven by exactly one carry
        # output pin.)
        set carries [get_cells -quiet -hierarchical -filter {REF_NAME == CARRY4 || REF_NAME == CARRY8}]
        set conets [get_nets -quiet -of_objects [get_pins -quiet -of_objects $carries -filter {DIRECTION == OUT}]]
        set conets [filter -quiet $conets {DRIVER_COUNT == 1}]
        set cis [get_pins -quiet -leaf -of_objects $conets -filter {REF_PIN_NAME == CI && DIRECTION == IN}]
        set ccells [filter -quiet [get_cells -quiet -of_objects $cis] {REF_NAME == CARRY4 || REF_NAME == CARRY8}]
        foreach ref [_df_props REF_NAME $ccells] site [_df_props SITE $ccells] {
            if {$site eq ""} continue
            # Only when the dedicated COUT -> CIN path is used (a driver
            # that is not the carry directly below is routed through AX).
            if {[llength [get_nets -quiet -of_objects [get_site_pins -quiet $site/CIN]]] == 0} continue
            if {$ref eq "CARRY4"} {
                puts $fp "sp $site PRECYINIT CIN OUT"
            } else {
                puts $fp "sp $site CARRY8 CIN CI"
            }
        }
    }
    puts $fp "# t_sp [expr {[clock milliseconds] - $t0}]"
    # Pad pull resistors (a port property, not in the BEL configuration):
    # reported like a BEL setting of the pad site (NONE when not set).
    foreach port [get_ports -quiet] {
        set s [get_sites -quiet -of_objects $port]
        if {![string match IOB_* $s]} continue
        set v [get_property -quiet PULLTYPE $port]
        if {$v eq ""} { set v NONE }
        puts $fp "cfg $s PAD PULLTYPE $v"
    }
    # Pin inversions of placed cells (IS_<pin>_INVERTED, e.g. the flip-flop
    # clock and set/reset inversion of a slice): a setting Vivado does not
    # expose as BEL configuration nor as a site pip.  The inverter is shared
    # by the BELs whose pin is wired to the same site pin (e.g. the clock of
    # all flip-flops of a slice half), so it is reported on the site pin:
    # "cfg <site> <site pin> IS_<pin>_INVERTED <value>" (the property name is
    # kept: a latch gate and a flip-flop clock invert oppositely), or on the
    # BEL when the pin reaches no site pin.  The BEL pin -> site pin wiring is
    # looked up once per (BEL, pin).  (Vectorised per cell type.)
    # A pin tied to a constant (e.g. a clock whose driver the repair loop
    # removed) is not inverted by its inverter setting: Vivado sets the
    # inverter to produce the constant level.  Such pins are reported as
    # "cfg <site> <site pin> <pin>_LEVEL <0|1>" (the constant after the
    # cell's inversion) instead.
    array unset constpin
    foreach {t lv} {GROUND 0 POWER 1} {
        set cn [get_nets -quiet -hierarchical -filter "TYPE == $t"]
        if {![llength $cn]} continue
        foreach q [get_pins -quiet -leaf -of_objects $cn -filter {DIRECTION == IN}] {
            set constpin($q) $lv
        }
    }
    puts $fp "# t_const [expr {[clock milliseconds] - $t0}]"
    set placed [get_cells -quiet -hierarchical -filter {IS_PRIMITIVE && LOC != ""}]
    set byref [dict create]
    foreach c $placed r [_df_props REF_NAME $placed] { dict lappend byref $r $c }
    set sitepin [dict create]
    set tries [dict create]
    set seen [dict create]
    dict for {ref cells} $byref {
        set props [list_property [lindex $cells 0] IS_*_INVERTED]
        if {![llength $props]} continue
        set sites [_df_props SITE $cells]
        set bels [_df_props BEL $cells]
        foreach p $props {
            set pin [string range $p 3 end-9]
            foreach c $cells s $sites b $bels v [_df_props $p $cells] {
                if {$s eq "" || $b eq "" || $v eq ""} continue
                # (an unconnected cell pin maps to no BEL pin: ask again
                # with the next cell, up to 8 times)
                set k "$b/$pin"
                if {![dict exists $sitepin $k] || ([dict get $sitepin $k] eq "" && [dict get $tries $k] < 8)} {
                    set bp [get_bel_pins -quiet -of_objects [get_pins -quiet $c/$pin]]
                    set sp [get_site_pins -quiet -of_objects $bp]
                    # only an unambiguous wiring (one BEL pin, one site pin)
                    if {[llength $bp] == 1 && [llength $sp] == 1} {
                        dict set sitepin $k [lindex [split $sp /] end]
                    } else {
                        dict set sitepin $k ""
                    }
                    dict incr tries $k
                }
                set where [dict get $sitepin $k]
                if {$where eq ""} { set where [lindex [split $b .] end] }
                if {[info exists constpin($c/$pin)]} {
                    set level [expr {$constpin($c/$pin) ^ [string match *1 $v]}]
                    set line "cfg $s $where ${pin}_LEVEL $level"
                } else {
                    set line "cfg $s $where $p $v"
                }
                if {[dict exists $seen $line]} continue
                dict set seen $line 1
                puts $fp $line
            }
        }
    }
    puts $fp "# t_inv [expr {[clock milliseconds] - $t0}]"
    # Bank wide settings (e.g. the 7-series STEPDOWN of low voltage banks)
    # also change unused pads: report the I/O standards used in each bank on
    # every pad site of the bank.
    set bankstd [dict create]
    set bankin [dict create]
    set bankinstd [dict create]
    foreach port [get_ports -quiet] {
        set pin [get_package_pins -quiet -of_objects $port]
        set std [get_property IOSTANDARD $port]
        if {$pin eq "" || $std eq ""} continue
        set b [get_property BANK $pin]
        dict set bankstd $b $std 1
        # Kinds of inputs in the bank (differential / single ended).
        if {[get_property DIRECTION $port] ne "OUT"} {
            set diff [regexp {^(DIFF_|LVDS|TMDS|MINI_LVDS|BLVDS|RSDS|PPDS|SUB_LVDS|SLVS|LVPECL|MIPI)} $std]
            dict set bankin $b [expr {$diff ? "DIFF" : "SE"}] 1
            dict set bankinstd $b $std 1
        }
    }
    # 7-series bank settings (internal VREF, ...) live in the HCLK_IOI tile
    # of the bank: report them on its IDELAYCTRL site too (same clock
    # region and I/O column as the bank's pads).
    set dlyctl [dict create]
    foreach s [get_sites -quiet -filter {SITE_TYPE == IDELAYCTRL}] {
        if {[regexp {_X(\d+)Y} $s - x]} {
            dict set dlyctl [get_property CLOCK_REGION $s],$x $s
        }
    }
    dict for {bank stds} $bankstd {
        set bsites [get_sites -quiet -of_objects [get_package_pins -quiet -filter "BANK == $bank"]]
        set vref [get_property -quiet INTERNAL_VREF [get_iobanks -quiet $bank]]
        if {$vref eq ""} { set vref NONE }
        set extra [list]
        foreach s $bsites {
            if {[regexp {^IOB_X(\d+)Y} $s - x]} {
                set k [get_property -quiet CLOCK_REGION $s],$x
                if {[dict exists $dlyctl $k]} { lappend extra [dict get $dlyctl $k] }
            }
        }
        foreach s [concat $bsites [lsort -unique $extra]] {
            foreach std [dict keys $stds] { puts $fp "bank $s IOSTD $std" }
            puts $fp "bank $s INTERNAL_VREF $vref"
            if {[dict exists $bankin $bank]} {
                puts $fp "bank $s INPUTS [join [lsort [dict keys [dict get $bankin $bank]]] _]"
                foreach std [dict keys [dict get $bankinstd $bank]] { puts $fp "bank $s INSTD $std" }
            }
        }
    }
    puts $fp "# t_done [expr {[clock milliseconds] - $t0}]"
    close $fp
}
