# Dump the timing model of a part: Vivado's speed models (delays, R/C
# values), and for every tile type the speed model index of its pips and
# wires, for every site type the speed model index of its site pins and the
# timing arc models of its BELs.  Architecture independent: only generic
# Vivado objects and properties are used.
#
#   vivado -mode batch -source dump_timing.tcl -tclargs <part> <outdir> [models]
#
# Writes <outdir>/speed_models.txt (depends on the speed grade) and, unless
# "models" is given, <outdir>/tile_timing.txt and <outdir>/site_timing.txt
# (the same for every speed grade of a device).  Line formats (tab separated):
#   speed_models.txt  M  name  speed_index  type  prop=value ...
#   tile_timing.txt   T  tile_type  tile  [second tile]
#                     P  pip  speed_index          (pip name without tile)
#                     W  wire  speed_index
#                     X  what  name  index  index2 (second tile disagrees)
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

set f [open $outdir/tile_timing.txt w]
puts $f "# part $part version [version -short]"
# First and last tile of every type, in one pass (get_tile_types needs an
# opened design).
set first [dict create]
set last [dict create]
set alltiles [get_tiles]
foreach t $alltiles type [get_property TYPE $alltiles] {
    if {![dict exists $first $type]} { dict set first $type $t }
    dict set last $type $t
}
foreach type [lsort [dict keys $first]] {
    set t [dict get $first $type]
    set t2 [dict get $last $type]
    puts $f [join [list T $type $t $t2] "\t"]
    foreach kind {pips wires} tag {P W} {
        set objs [get_$kind -quiet -of_objects $t]
        if {![llength $objs]} continue
        set names [strip $t $objs]
        set idx [get_property SPEED_INDEX $objs]
        foreach n $names i $idx { puts $f "$tag\t$n\t$i" }
        # Cross-check against another tile of the type.
        if {$t2 ne $t} {
            set objs2 [get_$kind -quiet -of_objects $t2]
            set m2 [dict create]
            foreach n [strip $t2 $objs2] i [get_property SPEED_INDEX $objs2] {
                dict set m2 $n $i
            }
            foreach n $names i $idx {
                if {![dict exists $m2 $n]} {
                    puts $f [join [list X $kind $n $i missing] "\t"]
                } elseif {[dict get $m2 $n] ne $i} {
                    puts $f [join [list X $kind $n $i [dict get $m2 $n]] "\t"]
                }
            }
        }
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
