# For each device given, synthesise a trivial design on its first installed
# part and report whether the licence allows it (Vivado 2026.1+ checks the
# device against the licence tier at synth_design).
#   vivado -mode batch -source lic_check.tcl -tclargs <out.txt> <device>...
set out [open [lindex $argv 0] w]
set v [file join [pwd] lic_check.v]
set fv [open $v w]
puts $fv "module top(input a, input b, output o); assign o = a & b; endmodule"
close $fv
foreach dev [lrange $argv 1 end] {
    set part [lindex [lsort [get_parts -quiet ${dev}*]] 0]
    if {$part eq ""} { puts $out "$dev - notinstalled"; flush $out; continue }
    close_project -quiet
    create_project -in_memory -part $part
    read_verilog $v
    if {[catch {synth_design -top top -part $part -mode out_of_context} err]} {
        set why [expr {[string match -nocase "*licen*" $err] ? "nolicence" : "error"}]
        puts $out "$dev $part $why [string map {"\n" " "} [string range $err 0 200]]"
    } else {
        puts $out "$dev $part ok"
    }
    flush $out
}
close $out
