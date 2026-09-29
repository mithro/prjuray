# Dump the timing model of a part: Vivado's speed models (delays, R/C
# values), and for every tile the speed model index of its pips and wires
# (grouped per tile type into variants), for every site type the speed
# model index of its site pins and the
# timing arc models of its BELs.  Architecture independent: only generic
# Vivado objects and properties are used.
#
#   vivado -mode batch -source dump_timing.tcl -tclargs <part> <outdir> [models]
#
# Writes <outdir>/speed_models.txt (depends on the speed grade) and, unless
# "models" is given, <outdir>/tile_timing.txt and <outdir>/site_timing.txt
# (the same for every speed grade of a device).  Line formats (tab separated):
#   speed_models.txt  M  name  speed_index  type  prop=value ...
#   tile_timing.txt   T  tile_type  ntiles
#                     V  pips|wires  variant          then its P or W lines
#                     P  pip  speed_index             (pip name without tile)
#                     W  wire  speed_index            (- if the tile lacks it)
#                     A  pips|wires  tile  variant    (tile -> variant)
#   site_timing.txt   S  site_type  site
#                     I  site_pin  direction  speed_index
#                     B  bel  bel_type  model ...  (NAME_INTERNAL of each)
set part   [lindex $argv 0]
set outdir [lindex $argv 1]
set models_only [expr {[lindex $argv 2] eq "models"}]
file mkdir $outdir
link_design -part $part

# Properties that repeat the name or are constant.
set skip {CLASS NAME NAME_INTERNAL NAME_LOGICAL SPEED_INDEX TYPE}

set f [open $outdir/speed_models.txt w]
puts $f "# part $part version [version -short]"
foreach m [get_speed_models] {
    set line [list M [get_property NAME_INTERNAL $m] \
        [get_property SPEED_INDEX $m] [get_property TYPE $m]]
    foreach p [list_property $m] {
        if {$p in $skip} continue
        lappend line "$p=[get_property $p $m]"
    }
    puts $f [join $line "\t"]
}
close $f
if {$models_only} { exit 0 }

# The tile's name prefix ("CLBLM_L_X12Y49/") is dropped from pip and wire
# names so that they are per tile type.
proc strip {tile names} {
    set n [string length $tile]
    set out {}
    foreach x $names { lappend out [string range $x [expr {$n + 1}] end] }
    return $out
}

# Speed models are per tile type on 7-series, but on UltraScale+ many pips
# have instance specific models (different delays per tile).  So every tile
# is read, tiles of a type are grouped by their speed index vector, and each
# distinct vector ("variant") is written once, with a tile -> variant map.
set f [open $outdir/tile_timing.txt w]
puts $f "# part $part version [version -short]"
set tiles_of [dict create]
set alltiles [get_tiles]
foreach t $alltiles type [get_property TYPE $alltiles] {
    dict lappend tiles_of $type $t
}
foreach type [lsort [dict keys $tiles_of]] {
    set tiles [dict get $tiles_of $type]
    set t0 [lindex $tiles 0]
    puts $f [join [list T $type [llength $tiles]] "\t"]
    foreach kind {pips wires} tag {P W} {
        set names0 [strip $t0 [get_$kind -quiet -of_objects $t0]]
        if {![llength $names0]} continue
        set n0 [llength $names0]
        set first0 [lindex $names0 0]
        set last0 [lindex $names0 end]
        set variants [dict create]
        set assign {}
        foreach t $tiles {
            set objs [get_$kind -quiet -of_objects $t]
            set idx {}
            if {[llength $objs]} { set idx [get_property SPEED_INDEX $objs] }
            # Fast path (the per name strip and compare dominate the run
            # time on large dies): same count and same first and last name
            # means the same list in the same order.
            set k [expr {[string length $t] + 1}]
            if {[llength $objs] == $n0 &&
                [string range [lindex $objs 0] $k end] eq $first0 &&
                [string range [lindex $objs end] $k end] eq $last0} {
                set names $names0
            } else {
                set names [strip $t $objs]
            }
            if {$names ne $names0} {
                # Different order or set: align to the first tile's names.
                set d [dict create]
                foreach n $names i $idx { dict set d $n $i }
                set idx {}
                foreach n $names0 {
                    lappend idx [expr {[dict exists $d $n] ? [dict get $d $n] : "-"}]
                }
            }
            if {![dict exists $variants $idx]} {
                dict set variants $idx [dict size $variants]
            }
            lappend assign $t [dict get $variants $idx]
        }
        dict for {idx v} $variants {
            puts $f [join [list V $kind $v] "\t"]
            foreach n $names0 i $idx { puts $f "$tag\t$n\t$i" }
        }
        foreach {t v} $assign { puts $f [join [list A $kind $t $v] "\t"] }
    }
}
close $f

set f [open $outdir/site_timing.txt w]
puts $f "# part $part version [version -short]"
set seen [dict create]
foreach site [get_sites] {
    set st [get_property SITE_TYPE $site]
    if {[dict exists $seen $st]} continue
    dict set seen $st 1
    puts $f [join [list S $st $site] "\t"]
    set pins [get_site_pins -quiet -of_objects $site]
    if {[llength $pins]} {
        set n [string length $site]
        foreach p $pins d [get_property DIRECTION $pins] \
                i [get_property SPEED_INDEX $pins] {
            puts $f [join [list I [string range $p [expr {$n + 1}] end] $d $i] "\t"]
        }
    }
    foreach bel [get_bels -quiet -of_objects $site] {
        set line [list B [lindex [split $bel /] end] [get_property TYPE $bel]]
        foreach m [get_speed_models -quiet -of_objects $bel] {
            lappend line [get_property NAME_INTERNAL $m]
        }
        puts $f [join $line "\t"]
    }
}
close $f
