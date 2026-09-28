# -tclargs <dcp> <tile>...: pips used in the tile (from the nets through its nodes)
open_checkpoint [lindex $argv 0]
foreach t [lrange $argv 1 end] {
    set tile [get_tiles $t]
    set nodes [get_nodes -quiet -of_objects $tile]
    set nets [get_nets -quiet -of_objects $nodes]
    set pips [get_pips -quiet -of_objects $nets -filter "TILE == $t"]
    puts "TQ == $t nodes [llength $nodes] nets [llength $nets] pips [llength $pips]"
    foreach p [lsort [get_property NAME $pips]] { puts "TQ   $p" }
}
