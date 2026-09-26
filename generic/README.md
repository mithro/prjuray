# Generic bitstream documentation flow

This directory holds an architecture independent flow that documents the
configuration bits of Xilinx 7-series, UltraScale and UltraScale+ devices
using Vivado 2025.2 (any part available in the WebPACK edition).

Unlike the hand written fuzzers in `fuzzers/`, nothing here is specific to a
tile type: random designs are generated for every die, Vivado reports which
*features* each design uses, and the features are correlated with the bits of
the bitstream.

## Flow

| Step | Tool | Output |
| ---- | ---- | ------ |
| Device metadata | `tcl/dump_tiles.tcl`, `tcl/dump_types.tcl`, `tcl/dump_prims.tcl`, `tcl/baseline.tcl` | `build/meta/` |
| Die grouping (devices sharing one tile grid) | `python/die_groups.py` | `build/meta/die_groups.json` |
| Random designs | `python/gen_design.py` + `tcl/netlist.tcl`, run by `python/run_designs.py` | `build/designs/<die>/<tag>/s<seed>/{bits.npz,design.features.gz,nl.log}` |
| Tile grid | `python/tilegrid.py` | `build/db/<arch>/<die>/tilegrid.json` |
| Bit database | `python/mkdb.py` | `build/db/<arch>/segbits_<tiletype>.db`, `defaults_<tiletype>.db` |
| Checking / decoding | `python/check.py` | undocumented bit report, FASM |

### Features

`tcl/dump_features.tcl` dumps, for a routed design, every used PIP, every used
site PIP (routing BEL input selection) and the *physical* configuration of
every used BEL (Vivado's `CONFIG.*` BEL properties, i.e. the values bitgen
uses, including hidden attributes).  `python/features.py` turns them into
per-tile feature names:

```
<wire0>-><wire1>                   routing PIP
<wire0><-><wire1>                  bidirectional PIP
<SITE>.TYPE.<site type>            site in use with this site type
<SITE>.<BEL>.SP.<from>.<to>        routing BEL input selection
<SITE>.<BEL>.<CFG>=<value>         enumerated BEL configuration
<SITE>.<BEL>.<CFG>[i]              bit i of a vector BEL configuration is 1
<SITE>.<BEL>.<CFG>[i]=0            bit i is 0 (vectors up to 64 bits; many
                                   attributes are stored inverted)
<SITE>.<BEL>.INIT[i]               LUT truth table bit (from the LUT equation)
```

`<SITE>` is the site prefix plus its coordinates relative to the lowest site
of the same prefix in the tile (e.g. `SLICE_X1Y0`).

### Tile grid

Frame addresses follow the architecture (7-series: block/half/row/column/
minor, UltraScale(+): block/row/column/minor).  Within a clock region row, a
tile anchored at INT row `r` (counted from the bottom of the clock region)
starts at bit `r * bits_per_row` (+ the centre HCLK/RCLK bits for the upper
half); tiles in the HCLK/RCLK row own the centre bits:

| arch | rows/CR | bits/row | centre bits | words/frame |
| ---- | ------- | -------- | ----------- | ----------- |
| Series7 | 50 | 64 | 32 | 101 |
| UltraScale | 60 | 64 | 96 | 123 |
| UltraScalePlus | 60 | 48 | 96 | 93 |

The frame row of each clock region row and the frame column of each grid
column are learnt from the designs (a tile's usage pattern matches the change
pattern of its window), with a monotonic assignment per row.  Block RAM
content (block type 1) columns map in order onto the BRAM grid columns.

`tilegrid.json` maps each tile to a list of regions:
`{"block", "half", "row", "col", "base" (frame address of minor 0),
"frames", "offset" (first bit in the frame), "nbits"}`.

### Bit database

Bits are named `<frame>_<bit>`: frame = minor within the region's frame
column, bit = offset from the region's first bit.  For region `k > 0` of a
tile the file name gets a `.k` suffix.

* `segbits_<type>.db`: `<TYPE>.<feature> <bit> ... !<bit> ...` - bits the
  feature sets, and (`!`) default bits it clears.
* `defaults_<type>.db`: bits set in every unused instance of the tile type.

A set bit is *documented* when it is a default bit of its tile or a bit of a
feature whose bits all match.  ECC bits (7-series word 50 bits 0-12,
UltraScale words 60/61, UltraScale+ words 45/46) are frame ECC, recomputed
from the frame contents, and not part of any tile.

## Configuration registers and bitstream options

Outside the configuration frames a bitstream is a sequence of type 1/2
packets writing configuration registers (`python/bitstream.py` parses them).
`tcl/regfuzz.tcl` writes many bitstreams of one design with random
`BITSTREAM.*` option values and `python/regdb.py` correlates the options with
the register values:

* `registers.db`: `<option>=<value> <REG>#<n>:<bit> ...` - register bits
  (register name, n-th write of that register in the bitstream) set by an
  option value.
* `registers.txt`: the register write sequence with the bits that are fixed.

CMD (command codes), FAR (frame address), IDCODE, CRC and FDRI/MFWR (frame
data) are defined by their meaning.  Options that change configuration frame
bits (unused pin pulls, configuration pin pulls, ...) are found by
`python/optionbits.py` and stored as features of the tiles owning those bits
in `segbits_opt_<tile type>.db` (`<TYPE>.BITSTREAM.<option>=<value>`).

`python/check.py --registers` checks register writes as well as frames.

## Cross-check against prjxray-db (Series7)

`python/xcheck_prjxray.py` compares the Series7 results with a prjxray-db
checkout (e.g. the openXC7 fork `https://github.com/openXC7/prjxray-db`, the
revision pinned by openXC7's toolchain-nix / toolchain-installer):

```
python3 generic/python/xcheck_prjxray.py --prjxray-db <prjxray-db> \
    --out build/xcheck/prjxray.md [--build build] [--db build/db/Series7]
```

For every prjxray device whose die (`python/dies.py`) has a tile grid it
compares each tile's CLB_IO_CLK / BLOCK_RAM `baseaddr/frames/offset/words`
with our region (offset and size x 32 bits), listing mismatches per tile type,
regions missing in ours and grid columns / clock region rows whose frame
address differs.  It then maps the prjxray `segbits_<type>[.block_ram].db`
bits onto our region coordinates and lists prjxray bits used by no feature or
default of ours (candidate undocumented bits), our bits unknown to prjxray, and
feature correspondences (`INT_L.EE2BEG0.LOGIC_OUTS_L0` =
`INT_L.LOGIC_OUTS_L0->EE2BEG0`, or identical set bits).
