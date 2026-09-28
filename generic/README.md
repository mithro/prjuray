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
| Tile grid evidence | `python/tilegrid.py --designs ... --evidence` | `<dir>/<arch>/<die>/evidence.json` |
| Frame column alignment (all dies of an arch) | `python/colalign.py` | `<dir>/<arch>/<die>/colmap.json`, `<dir>/<arch>/model.json` |
| Tile grid | `python/tilegrid.py --evidence ... --colmap ...` | `build/db/<arch>/<die>/tilegrid.json` |
| Bit database | `python/mkdb.py` | `build/db/<arch>/segbits_<tiletype>.db`, `defaults_<tiletype>.db` |
| Checking / decoding | `python/check.py` | undocumented bit report, FASM |
| Tile grid A/B test | `tg_eval.sh <variant> <die> <tilegrid.json>` (single die DBs, `check.py`, `python/check_cmp.py`) | `build/tilegrid_exp/dbeval/{inst,<variant>}_<die>/` |
| Bit encoding analysis | `python/explain_bit.py`, `python/bit_xtab.py` (features x bit values table) | stdout |

### Random designs

Each design has random logic clusters, 1-4 random hard site types and
(mostly) random I/O.  Primitives get random parameters; `nl_finish` places,
routes and writes the bitstream, removing the cells named in errors or
disconnecting unroutable nets until it succeeds.  Hard blocks whose random
parameters or connections rarely survive this have their own recipes
(`HARD_RECIPES` in `gen_design.py`):

* MMCM/PLL: legal counter settings (VCO/PFD ranges, duty/phase grid),
  clock input from a global buffer, feedback internal or through a BUFG,
  outputs on global buffers.
* GT quads: reference clock buffers on the MGTREFCLK pads, common block
  feeding the channels, serial pads on ports (`nl_padpin`), user clocks
  from buffered TX/RXOUTCLK (BUFG_GT on UltraScale).
* Configuration primitives: a random subset of all of them per design.
* Native mode I/O: bytes of bitslices with their BITSLICE_CONTROL,
  TX_BITSLICE_TRI, RIU_OR and PLL.

`PARAM_FIXUPS` restricts random parameters of primitives with interdependent
parameters (e.g. SERDES widths) to legal combinations.
`gen_design.py --focus <regexp>` limits a design to the hard site types
matching the pattern (no random I/O), for quick recipe experiments, e.g.
`run_designs.py --die xcku025 --tag t --seeds 1:4 -- --density 0.01 --focus '^(MMCM|PLL)'`.

### Running designs

`python/run_designs.py --die <dies> --tag <tag> --seeds <a:b> --jobs <n>`
runs the seeds of the dies with `n` designs in parallel.  Options for
throughput:

* `--reuse N`: one Vivado process (`tcl/nl_server.tcl`) runs up to N
  designs of a die in turn, saving the Vivado start-up and most of the
  device load and bitgen data loading of every design (xa7s15: ~26 s of
  ~225 s per design).
* `--threads T`: Vivado `general.maxThreads` (default 2).  Vivado runs
  mostly single threaded; T=1 uses the same CPU time per design.

Memory: always start runs through `generic/vrun.sh <name> <MemoryMax>
<command...>`, which puts the job into its own transient systemd scope in
the shared `vivado.slice` with a hard memory cap (out of memory only kills
that job, never the shell it was started from) and prints the scope's
`memory.peak` on exit.  Size `--jobs` from measured peaks, not cores:
`jobs ≈ cap / (peak × 1.2)`.  Measured single job peaks (Vivado 2025.2):
xa7s15 2.8 GiB (`--reuse 4`), xc7k160t 4.8 GiB (`--reuse 2`), xcku025
7.4 GiB per design (8.3 GiB with `--reuse 2`), xcu25 9.5 GiB per design
(~90 min each).  An OOM kill (exit 143) is a retryable failure: lower
`--jobs`.

For long campaigns and several machines, `python/scheduler.py` keeps the
work items as files in a queue directory (`submit` them, start any number
of `work` runners on machines sharing the file system, `status`,
`requeue`).

Every design directory gets `run.stats` (wall and CPU seconds, status) and
`nl.log` wall clock stamps; `python/runstats.py <roots>` prints the success
rate, successful designs per core hour, the phase split and a histogram of
failure causes with the time they cost.

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

The frame row of each clock region row is learnt from the designs (a tile's
usage pattern matches the change pattern of its window).  `tilegrid.py
--designs <roots> --evidence <file>` saves this and, for every clock region
row, a score of every (grid column, frame column) pair.

The frame column ("major") of each grid column is structural
(`python/colalign.py`): frame columns follow the grid columns from left to
right, each covers a run of neighbouring grid columns, and its frame count
(from the frame address enumeration of the bitstream) is characteristic of
the tile columns it covers.  Each grid column gets a *kind* (its tile type
with sites, else the most common tile type with PIPs, among the tiles with a
bit window); the ordered kinds of a clock region row are aligned with the
ordered frame columns by dynamic programming (a monotonic HMM) scoring

* log P(frame count | kind) (Witten-Bell smoothed; kinds seen with several
  frame counts, e.g. interconnect sharing its neighbour's frame column, have
  no preference against unseen ones),
* log P(new frame column | previous kind, kind) (unseen pairs back off to the
  kinds' left / right statistics, then to the architecture's overall rate),
* the design activity score as a bonus (a tie breaker; activity a column
  shares with a more active adjacent column is explained away: tiles used
  together with their neighbour, e.g. interface tiles, show its changes),
* a cost for grid columns left out and for frame columns without grid
  columns (free up to the frame columns the void grid columns there stand
  for, e.g. feed through columns or the PS),
* vertical consistency: clock region rows with more unused frame columns
  than the die's best rows are realigned with a bonus for the frame column
  the same grid column (of the same kind) has in the best rows, whose frame
  columns also give the void allowance.

The tables are learnt by hard EM over all dies of an architecture starting
from the activity supported assignment; frame counts and transitions are
only learnt from assignments the activity does not contradict, and not for
*silent* kinds (activity below `MIN_ACT` on every die), which take part in
the alignment without a frame count preference, so that an alignment does
not reinforce its own mistakes.  The learnt table is printed (Series7: CLB
36, BRAM / DSP 28, IO 42, CMT / CLK / CFG 30, GT 32 frames; INT and
interface kinds take the count of their neighbour; UltraScale+: INT 76, CLE
16, BRAM 6, DSP 8, ...).  Silent columns left out join their nearest
neighbour's frame column; a column sharing its neighbour's frame column
takes an unused frame column right next to it on its own side when it
shows activity there or has it in every other clock region row
(UltraScale+ INT_INTF_LEFT_TERM_IO_FT between CMT_L and INT).  PS8 tiles
(UltraScale+ PSS_ALTO) own no frame column: the PS is configured through
its registers.  Hard block tiles over part of another kind's
column (e.g. PCIE) take the frame column of their neighbours in their grid
row when the other columns of their column's frame column are absent there.
Clock region rows without activity get their frame row from the frame row
order learnt on the other dies (e.g. dies with few designs).  The report
lists per die unused frame columns, rare (kind, frame count) pairs and
columns placed against strong activity (`--verbose`: each of them;
`--show <die>`: the alignment), the self-consistency check for
architectures without a reference database.

```
python3 generic/python/tilegrid.py --die <die> --designs <roots> \
    --evidence <dir>/<arch>/<die>/evidence.json --out <activity tilegrid>
python3 generic/python/colalign.py --arch <arch> --exp <dir> [--verbose] \
    [--show <die>]
python3 generic/python/tilegrid.py --die <die> \
    --evidence <dir>/<arch>/<die>/evidence.json \
    --colmap <dir>/<arch>/<die>/colmap.json --out tilegrid.json
```

The bits of a tile within its frame column (its *window*) are structural
for fabric tiles: its INT row and the empty grid rows above it (on
UltraScale(+) through an empty RCLK row: a GT quad or CMT in the bottom
INT row spans the clock region).  Hard blocks
(CMT, CFG, clock rows, GT, IO bank tiles, ...) whose windows are doubtful
(taller than one INT row, in the centre row, without an INT row, or
between site-less filler tiles such as the Series7 PCIE_BOT among
PCIE_NULL, whose bits span the clock region) get
learnt windows: `tilegrid.py --probe auto` gives these tile types a window
of +-one clock region around their grid row, a bit database of these types
built with that tile grid (`mkdb.py --types`, `URAY_DB` pointing at a probe
directory) shows the rows their features use, and `windows.py --probe-db`
turns that into per tile type windows relative to the tile's grid row;
`windows.py --merge` takes the median over the dies and `tilegrid.py
--windows` widens the structural windows with them (regions may overlap;
a bit is documented when any owner documents it).  `pipeline.py
--probe-windows` does all of this.  `check.py` reports distinct undocumented
bits (`bits N`) next to the per owner count.  `tilegrid.py --stacks RE`
makes the overlapping learnt windows of the matching types within one grid
column disjoint (each tile keeps its busiest contiguous rows); it is off by
default because the rows a tile loses leave bits its features set that no
owner documents (Series7 CMT stack: fewer undocumented bits per owner, more
distinct ones).

A learnt window never covers the structural window of another tile with
sites (of a type without a learnt window) in the same grid column.  Tile
types sharing frame columns of several sizes with other columns
(interconnect next to CLB, BRAM, DSP, CMT and I/O columns) only use the
frames of the smallest (`colalign.py` writes `<arch>/frames.json`,
`tilegrid.py --frames` caps their regions; Series7 INT: 28 frames, like
prjxray).  Tile types with sites found only in the bottom and top INT rows
of clock region rows (Series7 `*_SING` I/O tiles, whose two instances hold
the two different halves of a two row I/O tile) get the type `<type>@TOP`
for their top instances.  Bits of frame rows without any tile (e.g. clock
region rows a device does not expose) that are also set in the empty design
are reported by `check.py` as `baseline_in_tileless_rows`, not unowned.

Block RAM content (block type 1) columns map in order onto the BRAM grid
columns of the clock region row; when the row has more of them (BRAM columns
replaced by the PS) the extra ones are placed on the side of the unused
block 0 frame columns with the BRAM frame count.

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

### Caches and fast turnaround

* Feature cache: `designdata.load_features` keeps `tile_features()` of
  every design in `<build>/cache/features/<die>/<tag>/<sN>.<code>.pkl`
  (`<code>`: checksum of the feature code; stale files can be deleted any
  time, `URAY_FEATURE_CACHE=0` disables it).  Only a change of the
  feature code recomputes features; tile grid changes, `evalpred.py`,
  `tilegrid.py` evidence and scratch trees reuse them.
* Sample cache (`mkdb.py --cache`, default `<db>/<arch>/cache`): per
  design and (tile type, region) the samples, vocabulary encoded, with a
  content digest per chunk.  `--sample-jobs N` sizes its (light) worker
  pool separately from `--jobs`.
* Task reuse: `mkdb.py` records per (tile type, region) the digest of its
  inputs (the selected samples' chunks, mkdb.py, `MKDB_*` settings) in
  `<db>/<arch>/.mkdb_tasks/` and does not rerun a task whose inputs and
  database files are unchanged (a feature code change touching one tile
  type reruns that type only).
* `generic/ci.sh [-u] [-t tilegrid.json] [die...]`: regression run in a
  minute or two (xa7s15, xazu1eg, xcku025 configured): a single die
  database from fixed tags with the current code, `check.py` and
  `evalpred.py` on frozen hold-out designs, compared with the golden
  summary in `<build>/ci/golden/<die>` (database content hash, check and
  prediction totals, per tile type changes).  Exit 0: identical, 1:
  changed, 2: failed; `-u` makes the results golden.

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

`--tilegrid-only` skips the bits.  With `--arch UltraScalePlus` (or
`UltraScale`) and `--prjxray-db` pointing at a prjuray-db checkout
(`https://github.com/SymbiFlow/prjuray-db`, devices named by part, e.g.
`zynqusp/xczu3eg-sfvc784-1-e` = our die `xazu2eg`) the tile grids are
compared the same way (tile grid only).
