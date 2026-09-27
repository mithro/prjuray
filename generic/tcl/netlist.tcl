# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
#
# Helpers used by generated fuzzing designs (see generic/python/gen_design.py).
# The generated script builds a netlist directly in Vivado (no synthesis),
# places/routes it and writes a bitstream plus a feature dump.

source [file join [file dirname [info script]] dump_features.tcl]

proc nl_log {msg} {
    global nl_logfp
    puts $nl_logfp $msg
    flush $nl_logfp
}

# Vivado log read back by nl_offenders (a shared Vivado process running
# several designs, see nl_server.tcl, passes its log in NL_VIVADO_LOG).
set nl_vivado_log vivado.log
if {[info exists ::env(NL_VIVADO_LOG)]} { set nl_vivado_log $::env(NL_VIVADO_LOG) }

proc nl_init {part} {
    global nl_logfp nl_vivado_log nl_logpos
    set nl_logfp [open nl.log w]
    # Wall clock stamps (ms since the epoch) for run time accounting.
    nl_log "t_start [clock milliseconds]"
    # Per design state left by a previous design of the same Vivado process.
    foreach v {nl_props nl_was_reset nl_bank_std} {
        global $v
        unset -nocomplain $v
    }
    if {[file exists $nl_vivado_log]} { set nl_logpos [file size $nl_vivado_log] }
    create_project -in_memory -part $part
    link_design -part $part
    nl_log "t_linked [clock milliseconds]"
    set_param messaging.defaultLimit 100000
    set threads 2
    if {[info exists ::env(NL_THREADS)]} { set threads $::env(NL_THREADS) }
    set_param general.maxThreads $threads
    # Router time limit (seconds, NL_ROUTE_TIMELIMIT).  Note: it does not
    # interrupt a global router iteration (an xazu7ev design spent 42 min
    # in 'Global Iteration 0' with a 300 s limit); the repair budget and
    # the runner's time limit (scaled for --region designs) are what bound
    # hung routes.
    if {[info exists ::env(NL_ROUTE_TIMELIMIT)]} {
        set_param route.timeLimit $::env(NL_ROUTE_TIMELIMIT)
    }
    create_cell -reference GND nl_gnd
    create_cell -reference VCC nl_vcc
    create_net nl_const0
    create_net nl_const1
    connect_net -net nl_const0 -objects [get_pins nl_gnd/G]
    connect_net -net nl_const1 -objects [get_pins nl_vcc/P]
}

# Create a cell with parameters.  Parameters Vivado rejects are logged and
# left at their defaults.
proc nl_cell {name ref {props {}} {loc {}} {bel {}}} {
    nl_flush_nets
    if {[catch {create_cell -reference $ref $name} e]} {
        nl_log "cellerr $name $ref $e"
        return 0
    }
    set c [get_cells $name]
    global nl_props
    set nl_props($name) [list]
    foreach {k v} $props {
        if {[catch {set_property $k $v $c} e]} {
            nl_log "properr $name $k $v"
        } else {
            lappend nl_props($name) $k
        }
    }
    if {$loc ne ""} {
        if {$bel ne ""} {
            if {[catch {place_cell $c $loc/$bel} e]} {
                nl_log "placeerr $name $loc/$bel [string range $e 0 200]"
            }
        } else {
            if {[catch {set_property LOC $loc $c} e]} {
                nl_log "locerr $name $loc [string range $e 0 200]"
            }
        }
    }
    return 1
}

# The design's ground net (Vivado may rename or merge the constant nets).
proc nl_gnd {} {
    set n [get_nets -quiet nl_const0]
    if {$n eq ""} {
        set n [lindex [get_nets -quiet -hierarchical -filter {TYPE == GROUND}] 0]
    }
    if {$n eq ""} {
        catch {create_cell -reference GND nl_gnd2}
        create_net nl_const0
        connect_net -net nl_const0 -objects [get_pins nl_gnd2/G]
        set n [get_nets nl_const0]
    }
    return $n
}

# Tie pins to ground.  Clock / set-reset pins of flip-flops, latches and
# shift registers lose their inversion: with it the tied pin is a logic 1
# on the site's shared inverter, and a neighbouring cell tied to ground
# without inversion makes the site unroutable ("Conflicting nets for
# physical connection CLK1INV_OUT / RST_ABCDINV_OUT: GROUND, POWER").
proc nl_tie_gnd {pins} {
    foreach p $pins {
        set c [get_cells -quiet -of_objects $p]
        if {$c eq "" || ![regexp {^(FD|LD|SRL)} [get_property REF_NAME $c]]} continue
        set prop IS_[get_property REF_PIN_NAME $p]_INVERTED
        catch {set_property $prop 1'b0 $c}
    }
    connect_net -net [nl_gnd] -objects $pins
}

# Create net <name> connecting the given pins (first one is the driver).
# A missing driver (e.g. an I/O buffer that could not be created) ties the
# loads to ground; I/O logic left without loads is removed.
proc nl_net {name pins} {
    set drv [get_pins -quiet [lindex $pins 0]]
    set loads [get_pins -quiet [lrange $pins 1 end]]
    if {$drv eq ""} {
        set tie [list]
        foreach l $loads {
            set c [get_cells -of_objects $l]
            if {[regexp {^(IDDR|ODDR|ISERDES|OSERDES|IDELAY|ODELAY|RX_BITSLICE|TX_BITSLICE|RXTX_BITSLICE)} [get_property REF_NAME $c]]} {
                # I/O logic whose I/O buffer is missing cannot be placed;
                # removed before implementation (not while building).
                global nl_orphans
                lappend nl_orphans [get_property NAME $c]
            } else {
                lappend tie $l
            }
        }
        if {[llength $tie]} {
            if {[catch {nl_tie_gnd $tie} e]} {
                nl_log "connerr $name [string range $e 0 200]"
            }
        }
        return
    }
    if {[llength $loads] == 0} {
        set c [get_cells -of_objects $drv]
        if {[regexp {^(IDDR|ODDR|ISERDES|OSERDES|IDELAY|ODELAY|TX_BITSLICE$|RXTX_BITSLICE)} [get_property REF_NAME $c]]} {
            global nl_orphans
            lappend nl_orphans [get_property NAME $c]
        }
        return
    }
    # Nets are created and connected in batches (one create_net and one
    # connect_net call per batch is much faster than one per net).
    global nl_pending
    lappend nl_pending $name [concat $drv $loads]
    if {[llength $nl_pending] >= 4000} { nl_flush_nets }
}

# Create and connect the nets queued by nl_net.  A failing batch is redone
# net by net so that errors are logged per net as before.
set nl_pending [list]
proc nl_flush_nets {} {
    global nl_pending
    if {[llength $nl_pending] == 0} return
    set batch $nl_pending
    set nl_pending [list]
    set names [list]
    foreach {n -} $batch { lappend names $n }
    if {[catch {create_net $names}]} {
        set ok [list]
        foreach {n objs} $batch {
            if {[llength [get_nets -quiet $n]]} {
                nl_log "neterr $n exists"
            } elseif {[catch {create_net $n} e]} {
                nl_log "neterr $n $e"
            } else {
                lappend ok $n $objs
            }
        }
        set batch $ok
    }
    if {[catch {connect_net -net_object_list $batch}]} {
        foreach {n objs} $batch {
            set have [get_pins -quiet -of_objects [get_nets -quiet $n]]
            set todo [list]
            foreach o $objs {
                if {[lsearch -exact $have $o] < 0} { lappend todo $o }
            }
            if {[llength $todo] == 0} continue
            if {[catch {connect_net -net $n -objects [get_pins -quiet $todo]} e]} {
                nl_log "connerr $n [string range $e 0 200]"
            }
        }
    }
}

# Connect pins to an existing net (e.g. the constant nets).
proc nl_conn {net pins} {
    nl_flush_nets
    set objs [get_pins -quiet $pins]
    if {[llength $objs] == 0} return
    if {[catch {connect_net -net $net -objects $objs} e]} {
        nl_log "connerr $net [string range $e 0 200]"
    }
}

# Create a top level port with an I/O buffer attached to <pin>.
proc nl_port {name dir pin} {
    nl_flush_nets
    if {[catch {create_port -direction $dir $name} e]} {
        nl_log "porterr $name $e"
        return
    }
    if {[catch {
        create_net ${name}_n
        connect_net -net ${name}_n -objects [list [get_ports $name] [get_pins $pin]]
    } e]} {
        nl_log "porterr $name [string range $e 0 200]"
    }
}

# Top level port on the bonded package pin of <site> whose PIN_FUNC matches
# <func>, connected to cell pin <pin> (dedicated pads of GTs, reference
# clock buffers, ...).  Without such a pin the cell is removed.
proc nl_padpin {name pin site func dir} {
    set p [get_pins -quiet $pin]
    if {$p eq ""} return
    set pp ""
    foreach x [get_package_pins -quiet -of_objects [get_sites -quiet $site]] {
        if {[regexp $func [get_property PIN_FUNC $x]]} { set pp $x; break }
    }
    if {$pp eq "" || [llength [get_ports -quiet -of_objects $pp]]} {
        nl_log "padless $name $site"
        catch {remove_cell [get_cells -of_objects $p]}
        return
    }
    if {$dir eq "CHECK"} return
    if {[catch {
        create_port -direction $dir $name
        create_net ${name}_pad
        connect_net -net ${name}_pad -objects [list [get_ports $name] $p]
        set_property PACKAGE_PIN $pp [get_ports $name]
    } e]} {
        nl_log "padpinerr $name [string range $e 0 200]"
    }
}

# Returns generated cell names mentioned in ERROR messages written to
# vivado.log since the last call.
set nl_logpos 0
proc nl_offenders {{extra ""}} {
    global nl_logpos nl_vivado_log
    set names [list]
    set nets [list]
    if {[catch {
        set lf [open $nl_vivado_log r]
        seek $lf $nl_logpos
        set txt [read $lf]
        set nl_logpos [tell $lf]
        close $lf
        append txt "\n" $extra
        global nl_lasttxt
        set nl_lasttxt $txt
    } e]} {
        nl_log "logread $e"
        return [list {} {}]
    }
    # Message ids of errors: warnings with the same id often name the cells.
    set ids [list]
    foreach {- id} [regexp -all -inline {ERROR: \[([^\]]+)\]} $txt] {
        lappend ids $id
    }
    set grab 0
    foreach line [split $txt "\n"] {
        if {[string match "ERROR:*" $line]} {
            set grab 12
        } elseif {[regexp {^(CRITICAL WARNING|WARNING): \[([^\]]+)\]} $line - - id] && [lsearch -exact $ids $id] >= 0} {
            set grab 12
        } elseif {[regexp {^(INFO|WARNING|CRITICAL WARNING|Phase|Resolution|Time)} $line]} {
            set grab 0
        }
        # A constant that cannot reach a (possibly unconnected, hence tied)
        # hard block pin: blame the cells on that site.
        if {[regexp {(?:Gnd|Vcc) Src -> ([A-Z][A-Z0-9_]*_X\d+Y\d+)/} $line - s]} {
            foreach c [get_cells -quiet -of_objects [get_sites -quiet $s]] {
                lappend names [get_property NAME $c]
            }
        }
        # I/O placer reports list the terminals on continuation lines far
        # below the ERROR ("Term: io12_p", "occupied by term: io13_n").
        foreach {- n} [regexp -all -inline -nocase {\mterm: ((?:c|io)\d+)(?![0-9])} $line] {
            lappend names $n
        }
        if {[regexp {Net: (\S+) is not completely routed} $line - n]} {
            lappend nets $n
        }
        if {[regexp {Router will skip net (\S+)} $line - n]} {
            lappend nets $n
        }
        # Unreachable hard block pins: drop the net, keep the block.
        if {[regexp {router will skip routing of net (\S+?)\.?$} $line - n]} {
            lappend nets $n
        }
        if {[regexp {problem bus\(es\) and/or net\(s\) are (.*)\.$} $line - lst]} {
            foreach n [split [string map {, " "} $lst]] {
                if {$n ne ""} { lappend nets $n }
            }
        }
        if {$grab > 0} {
            incr grab -1
            # Generated cell names, possibly with a suffix (port "_p"/"_n",
            # Vivado inserted "_OPT_INSERTED" cells, ...).
            foreach {- n} [regexp -all -inline {\m((?:c|io)\d+)(?![0-9])} $line] {
                lappend names $n
            }
            # Net names: blame the driving cell.
            foreach {- n} [regexp -all -inline {\m(n\d+)\M} $line] {
                set net [get_nets -quiet $n]
                if {$net ne ""} {
                    foreach c [get_cells -quiet -of_objects [get_pins -quiet -of_objects $net -filter {DIRECTION == OUT}]] {
                        lappend names [get_property NAME $c]
                    }
                }
            }
            # Errors naming sites (e.g. bitgen): take the cells placed there.
            foreach {- s} [regexp -all -inline {\m([A-Z][A-Z0-9_]*_X\d+Y\d+)\M} $line] {
                set site [get_sites -quiet $s]
                if {$site eq "" && ![regexp {^(CLE|CLB|INT)} $s]} {
                    # A tile (e.g. bitgen "VEAM exception in tile
                    # BRAM_X56Y125"): the cells of its hard block sites.
                    set site [get_sites -quiet -of_objects [get_tiles -quiet $s]]
                }
                if {$site ne "" && ![string match SLICE_* $s]} {
                    foreach c [get_cells -quiet -of_objects $site] {
                        lappend names [get_property NAME $c]
                    }
                }
            }
        }
    }
    # The constant nets cannot be disconnected (their unreachable loads are
    # blamed above instead).
    set keep [list]
    foreach n [lsort -unique $nets] {
        set net [get_nets -quiet $n]
        if {$n in {GNDNet VCCNet nl_const0 nl_const1} || ($net ne "" && [get_property TYPE $net] in {GROUND POWER})} continue
        lappend keep $n
    }
    return [list [lsort -unique $names] $keep]
}

# True if the last log chunk read by nl_offenders mentions pblocks.
set nl_lasttxt ""
proc nl_pblock_trouble {} {
    global nl_lasttxt
    foreach line [split $nl_lasttxt "\n"] {
        if {[regexp {^(ERROR|CRITICAL WARNING)} $line] && [regexp -nocase {pblock|area group|area constraint} $line]} {
            return 1
        }
    }
    # Placement failures inside a pblock are reported on the next lines.
    return [regexp -nocase {in pblock} $nl_lasttxt]
}

# Fallback for errors naming a primitive kind rather than a cell (e.g.
# "does not support the STARTUP component ..."): cells whose REF_NAME starts
# with an upper case word of the error text.
proc nl_ref_offenders {txt} {
    set refs [lsort -unique [get_property REF_NAME [get_cells -quiet -hierarchical -filter {IS_PRIMITIVE}]]]
    set names [list]
    foreach line [split $txt "\n"] {
        if {![string match "*ERROR*" $line]} continue
        foreach w [regexp -all -inline {\m[A-Z][A-Z0-9_]{3,}\M} $line] {
            if {$w in {ERROR DRC}} continue
            foreach r $refs {
                if {[string match "${w}*" $r] && ![regexp {^(LUT|FD|LD|CARRY|MUXF|SRL|GND|VCC)} $r]} {
                    foreach c [get_cells -quiet -hierarchical -filter "REF_NAME == $r"] {
                        lappend names [get_property NAME $c]
                    }
                }
            }
        }
    }
    return [lsort -unique $names]
}

# Over-utilisation (DRC UTLZ-1) names no cells: remove half of the cells of
# the over-used resource kinds.
proc nl_utlz_offenders {txt} {
    set kinds {
        {RAMB|FIFO} {^(RAMB|FIFO)}
        {DSP} {^DSP}
        {ILOGIC} {^(IDDR|ISERDES)}
        {OLOGIC} {^(ODDR|OSERDES)}
        {IDELAY} {^IDELAY}
        {ODELAY} {^ODELAY}
        {BUFG} {^BUFG}
        {BUFH} {^BUFH}
        {BUFR} {^BUFR}
        {BUFIO} {^BUFIO}
        {MMCM} {^MMCM}
        {PLL} {^PLL}
        {GT} {^GT}
        {URAM} {^URAM}
        {BITSLICE} {BITSLICE}
    }
    set names [list]
    foreach line [split $txt "\n"] {
        if {![regexp {UTLZ-1\] Resource utilization: (.*) over-utilized} $line - what]} continue
        foreach {key re} $kinds {
            if {![regexp $key $what]} continue
            set i 0
            foreach c [get_cells -quiet -hierarchical -filter {IS_PRIMITIVE}] {
                if {[regexp $re [get_property REF_NAME $c]] && [incr i] % 2 == 0} {
                    lappend names [get_property NAME $c]
                }
            }
        }
    }
    return [lsort -unique $names]
}

# Carry chains must be removed as a whole: a chain cut in the middle leaves
# a CARRY cell whose CI is tied to a constant, which Vivado legalises with
# inserted GND/CARRY cells the placer then rejects.
proc nl_carry_chains {cells} {
    set todo [get_cells -quiet $cells -filter {REF_NAME =~ CARRY*}]
    if {[llength $todo] == 0} { return $cells }
    set seen [dict create]
    foreach c $todo { dict set seen [get_property NAME $c] 1 }
    while {[llength $todo]} {
        set c [lindex $todo end]
        set todo [lrange $todo 0 end-1]
        set pins [get_pins -quiet -of_objects $c -filter {REF_PIN_NAME =~ CO* || REF_PIN_NAME == CI || REF_PIN_NAME =~ CI_TOP}]
        foreach n [get_nets -quiet -of_objects $pins] {
            if {[get_property TYPE $n] in {GROUND POWER}} continue
            foreach p [get_pins -quiet -of_objects $n -filter {REF_PIN_NAME =~ CO* || REF_PIN_NAME == CI || REF_PIN_NAME =~ CI_TOP}] {
                set o [get_cells -quiet -of_objects $p]
                if {[get_property REF_NAME $o] ni {CARRY4 CARRY8}} continue
                set on [get_property NAME $o]
                if {![dict exists $seen $on]} {
                    dict set seen $on 1
                    lappend todo $o
                }
            }
        }
    }
    return [lsort -unique [concat [get_property NAME [get_cells -quiet $cells]] [dict keys $seen]]]
}

proc nl_remove {names} {
    # Only ever remove our own cells: removing cells Vivado inserted (e.g.
    # legalisation CARRY4/GND cells) corrupts its placer database (segfault).
    set names [lsearch -all -inline -regexp [nl_carry_chains $names] {^(?:c|io)\d+$}]
    set cells [get_cells -quiet $names]
    if {[llength $cells] == 0} { return 0 }
    # ... together with the cells Vivado inserted for them (e.g. the
    # ZHOLD_DELAY "<cell>_OPT_INSERTED"), which would be left dangling.
    foreach n $names {
        foreach c [get_cells -quiet "${n}_OPT_INSERTED*"] { lappend cells $c }
    }
    nl_log "removing [llength $cells] cells: [lrange $names 0 20]"
    # Drop placement constraints first: a removed cell or port left in
    # Vivado's placement constraint store crashes the next route_design
    # (PSSiteStore::getCheckSum).
    set ports [get_ports -quiet -of_objects [get_nets -quiet -of_objects [get_pins -quiet -of_objects $cells]]]
    foreach prop {LOC BEL IS_LOC_FIXED IS_BEL_FIXED} { catch {reset_property $prop $cells} }
    if {[llength $ports]} {
        foreach prop {PACKAGE_PIN LOC} { catch {reset_property $prop $ports} }
    }
    # Remove IO ports attached to removed IO buffers as well.
    foreach p $ports { catch {remove_port $p} }
    set onets [get_nets -quiet -of_objects [get_pins -quiet -of_objects $cells -filter {DIRECTION == OUT}]]
    # The design is always placed again after a removal: unplace everything
    # so no stale placement refers to removed cells.
    catch {route_design -unroute}
    catch {place_design -unplace}
    if {[catch {remove_cell $cells} e]} { nl_log "removeerr $e" }
    # Nets left without a driver: detach their loads.
    set dangling [list]
    foreach n $onets {
        if {[llength [get_pins -quiet -of_objects $n -filter {DIRECTION == OUT}]] == 0} {
            lappend dangling [get_property NAME $n]
        }
    }
    if {[llength $dangling]} { nl_unroutable $dangling }
    return [llength $cells]
}

# Detach the loads of unroutable nets.  Returns the number of nets handled
# (0 when none of them could be found or they are constant nets).
proc nl_unroutable {bad} {
    nl_log "disconnecting [llength $bad] unroutable nets"
    set done 0
    foreach n $bad {
        set net [get_nets -quiet $n]
        if {$net eq ""} continue
        # Never tear down the global constant nets.
        if {[get_property TYPE $net] in {GROUND POWER} || $n in {nl_const0 nl_const1}} continue
        incr done
        set loads [get_pins -quiet -of_objects $net -filter {DIRECTION == IN}]
        catch {disconnect_net -net $net -objects $loads}
        # Fabric cells need their pins connected: tie them to ground.
        set fab [list]
        foreach p $loads {
            if {[regexp {^(LUT|FD|LD|CARRY|MUXF|SRL)} [get_property REF_NAME [get_cells -of_objects $p]]]} {
                lappend fab $p
            }
        }
        if {[llength $fab]} {
            if {[catch {nl_tie_gnd $fab} e]} {
                nl_log "tieerr [string range $e 0 150]"
            }
        }
    }
    return $done
}

# Cells to blame for nets that cannot be repaired by disconnecting them:
# the net's drivers, or for a net internal to a macro (e.g. "io12/I" of an
# OBUFDS, not visible to get_nets) the macro cell named before the "/".
proc nl_net_cells {nets} {
    set cells [list]
    foreach n $nets {
        set net [get_nets -quiet $n]
        if {$net ne ""} {
            foreach c [get_cells -quiet -of_objects [get_pins -quiet -of_objects $net -filter {DIRECTION == OUT}]] {
                lappend cells [get_property NAME $c]
            }
        }
        while {[string first / $n] >= 0} {
            set n [string range $n 0 [expr {[string last / $n] - 1}]]
            if {[get_cells -quiet $n] ne ""} { lappend cells $n; break }
        }
    }
    return [lsort -unique $cells]
}

# Disconnect unroutable nets; when that changes nothing or the same nets
# failed before, remove the cells driving them instead.  Returns 0 when
# nothing could be repaired.
set nl_seen_bad [dict create]
proc nl_repair_nets {nets} {
    global nl_seen_bad
    set again 0
    foreach n $nets {
        if {[dict exists $nl_seen_bad $n]} { set again 1 }
        dict set nl_seen_bad $n 1
    }
    if {!$again && [nl_unroutable $nets] > 0} { return 1 }
    set cells [nl_net_cells $nets]
    nl_log "removing [llength $cells] cells driving unrepairable nets"
    return [nl_remove $cells]
}

# Tie every unconnected input pin of fabric cells to ground (removed cells
# leave their loads dangling; LUT equations need all their inputs).
proc nl_fix_dangling {} {
    # Nets that lost their driver: detach their loads (fabric loads get tied
    # to ground below / by nl_unroutable).
    set orphaned [get_nets -quiet -hierarchical -filter {DRIVER_COUNT == 0 && TYPE == SIGNAL && FLAT_PIN_COUNT > 0}]
    if {[llength $orphaned]} {
        nl_log "driverless nets [llength $orphaned]"
        nl_unroutable [get_property NAME $orphaned]
    }
    set cells [get_cells -quiet -hierarchical -filter {IS_PRIMITIVE && (REF_NAME =~ LUT* || REF_NAME =~ FD* || REF_NAME =~ LD* || REF_NAME =~ CARRY* || REF_NAME =~ MUXF* || REF_NAME =~ SRL*)}]
    if {[llength $cells] == 0} return
    set pins [get_pins -quiet -of_objects $cells -filter {DIRECTION == IN}]
    set dangling [list]
    foreach p $pins {
        if {[llength [get_nets -quiet -of_objects $p]] == 0} { lappend dangling $p }
    }
    if {[llength $dangling]} {
        nl_log "tying [llength $dangling] dangling pins"
        if {[catch {nl_tie_gnd $dangling} e]} {
            nl_log "tieerr [string range $e 0 150]"
        }
    }
}

# "Instance GND of type GND is not Placeable": constant cells inserted by
# Vivado (e.g. for the CI pin of a carry chain whose neighbour was removed)
# that cannot be placed.  Blame the cells they drive.
proc nl_const_offenders {txt} {
    set cells [list]
    foreach {- c} [regexp -all -inline {Instance\s+(\S+) of type (?:GND|VCC) is not Placeable} $txt] {
        foreach n [get_nets -quiet -of_objects [get_pins -quiet -of_objects [get_cells -quiet $c]]] {
            foreach l [get_cells -quiet -of_objects [get_pins -quiet -of_objects $n -filter {DIRECTION == IN}]] {
                set ln [get_property NAME $l]
                if {[regexp {^(?:c|io)\d+$} $ln]} { lappend cells $ln; continue }
                # A cell Vivado inserted (removing it corrupts the placer
                # database): blame our cells connected to it instead.
                foreach nn [get_nets -quiet -of_objects [get_pins -quiet -of_objects $l]] {
                    if {[get_property TYPE $nn] in {GROUND POWER}} continue
                    foreach o [get_cells -quiet -of_objects [get_pins -quiet -of_objects $nn]] {
                        set on [get_property NAME $o]
                        if {[regexp {^(?:c|io)\d+$} $on]} { lappend cells $on }
                    }
                }
            }
        }
    }
    if {[llength $cells]} { nl_log "blaming loads of unplaceable constants: [llength $cells] cells" }
    return [lsort -unique $cells]
}

# Optional place_design / route_design directive from the environment
# (NL_PLACE_DIRECTIVE, NL_ROUTE_DIRECTIVE, e.g. Quick); none by default.
proc nl_directive {what} {
    if {[info exists ::env(NL_${what}_DIRECTIVE)] && $::env(NL_${what}_DIRECTIVE) ne ""} {
        return [list -directive $::env(NL_${what}_DIRECTIVE)]
    }
    return [list]
}

set nl_orphans [list]
proc nl_finish {{relaxclk 0}} {
    nl_flush_nets
    set t0 [clock seconds]
    nl_log "t_built [clock milliseconds]"
    global nl_orphans
    if {[llength $nl_orphans]} {
        nl_log "orphans [llength $nl_orphans]"
        nl_remove [lsort -unique $nl_orphans]
    }
    if {$relaxclk} {
        catch {set_property CLOCK_DEDICATED_ROUTE FALSE [get_nets -quiet -hierarchical]}
        # Except the outputs of clock generators / transceivers: routed
        # through general interconnect they end in unresolvable overlaps.
        set gen [get_cells -quiet -hierarchical -filter {REF_NAME =~ MMCM* || REF_NAME =~ PLL* || REF_NAME =~ GT* || REF_NAME =~ IBUFDS_GT*}]
        if {[llength $gen]} {
            catch {set_property CLOCK_DEDICATED_ROUTE TRUE [get_nets -quiet -of_objects [get_pins -quiet -of_objects $gen -filter {DIRECTION == OUT}]]}
        }
    }
    foreach d [get_drc_checks] {
        catch {set_property SEVERITY Warning $d}
    }
    nl_offenders
    # Implement; after any failure remove the cells the errors name (or
    # disconnect unroutable nets) and try again.
    set stage place
    for {set attempt 0} {$attempt < 40} {incr attempt} {
        set budget 2700
        if {[info exists ::env(NL_BUDGET)]} { set budget $::env(NL_BUDGET) }
        if {[clock seconds] - $t0 > $budget} {
            nl_log "giving up: time budget exceeded"
            return
        }
        if {$stage eq "place"} {
            nl_fix_dangling
            if {[catch {place_design {*}[nl_directive PLACE]} e]} {
                nl_log "place_design failed: [string range $e 0 200]"
                lassign [nl_offenders "ERROR: $e"] names nets
                global nl_lasttxt
                if {[llength $names] == 0} { set names [nl_const_offenders $nl_lasttxt] }
                if {[llength $names] == 0} { set names [nl_utlz_offenders $nl_lasttxt] }
                if {[llength [get_pblocks -quiet]] && [nl_pblock_trouble]} {
                    nl_log "dropping pblocks (pblock errors)"
                    catch {place_design -unplace}
                    catch {delete_pblocks [get_pblocks]}
                    continue
                }
                if {[nl_remove $names] == 0} {
                    if {[llength [get_pblocks -quiet]]} {
                        nl_log "dropping pblocks"
                        catch {place_design -unplace}
                        catch {delete_pblocks [get_pblocks]}
                        continue
                    }
                    return
                }
                continue
            }
            nl_log "placed [expr [clock seconds] - $t0]"
            global nl_wanted_pips
            if {[llength $nl_wanted_pips]} { nl_force_pips; set nl_wanted_pips [list] }
            set stage route
        }
        if {$stage eq "route"} {
            if {[catch {route_design {*}[nl_directive ROUTE]} e]} {
                nl_log "route_design failed: [string range $e 0 200]"
                lassign [nl_offenders "ERROR: $e"] names nets
                catch {route_design -unroute}
                global nl_lasttxt nl_unrelaxed
                if {[string match "*Fixed routes overlap*" $nl_lasttxt] && ![info exists nl_unrelaxed]} {
                    # Clock nets routed through the fabric (relaxed dedicated
                    # routes) conflict with the global clock routes: go back
                    # to dedicated clock routing and place again.
                    set nl_unrelaxed 1
                    nl_log "fixed routes overlap: dedicated clock routes again"
                    catch {set_property CLOCK_DEDICATED_ROUTE TRUE [get_nets -quiet -hierarchical -filter {CLOCK_DEDICATED_ROUTE == FALSE}]}
                    catch {place_design -unplace}
                    set stage place
                    continue
                }
                if {[llength $nets]} {
                    # Placement is unchanged: route again directly (unless
                    # cells had to be removed).
                    set nfab [llength [get_cells -quiet -hierarchical]]
                    if {[nl_repair_nets $nets] == 0} { return }
                    if {[llength [get_cells -quiet -hierarchical]] != $nfab} { set stage place }
                    continue
                } elseif {[nl_remove $names] == 0} {
                    nl_log "route: nothing to repair"
                    set stage write
                    continue
                }
                set stage place
                continue
            }
            nl_log "routed [expr [clock seconds] - $t0]"
            set stage write
        }
        if {$stage eq "write"} {
            set_property BITSTREAM.GENERAL.PERFRAMECRC YES [current_design]
            if {[catch {write_bitstream -force design.bit} e]} {
                nl_log "write_bitstream failed: [string range $e 0 200]"
                lassign [nl_offenders "ERROR: $e"] names nets
                # First try resetting the random parameters of the blamed
                # cells (keeps the site in use), then remove them.
                global nl_props nl_was_reset
                set reset 0
                foreach n $names {
                    if {[info exists nl_props($n)] && [llength $nl_props($n)] && ![info exists nl_was_reset($n)]} {
                        set nl_was_reset($n) 1
                        foreach k $nl_props($n) { catch {reset_property $k [get_cells $n]} }
                        incr reset
                    }
                }
                if {$reset} {
                    nl_log "reset parameters of $reset cells"
                    continue
                }
                if {[llength $names] == 0} {
                    global nl_lasttxt
                    set names [nl_ref_offenders $nl_lasttxt]
                    nl_log "blaming by primitive kind: [llength $names] cells"
                }
                if {[llength $nets]} {
                    catch {route_design -unroute}
                    if {[nl_repair_nets $nets] == 0} { return }
                    set stage place
                    continue
                }
                if {[nl_remove $names] == 0} { return }
                catch {route_design -unroute}
                set stage place
                continue
            }
            nl_log "written [expr [clock seconds] - $t0]"
            if {[info exists ::env(NL_DCP)]} { write_checkpoint -force design.dcp }
            dump_features design.features
            nl_log "dumped [expr [clock seconds] - $t0]"
            set refs [dict create]
            foreach c [get_cells -quiet -hierarchical -filter {IS_PRIMITIVE}] {
                dict incr refs [get_property REF_NAME $c]
            }
            nl_log "final $refs"
            nl_log "done [expr [clock seconds] - $t0]"
            return
        }
    }
}

# Constrain cells to a region.
proc nl_pblock {name range cells} {
    nl_flush_nets
    if {[catch {
        set pb [create_pblock $name]
        resize_pblock $pb -add $range
        add_cells_to_pblock $pb [get_cells $cells]
        set_property IS_SOFT FALSE $pb
    } e]} {
        nl_log "pblockerr $name [string range $e 0 200]"
    }
}

# I/O buffer on a pad site with a random, bank consistent I/O standard.
#   mode: in out tri inout diffin diffout difftri
#   stds: list of STANDARD:VCCO pairs (single ended and differential mixed,
#         differential ones must start with DIFF_ or be in diffstds)
#   props: random buffer/port properties to try
set nl_bank_vcco [dict create]
# Input reference voltage of the single ended VREF standards (7-series /
# UltraScale HP/HR).
proc nl_vref_of {std} {
    foreach {re v} {
        {^SSTL135} 0.675
        {^(SSTL15|HSTL_I$|HSTL_II$|HSTL_I_DCI$|HSTL_II_DCI$|HSTL_II_T_DCI$)} 0.75
        {^(SSTL18|HSTL_I_18|HSTL_II_18|HSTL_I_DCI_18|HSTL_II_DCI_18|HSTL_II_T_DCI_18|MOBILE_DDR)} 0.9
        {^(SSTL12|HSUL_12|HSTL_I_12|HSTL_I_DCI_12)} 0.6
        {^POD12} 0.84
        {^POD10} 0.7
    } {
        if {[regexp $re $std]} { return $v }
    }
    return ""
}

# Internal VREF: in banks whose single ended inputs all use one VREF
# standard, use the bank's internal reference with probability p (the
# INTERNAL_VREF bank property; otherwise the VREF pins supply it).
proc nl_internal_vref {p} {
    set want [dict create]
    foreach port [get_ports -quiet -filter {DIRECTION != OUT}] {
        set pin [get_package_pins -quiet -of_objects $port]
        if {$pin eq ""} continue
        set v [nl_vref_of [get_property IOSTANDARD $port]]
        set b [get_property BANK $pin]
        if {$v eq ""} continue
        if {[dict exists $want $b] && [dict get $want $b] ne $v} {
            dict set want $b -
        } else {
            dict set want $b $v
        }
    }
    dict for {b v} $want {
        if {$v eq "-" || rand() >= $p} continue
        if {[catch {set_property INTERNAL_VREF $v [get_iobanks $b]} e]} {
            nl_log "vreferr $b $v [string range $e 0 150]"
        } else {
            nl_log "internal_vref $b $v"
        }
    }
}

# Remove a half built I/O buffer: its pad nets first (with them connected
# remove_port / remove_cell fail, leaving a port at the default standard
# that conflicts with the bank VCCO: "Bank 65 has terminals with
# incompatible standards").
proc nl_iob_drop {name ports} {
    foreach p $ports {
        catch {remove_net [get_nets -quiet ${p}_pad]}
        catch {remove_port [get_ports -quiet $p]}
    }
    catch {remove_cell [get_cells -quiet $name]}
    foreach p $ports {
        if {[llength [get_ports -quiet $p]]} { nl_log "ioberr $name port $p left" }
    }
}

proc nl_iob {name site mode ref stds props} {
    nl_flush_nets
    global nl_bank_vcco
    set s [get_sites -quiet $site]
    if {$s eq ""} { nl_log "ioberr $name nosite"; return 0 }
    set pin [get_package_pins -quiet -of_objects $s]
    if {$pin eq ""} { nl_log "ioberr $name unbonded"; return 0 }
    # Pad already used (e.g. the N side of a differential pair).
    if {[llength [get_ports -quiet -of_objects $pin]]} {
        nl_log "ioberr $name padused"
        return 0
    }
    # The DCI reference resistor pin: DCI standards of its bank need it free.
    # Same for the VREF pins of standards with an input reference.
    if {[regexp {VRP|VREF} [get_property -quiet PIN_FUNC $pin]]} {
        nl_log "ioberr $name vref"
        return 0
    }
    set pair [get_property -quiet DIFF_PAIR_PIN $pin]
    # (The N side of a differential buffer must not land on them either.)
    if {[string match diff* $mode] && $pair ne "" && [regexp {VRP|VREF} [get_property -quiet PIN_FUNC [get_package_pins -quiet $pair]]]} {
        nl_log "ioberr $name vref"
        return 0
    }
    if {[string match diff* $mode] && $pair ne "" && [llength [get_ports -quiet -of_objects [get_package_pins -quiet $pair]]]} {
        set mode [dict get {diffin in diffout out difftri tri} $mode]
        set ref [dict get {in IBUF out OBUF tri OBUFT} $mode]
    }
    set bank [get_iobanks -quiet -of_objects $s]
    # A quarter of the banks take only differential inputs (bank settings
    # such as the 7-series HCLK_IOI ONLY_DIFF_IN_USE depend on it).
    global nl_bank_diffonly
    if {![info exists nl_bank_diffonly]} { set nl_bank_diffonly [dict create] }
    if {![dict exists $nl_bank_diffonly $bank]} {
        dict set nl_bank_diffonly $bank [expr {rand() < 0.25}]
    }
    if {[string match diff* $mode] && ![regexp {_L\d+P} [get_property PIN_FUNC $pin]]} {
        # Not the P side of a pair: use the single ended equivalent.
        set mode [dict get {diffin in diffout out difftri tri} $mode]
        set ref [dict get {in IBUF out OBUF tri OBUFT} $mode]
        set ses [list]
        foreach sv $stds {
            if {![regexp {^(DIFF_|LVDS|TMDS|MINI_LVDS|BLVDS|RSDS|PPDS|SUB_LVDS|SLVS|LVPECL|MIPI)} $sv]} { lappend ses $sv }
        }
        # (LVCMOS33 does not exist in High Performance banks.)
        if {[llength $ses] == 0} { set ses {LVCMOS18:1.8 LVCMOS12:1.2} }
        set stds $ses
    }
    if {[dict get $nl_bank_diffonly $bank] && $mode in {in inout}} {
        nl_log "ioberr $name diffonly"
        return 0
    }
    global nl_bank_std
    if {![info exists nl_bank_std]} { set nl_bank_std [dict create] }
    # (Differential inputs and outputs remember their standard separately:
    # some differential standards are receiver only.)
    set diffkey [expr {[string match diff* $mode] ? ($mode eq "diffin" ? "di" : "do") : "s"}]
    if {$mode eq "inout"} {
        # Bidirectional ports need a bidirectional standard.
        set bi [list]
        foreach sv $stds {
            if {[regexp {^(LVCMOS|LVTTL|LVDCI)|_T_DCI:} $sv] && ![string match LVCMOS10:* $sv]} { lappend bi $sv }
        }
        set stds $bi
    }
    if {[dict exists $nl_bank_std $bank,$diffkey] && $mode ne "inout"} {
        # One standard per bank (VREF / DCI requirements must agree).
        set cands [list [dict get $nl_bank_std $bank,$diffkey]]
    } elseif {[dict exists $nl_bank_vcco $bank]} {
        set v [dict get $nl_bank_vcco $bank]
        set cands [list]
        foreach sv $stds {
            if {[lindex [split $sv :] 1] eq $v} { lappend cands $sv }
        }
    } else {
        set cands $stds
    }
    if {[llength $cands] == 0} { nl_log "ioberr $name novcco"; return 0 }
    set sv [lindex $cands [expr {int(rand() * [llength $cands])}]]
    set std [lindex [split $sv :] 0]
    set vcco [lindex [split $sv :] 1]
    if {[dict exists $nl_bank_vcco $bank] && [dict get $nl_bank_vcco $bank] ne $vcco} {
        # (A standard remembered for the bank with another VCCO.)
        nl_log "ioberr $name novcco"
        return 0
    }
    if {[catch {create_cell -reference $ref $name} e]} {
        nl_log "cellerr $name $ref [string range $e 0 150]"
        return 0
    }
    set diff [string match diff* $mode]
    set ports [list ${name}_p]
    if {$diff} { lappend ports ${name}_n }
    foreach p $ports {
        set dir [expr {$mode in {in diffin} ? "IN" : ($mode eq "inout" ? "INOUT" : "OUT")}]
        create_port -direction $dir $p
    }
    # Pad pins of the buffer: I (inputs), O (outputs), IO (bidir), IB/OB (n).
    set padpins [list]
    foreach bp [get_pins -of_objects [get_cells $name]] {
        set rp [get_property REF_PIN_NAME $bp]
        if {$mode in {in diffin} && $rp in {I IB}} { lappend padpins $rp $bp }
        if {$mode in {out tri diffout difftri} && $rp in {O OB}} { lappend padpins $rp $bp }
        if {$mode eq "inout" && $rp in {IO}} { lappend padpins $rp $bp }
    }
    foreach {rp bp} $padpins {
        set p [expr {$rp in {IB OB} ? "${name}_n" : "${name}_p"}]
        if {$diff == 0 && $rp in {IB OB}} continue
        create_net ${p}_pad
        connect_net -net ${p}_pad -objects [list [get_ports $p] $bp]
    }
    if {[catch {set_property PACKAGE_PIN $pin [get_ports ${name}_p]} e]} {
        nl_log "ioberr $name pin [string range $e 0 150]"
        nl_iob_drop $name $ports
        return 0
    }
    foreach p $ports {
        if {[catch {set_property IOSTANDARD $std [get_ports $p]} e]} {
            # Left at the default standard the buffer conflicts with the
            # bank VCCO chosen here: drop it.
            nl_log "ioberr $name IOSTANDARD $std"
            nl_iob_drop $name $ports
            return 0
        }
    }
    set btype [get_property -quiet BANK_TYPE $bank]
    # The default drive strength (12) does not exist for LVCMOS12/10 in
    # High Performance banks.
    if {$mode ni {in diffin} && [regexp {^LVCMOS1[02]$} $std] && [string match *HIGH_PERFORMANCE* $btype] && [lsearch -exact $props DRIVE] < 0} {
        lappend props DRIVE [lindex {2 4 6 8} [expr {int(rand() * 4)}]]
    }
    foreach {k v} $props {
        # Input termination only exists for the SSTL/HSTL/HSUL family and
        # drive strengths only for LVCMOS/LVTTL (with a per standard / bank
        # type set): other values fail placement, removing the buffer.
        if {$k eq "IN_TERM" && ![regexp {^(DIFF_)?(SSTL|HSTL|HSUL|MOBILE_DDR)} $std]} continue
        # SLEW MEDIUM: only the SSTL/HSTL/HSUL/POD family (else an error at
        # every DRC run).
        if {$k eq "SLEW" && $v eq "MEDIUM" && ![regexp {^(DIFF_)?(SSTL|HSTL|HSUL|POD)} $std]} { set v FAST }
        if {$k eq "DRIVE"} {
            if {![regexp {^(LVCMOS|LVTTL)} $std]} continue
            set ok {4 8 12 16}
            if {[string match *HIGH_PERFORMANCE* $btype]} {
                set ok [expr {$std in {LVCMOS12 LVCMOS10} ? {2 4 6 8} : {2 4 6 8 12}}]
            } elseif {$std eq "LVCMOS12"} {
                set ok {4 8 12}
            } elseif {$std in {LVTTL LVCMOS18}} {
                set ok {4 8 12 16 24}
            }
            if {$v ni $ok} { set v [lindex $ok [expr {int(rand() * [llength $ok])}]] }
        }
        if {[catch {set_property $k $v [get_ports ${name}_p]} e]} {
            if {[catch {set_property $k $v [get_cells $name]} e]} {
                nl_log "properr $name $k $v"
            }
        }
    }
    dict set nl_bank_vcco $bank $vcco
    if {$mode ne "inout"} { dict set nl_bank_std $bank,$diffkey $sv }
    return 1
}

# Directed PIP coverage: after placement, route net <net> (one driver, one
# load) through the given PIP by fixing its route.  Registered by the
# generator with nl_want_pip and applied by nl_finish before routing.
set nl_wanted_pips [list]
proc nl_want_pip {net pip} {
    nl_flush_nets
    global nl_wanted_pips
    lappend nl_wanted_pips $net $pip
}

proc nl_force_pips {} {
    global nl_wanted_pips
    set ok 0
    set bad 0
    foreach {n p} $nl_wanted_pips {
        set net [get_nets -quiet $n]
        set pip [get_pips -quiet $p]
        if {$net eq "" || $pip eq ""} { incr bad; continue }
        if {[catch {
            set drv [get_site_pins -of_objects [get_pins -of_objects $net -filter {DIRECTION == OUT}]]
            set ld [lindex [get_site_pins -of_objects [get_pins -of_objects $net -filter {DIRECTION == IN}]] 0]
            set from [get_nodes -of_objects $drv]
            set to [get_nodes -of_objects $ld]
            set n0 [get_nodes -uphill -of_objects $pip]
            set n1 [get_nodes -downhill -of_objects $pip]
            set path [find_routing_path -quiet -from $from -to $to -include_nodes [list $n0 $n1] -sort_include_nodes -max_nodes 120]
            if {[llength $path]} {
                set_property FIXED_ROUTE $path $net
                incr ok
            } else {
                incr bad
            }
        } e]} {
            incr bad
        }
    }
    nl_log "forced pips ok $ok failed $bad"
}
