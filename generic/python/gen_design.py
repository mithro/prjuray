#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Random fuzzing design generator.

Emits a Vivado Tcl script (using generic/tcl/netlist.tcl) that builds a random
netlist directly, LOCs cells onto random sites of the die, then places, routes,
dumps features and writes a per-frame-CRC bitstream.

Each "recipe" instantiates the primitives for one kind of site with random
parameters; recipes are architecture aware through the primitive library dump.
"""
import argparse
import collections
import os
import random
import re

import prims as primlib

URAY_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
NETLIST_TCL = os.path.join(URAY_DIR, 'generic', 'tcl', 'netlist.tcl')

SITE_XY = re.compile(r'_X(\d+)Y(\d+)$')


class Die:
    def __init__(self, tiles_tsv):
        self.sites = {}  # site -> (site_type, tile, tile_type, gx, gy)
        self.by_type = collections.defaultdict(list)
        self.tile_pos = {}
        self.tile_cr = {}
        self.part = None
        self.arch = None
        for line in open(tiles_tsv):
            p = line.split()
            if p[0] == 'part':
                self.part, self.device, self.arch = p[1], p[2], p[3]
                continue
            if p[0] != 'tile':
                continue
            if p[6] == '-':
                self.tile_pos[p[1]] = (p[2], int(p[3]), int(p[4]))
                continue
            tile, ttype, gx, gy = p[1], p[2], int(p[3]), int(p[4])
            self.tile_pos[tile] = (ttype, gx, gy)
            self.tile_cr[tile] = p[5]
            for s in p[6].split(','):
                name, stype = s.split(':')
                self.sites[name] = (stype, tile, ttype, gx, gy)
                self.by_type[stype].append(name)


# Parameter values that are listed as legal but break Vivado.
BAD_VALUES = {('CE_TYPE', 'HARDSYNC')}


def _fix_iserdese2(p, rng):
    """Legal ISERDESE2 mode / rate / width combinations (UG471): others
    fail bitgen and end up at the defaults, so the width bits never vary."""
    it = p.get('INTERFACE_TYPE', 'MEMORY')
    if it == 'NETWORKING':
        p['DATA_RATE'] = rng.choice(['SDR', 'DDR'])
        p['DATA_WIDTH'] = rng.choice(['2', '3', '4', '5', '6', '7', '8']
                                     if p['DATA_RATE'] == 'SDR' else
                                     ['4', '6', '8'])
    else:
        p['DATA_RATE'] = 'DDR'
        p['DATA_WIDTH'] = rng.choice(['4', '8']) \
            if it == 'MEMORY_DDR3' else '4'
    # Width expansion (SLAVE) and OFB need dedicated neighbour connections.
    p['SERDES_MODE'] = 'MASTER'
    p['OFB_USED'] = 'FALSE'


def _fix_oserdese2(p, rng):
    """Legal OSERDESE2 rate / width / tristate combinations (UG471)."""
    p['DATA_RATE_OQ'] = rng.choice(['SDR', 'DDR'])
    if p['DATA_RATE_OQ'] == 'SDR':
        p['DATA_WIDTH'] = rng.choice(['2', '3', '4', '5', '6', '7', '8'])
    else:
        p['DATA_WIDTH'] = rng.choice(['4', '6', '8'])
    if p['DATA_RATE_OQ'] == 'DDR' and p['DATA_WIDTH'] == '4' and \
            rng.random() < 0.5:
        p['DATA_RATE_TQ'], p['TRISTATE_WIDTH'] = 'DDR', '4'
    else:
        p['DATA_RATE_TQ'] = rng.choice(['SDR', 'BUF'])
        p['TRISTATE_WIDTH'] = '1'
    p['SERDES_MODE'] = 'MASTER'
    if rng.random() < 0.8:
        p['TBYTE_CTL'] = p['TBYTE_SRC'] = 'FALSE'


# Per primitive adjustment of random parameters to legal combinations.
PARAM_FIXUPS = {
    'ISERDESE2': _fix_iserdese2,
    'OSERDESE2': _fix_oserdese2,
}


class Design:
    def __init__(self, die, prims, rng):
        self.die = die
        self.prims = prims
        self.rng = rng
        self.lines = []
        self.ncell = 0
        self.sources = []  # (pin, gx, gy)
        self.sinks = []  # (pin, gx, gy, kind)
        self.fixed_nets = []  # (driver pin, [sink pins])
        self.clocks = []  # clock net driver pins
        self.gclocks = []  # fabric driven global buffer outputs
        self.pblocks = []  # (range, cells)
        self.pblock_used = set()
        self.lines_post = []
        self.ties = []  # (pin, 0 | 1) fixed constant inputs
        self._tile_sites = None
        self.gt_quads = set()
        self.gt_bufs = []  # (BUFG_GT, driver) of the current GT quad
        self.config_done = False
        self.nclkbuf = 0
        self.native_used = set()
        self.sites_used = set()  # hard sites taken by recipes

    def take_clock_buffer(self):
        # Account one more recipe clock buffer; False once there are as
        # many as the clock tracks of a clock region can carry (else the
        # global clock router fails, after a long time).
        if self.nclkbuf >= (16 if is_us(self.die) else 10):
            return False
        self.nclkbuf += 1
        return True

    def die_arch(self):
        return {'kintexu': 'UltraScale', 'kintexuplus': 'UltraScalePlus',
                'zynquplus': 'UltraScalePlus',
                'artixuplus': 'UltraScalePlus'}.get(self.die.arch, 'Series7')

    def name(self, prefix='c'):
        self.ncell += 1
        return f'{prefix}{self.ncell}'

    def random_params(self, ref, overrides=None, skip=()):
        p = self.prims[ref]
        out = {}
        for k, (default, values) in p.params.items():
            if k.startswith('SIM_') or k in skip:
                continue
            values = [v for v in values if (k, v) not in BAD_VALUES]
            v = primlib.random_value(self.rng, default, values)
            if v is not None:
                out[k] = v
        if ref in PARAM_FIXUPS:
            PARAM_FIXUPS[ref](out, self.rng)
        if overrides:
            out.update(overrides)
        return out

    def cell(self, ref, site=None, bel=None, props=None, prefix='c'):
        name = self.name(prefix)
        props = props or {}
        plist = ' '.join(f'{k} {{{v}}}' for k, v in props.items())
        self.lines.append(
            f'nl_cell {name} {ref} {{{plist}}} {{{site or ""}}} {{{bel or ""}}}'
        )
        return name

    def loc_of(self, site):
        s = self.die.sites[site]
        return s[3], s[4]

    def add_source(self, pin, site):
        gx, gy = self.loc_of(site)
        self.sources.append((pin, gx, gy))

    def add_sink(self, pin, site, kind='data', hard=False):
        """pin may be a list of pins that must share a single driver.
        kind: 'data', 'clock', 'const' (tied to 0 or 1) or 'const0'.
        hard: pin of a hard block (not tied to constants)."""
        gx, gy = self.loc_of(site)
        self.sinks.append((pin, gx, gy, kind if not hard else kind + '_hard'))

    def connect(self, driver, sinks):
        self.fixed_nets.append((driver, list(sinks)))

    def tie(self, pin, value):
        self.ties.append((pin, value))

    def padpin(self, port, pin, site, func, direction):
        """Top level port on the package pin of <site> whose function
        matches the regexp <func>, connected to cell pin <pin> (the cell
        is removed when the site has no such bonded pin)."""
        self.lines.append(f'nl_padpin {port} {pin} {site} {{{func}}} '
                          f'{direction}')

    def tile_sites(self, site):
        """Sites sharing the tile of <site>."""
        if self._tile_sites is None:
            self._tile_sites = collections.defaultdict(list)
            for s, v in self.die.sites.items():
                self._tile_sites[v[1]].append(s)
        return self._tile_sites[self.die.sites[site][1]]

    def emit(self, path):
        rng = self.rng
        # Bucket sources spatially so most connections are local.
        grid = collections.defaultdict(list)
        for pin, gx, gy in self.sources:
            grid[(gx // 16, gy // 16)].append(pin)
        allsrc = [s[0] for s in self.sources]
        drive = collections.defaultdict(list)
        const0, const1 = [], []
        # Clock budget: loads spread over too many clocks exceed the clock
        # tracks of a clock region (12 / 24) or the 6 clocks of half an I/O
        # bank.  Fabric clock loads use at most 8 clocks, I/O logic clock
        # loads at most 4; unused pad clocks become data sources.
        clocks = list(self.clocks)
        if len(clocks) > 8:
            keep = set(rng.sample(clocks, 8))
            for c in clocks:
                if c not in keep and c not in self.gclocks:
                    allsrc.append(c)
            clocks = [c for c in clocks if c in keep]
        ioclocks = rng.sample(clocks, min(4, len(clocks)))
        for pin, gx, gy, kind in self.sinks:
            pins = pin if isinstance(pin, list) else [pin]
            hard = kind.endswith('_hard')
            kind = kind.replace('_hard', '')
            if kind == 'gclock' and (self.gclocks or clocks):
                drive[rng.choice(self.gclocks or clocks)].extend(pins)
                continue
            if kind == 'ioclock' and ioclocks:
                drive[rng.choice(ioclocks)].extend(pins)
                continue
            if kind in ('clock', 'gclock', 'ioclock') and clocks:
                drive[rng.choice(clocks)].extend(pins)
                continue
            if kind == 'ioclock':
                kind = 'data'
            kind = 'data' if kind == 'gclock' else kind
            if kind in ('const', 'const0'):
                (const0 if kind == 'const0' or rng.random() < 0.5
                 else const1).extend(pins)
                continue
            r = rng.random()
            if kind == 'fabric' and allsrc:
                # Pins constants cannot reach: always a fabric driver.
                r = 1.0
            if hard and (r < 0.16 or not allsrc):
                # Hard block inputs are left unconnected rather than tied:
                # many cannot be reached from the constant tie-offs.
                continue
            if r < 0.08 or not allsrc:
                const0.extend(pins)
            elif r < 0.16:
                const1.extend(pins)
            else:
                local = grid.get((gx // 16, gy // 16))
                if local and rng.random() < 0.85:
                    drive[rng.choice(local)].extend(pins)
                else:
                    drive[rng.choice(allsrc)].extend(pins)
        for d, s in self.fixed_nets:
            drive[d].extend(s)
        for pin, v in self.ties:
            (const1 if v else const0).append(pin)
        with open(path, 'w') as f:
            f.write(f'source {NETLIST_TCL}\n')
            f.write(f'nl_init {self.die.part}\n')
            for l in self.lines:
                f.write(l + '\n')
            for l in self.lines_post:
                f.write(l + '\n')
            for i, (d, s) in enumerate(drive.items()):
                f.write(f'nl_net n{i} {{{d} {" ".join(s)}}}\n')
            for i in range(0, len(const0), 500):
                f.write(f'nl_conn nl_const0 {{{" ".join(const0[i:i+500])}}}\n')
            for i in range(0, len(const1), 500):
                f.write(f'nl_conn nl_const1 {{{" ".join(const1[i:i+500])}}}\n')
            for i, (rng_, cells) in enumerate(self.pblocks):
                f.write(f'nl_pblock pb{i} {{{rng_}}} {{{" ".join(cells)}}}\n')
            f.write('catch {set_property BITSTREAM.CONFIG.UNUSEDPIN '
                    f'{rng.choice(["Pulldown", "Pullup", "Pullnone"])} '
                    '[current_design]}\n')
            f.write('nl_internal_vref 0.5\n')
            f.write(f'nl_finish {int(rng.random() < 0.5)}\n')


def is_us(die):
    return die.arch in ('kintexu', 'kintexuplus', 'zynquplus', 'virtexu',
                        'virtexuplus', 'artixuplus')


CLOCK_PINS = {'C', 'CLK', 'G', 'WCLK', 'CLKARDCLK', 'CLKBWRCLK'}


def pins_of(prims, ref):
    return prims[ref].pins


def add_generic_pins(d, ref, name, site, skip=()):
    """Registers all input pins as sinks and outputs as sources."""
    for direction, pin in pins_of(d.prims, ref):
        if pin in skip:
            continue
        full = f'{name}/{pin}'
        if direction == 'IN':
            d.add_sink(full, site, 'clock' if pin in CLOCK_PINS else 'data')
        elif direction == 'OUT':
            d.add_source(full, site)


def recipe_slice(d, site, slicem):
    """Random logic cluster constrained (by pblock) around one SLICE.

    Cells are not LOCed individually: Vivado's placer takes care of the many
    intra-slice legality rules, the pblock just spreads the logic over the
    whole device."""
    rng = d.rng
    us = is_us(d.die)
    nletters = 8 if us else 4
    cells = []

    def cell(ref, props=None):
        n = d.cell(ref, None, None, props)
        cells.append(n)
        return n

    lut_out = []
    ctl = dict(cinv=rng.random() < 0.3, srl_clk=[], srl_ce=[])
    for l in range(rng.randint(1, nletters)):
        mode = rng.random()
        if slicem and mode < 0.15:
            ref = rng.choice(['SRL16E', 'SRLC32E'])
            props = d.random_params(ref)
            props['IS_CLK_INVERTED'] = "1'b1" if ctl['cinv'] else "1'b0"
            n = cell(ref, props)
            add_generic_pins(d, ref, n, site, skip=('CLK', 'CE'))
            ctl['srl_clk'].append(f'{n}/CLK')
            ctl['srl_ce'].append(f'{n}/CE')
            continue
        if mode < 0.45:
            # Two LUTs sharing inputs so they can pack into one LUT6 (O5/O6).
            k5 = rng.randint(1, 5)
            k6 = rng.randint(1, 5)
            n5 = cell(f'LUT{k5}', d.random_params(f'LUT{k5}'))
            n6 = cell(f'LUT{k6}', d.random_params(f'LUT{k6}'))
            for i in range(max(k5, k6)):
                grp = []
                if i < k5:
                    grp.append(f'{n5}/I{i}')
                if i < k6:
                    grp.append(f'{n6}/I{i}')
                d.add_sink(grp, site)
            d.add_source(f'{n5}/O', site)
            d.add_source(f'{n6}/O', site)
            continue
        k = rng.randint(1, 6)
        n = cell(f'LUT{k}', d.random_params(f'LUT{k}'))
        add_generic_pins(d, f'LUT{k}', n, site, skip=('O', ))
        lut_out.append(f'{n}/O')
    # Wide muxes (MUXF7/F8[/F9]) fed by LUT6s.
    rng.shuffle(lut_out)
    while len(lut_out) >= 2 and rng.random() < 0.4:
        a, b = lut_out.pop(), lut_out.pop()
        m = cell('MUXF7')
        d.connect(a, [f'{m}/I0'])
        d.connect(b, [f'{m}/I1'])
        d.add_sink(f'{m}/S', site)
        if lut_out and len(lut_out) >= 2 and rng.random() < 0.4:
            c, e = lut_out.pop(), lut_out.pop()
            m2 = cell('MUXF7')
            d.connect(c, [f'{m2}/I0'])
            d.connect(e, [f'{m2}/I1'])
            d.add_sink(f'{m2}/S', site)
            m8 = cell('MUXF8')
            d.connect(f'{m}/O', [f'{m8}/I0'])
            d.connect(f'{m2}/O', [f'{m8}/I1'])
            d.add_sink(f'{m8}/S', site)
            d.add_source(f'{m8}/O', site)
        else:
            d.add_source(f'{m}/O', site)
    for o in lut_out:
        d.add_source(o, site)
    # Carry chain (1-3 elements), S inputs driven by dedicated LUTs.  CI can
    # only come from the previous element's last CO (or a constant): a CI
    # driven from fabric makes Vivado insert CARRY/GND cells it cannot place.
    # CARRY4 CI and CYINIT are alternatives: the first element takes CYINIT
    # from fabric with CI = 0, later ones CI from the chain with CYINIT = 0.
    # CARRY8 (no CYINIT) may take its first CI from fabric (AX).
    if rng.random() < 0.3:
        ref = 'CARRY8' if us else 'CARRY4'
        last_co = 'CO[7]' if us else 'CO[3]'
        prev = None
        nchain = rng.choice((1, 1, 2, 3))
        # 7-series: the last CO and the last O share the D output mux of the
        # slice; using both makes Vivado insert a pass-through CARRY4 (with
        # GND/VCC cells) above the chain, often unplaceable in the pblock.
        last_o = 'O[7]' if us else 'O[3]'
        co_out = us or rng.random() < 0.5
        for i in range(nchain):
            props = d.random_params(ref) if us else {}
            n = cell(ref, props)
            for direction, pin in pins_of(d.prims, ref):
                full = f'{n}/{pin}'
                if direction == 'IN':
                    if pin == 'CI':
                        if prev:
                            d.connect(f'{prev}/{last_co}', [full])
                        else:
                            d.add_sink(full, site, 'data' if us else 'const0')
                        continue
                    if pin == 'CI_TOP' or (pin == 'CYINIT' and prev):
                        d.add_sink(full, site, 'const0')
                        continue
                    if pin.startswith('S[') and rng.random() < 0.7:
                        k = rng.randint(1, 6)
                        l = cell(f'LUT{k}', d.random_params(f'LUT{k}'))
                        add_generic_pins(d, f'LUT{k}', l, site, skip=('O', ))
                        d.connect(f'{l}/O', [full])
                        continue
                    d.add_sink(full, site)
                elif pin == last_co:
                    # Kept for the next element of the chain only.
                    continue
                elif pin == last_o and i == nchain - 1 and co_out and not us:
                    continue
                else:
                    d.add_source(full, site)
            prev = n
        if co_out:
            d.add_source(f'{prev}/{last_co}', site)
    # Flip flops sharing a control set.
    nff = rng.randint(0, 2 * nletters)
    kind = rng.random()
    if kind < 0.45:
        choices = ['FDRE', 'FDSE']
    elif kind < 0.9:
        choices = ['FDCE', 'FDPE']
    else:
        choices = ['LDCE', 'LDPE']
    cinv = "1'b1" if ctl['cinv'] else "1'b0"
    # Set/reset inversion: shared by the control set like the clock one
    # (FFs of a site share the SR pin and its inverter).  UltraScale only:
    # 7-series slices have no SR inverter (Vivado then cannot commit the
    # placement: "failed to commit all instances").
    srinv = "1'b1" if us and rng.random() < 0.3 else "1'b0"
    clk, ce, sr = [], [], []
    for i in range(nff):
        ref = rng.choice(choices)
        props = {'INIT': rng.choice(["1'b0", "1'b1"])}
        if ref[0] == 'F':
            props['IS_C_INVERTED'] = cinv
        else:
            props['IS_G_INVERTED'] = cinv
        for p in ('IS_R_INVERTED', 'IS_S_INVERTED', 'IS_CLR_INVERTED',
                  'IS_PRE_INVERTED'):
            if p in d.prims[ref].params:
                props[p] = srinv
        n = cell(ref, props)
        for direction, pin in pins_of(d.prims, ref):
            full = f'{n}/{pin}'
            if direction == 'OUT':
                d.add_source(full, site)
            elif pin in ('C', 'G'):
                clk.append(full)
            elif pin in ('CE', 'GE'):
                ce.append(full)
            elif pin in ('R', 'S', 'CLR', 'PRE'):
                sr.append(full)
            else:
                d.add_sink(full, site)
    clk += ctl['srl_clk']
    ce += ctl['srl_ce']
    for grp, kind in ((clk, 'clock'), (ce, 'data'), (sr, 'data')):
        if grp:
            d.add_sink(grp, site, kind)
    # Constrain the cluster to a small region around the anchor site; regions
    # never overlap (overlapping pblocks over-subscribe slices).
    m = SITE_XY.search(site)
    x, y = int(m.group(1)), int(m.group(2))
    rx = 1 if slicem else 2
    area = {(i, j) for i in range(max(0, x - rx), x + rx + 1)
            for j in range(max(0, y - 2), y + 3)}
    if area & d.pblock_used:
        return
    d.pblock_used |= area
    d.pblocks.append((f'SLICE_X{max(0, x - rx)}Y{max(0, y - 2)}:'
                      f'SLICE_X{x + rx}Y{y + 2}', cells))


def recipe_bufg(d, site):
    ref = 'BUFGCE' if is_us(d.die) else 'BUFG'
    if ref not in d.prims:
        ref = 'BUFGCTRL'
    # Not LOCed: the placer resolves global clock resource conflicts.
    n = d.cell(ref, None, None, d.random_params(ref))
    for direction, pin in pins_of(d.prims, ref):
        full = f'{n}/{pin}'
        if direction == 'IN':
            d.add_sink(full, site)
        elif direction == 'OUT':
            d.clocks.append(full)
            d.gclocks.append(full)


# Site type -> candidate primitives, per architecture.  Sites not listed
# here are handled by dedicated recipes (slices, IO) or have no primitive.
# (BUFCE_ROW / BUFCE_LEAF cannot be instantiated: Vivado rejects them; they
# are configured by the clock router.)
_CFG_US = ['STARTUPE3', 'ICAPE3', 'BSCANE2', 'DNA_PORTE2', 'USR_ACCESSE2',
           'EFUSE_USR', 'FRAME_ECCE3', 'DCIRESET', 'MASTER_JTAG']
SITE_REFS = {
    'Series7': {
        'RAMB18E1': ['RAMB18E1', 'FIFO18E1'],
        'RAMBFIFO36E1': ['RAMB36E1', 'FIFO36E1'],
        'DSP48E1': ['DSP48E1'],
        'IDELAYE2': ['IDELAYE2'],
        'IDELAYE2_FINEDELAY': ['IDELAYE2_FINEDELAY'],
        'ODELAYE2': ['ODELAYE2'],
        'ILOGICE3': ['ISERDESE2', 'IDDR', 'IDDR_2CLK'],
        'ILOGICE2': ['ISERDESE2', 'IDDR', 'IDDR_2CLK'],
        'OLOGICE3': ['OSERDESE2', 'ODDR'],
        'OLOGICE2': ['OSERDESE2', 'ODDR'],
        'BUFGCTRL': ['BUFGCTRL', 'BUFGMUX', 'BUFGCE', 'BUFG'],
        'BUFHCE': ['BUFHCE', 'BUFH'],
        'BUFR': ['BUFR'],
        'BUFIO': ['BUFIO'],
        'BUFMRCE': ['BUFMRCE', 'BUFMR'],
        'MMCME2_ADV': ['MMCME2_ADV'],
        'PLLE2_ADV': ['PLLE2_ADV'],
        'IN_FIFO': ['IN_FIFO'],
        'OUT_FIFO': ['OUT_FIFO'],
        'PHASER_IN_PHY': ['PHASER_IN_PHY', 'PHASER_IN'],
        'PHASER_OUT_PHY': ['PHASER_OUT_PHY', 'PHASER_OUT'],
        'PHASER_REF': ['PHASER_REF'],
        'PHY_CONTROL': ['PHY_CONTROL'],
        'IDELAYCTRL': ['IDELAYCTRL'],
        'GTPE2_CHANNEL': ['GTPE2_CHANNEL'],
        'GTPE2_COMMON': ['GTPE2_COMMON'],
        'GTXE2_CHANNEL': ['GTXE2_CHANNEL'],
        'GTXE2_COMMON': ['GTXE2_COMMON'],
        'IBUFDS_GTE2': ['IBUFDS_GTE2'],
        'PCIE_2_1': ['PCIE_2_1'],
        'XADC': ['XADC'],
        'PS7': ['PS7'],
        'STARTUP': ['STARTUPE2'],
        'BSCAN': ['BSCANE2'],
        'ICAP': ['ICAPE2'],
        'CAPTURE': ['CAPTUREE2'],
        'DNA_PORT': ['DNA_PORT'],
        'EFUSE_USR': ['EFUSE_USR'],
        'FRAME_ECC': ['FRAME_ECCE2'],
        'USR_ACCESS': ['USR_ACCESSE2'],
        'DCIRESET': ['DCIRESET'],
    },
    'UltraScale': {
        'RAMBFIFO36': ['RAMB36E2', 'FIFO36E2'],
        'RAMBFIFO18': ['RAMB18E2', 'FIFO18E2'],
        'RAMB181': ['RAMB18E2'],
        'DSP48E2': ['DSP48E2'],
        'BITSLICE_RX_TX': ['RXTX_BITSLICE', 'RX_BITSLICE', 'TX_BITSLICE',
                           'IDELAYE3', 'ODELAYE3', 'ISERDESE3', 'OSERDESE3'],
        'BITSLICE_TX': ['TX_BITSLICE_TRI'],
        'BITSLICE_CONTROL': ['BITSLICE_CONTROL'],
        'RIU_OR': ['RIU_OR'],
        'BUFGCE': ['BUFGCE'],
        'BUFGCE_DIV': ['BUFGCE_DIV'],
        'BUFGCTRL': ['BUFGCTRL'],
        'BUFG_GT': ['BUFG_GT'],
        'BUFG_GT_SYNC': ['BUFG_GT_SYNC'],
        'MMCME3_ADV': ['MMCME3_ADV'],
        'PLLE3_ADV': ['PLLE3_ADV'],
        'GTHE3_CHANNEL': ['GTHE3_CHANNEL'],
        'GTHE3_COMMON': ['GTHE3_COMMON'],
        'PCIE_3_1': ['PCIE_3_1'],
        'SYSMONE1': ['SYSMONE1'],
        'HPIO_VREF_SITE': ['HPIO_VREF'],
        'CONFIG_SITE': _CFG_US,
        'HARD_SYNC': ['HARD_SYNC'],
    },
    'UltraScalePlus': {
        'RAMBFIFO36': ['RAMB36E2', 'FIFO36E2'],
        'RAMBFIFO18': ['RAMB18E2', 'FIFO18E2'],
        'RAMB181': ['RAMB18E2'],
        'DSP48E2': ['DSP48E2'],
        'URAM288': ['URAM288', 'URAM288_BASE'],
        'BITSLICE_RX_TX': ['RXTX_BITSLICE', 'RX_BITSLICE', 'TX_BITSLICE',
                           'IDELAYE3', 'ODELAYE3', 'ISERDESE3', 'OSERDESE3'],
        'BITSLICE_TX': ['TX_BITSLICE_TRI'],
        'BITSLICE_CONTROL': ['BITSLICE_CONTROL'],
        'RIU_OR': ['RIU_OR'],
        'BUFGCE': ['BUFGCE'],
        'BUFGCE_DIV': ['BUFGCE_DIV'],
        'BUFGCTRL': ['BUFGCTRL'],
        'BUFG_GT': ['BUFG_GT'],
        'BUFG_GT_SYNC': ['BUFG_GT_SYNC'],
        'BUFG_PS': ['BUFG_PS'],
        'MMCM': ['MMCME4_ADV'],
        'PLL': ['PLLE4_ADV'],
        'GTHE4_CHANNEL': ['GTHE4_CHANNEL'],
        'GTHE4_COMMON': ['GTHE4_COMMON'],
        'GTYE4_CHANNEL': ['GTYE4_CHANNEL'],
        'GTYE4_COMMON': ['GTYE4_COMMON'],
        'PCIE40E4': ['PCIE40E4'],
        'PCIE4CE4': ['PCIE4CE4'],
        'CMACE4': ['CMACE4'],
        'SYSMONE4': ['SYSMONE4'],
        'PS8': ['PS8'],
        'VCU': ['VCU'],
        'HPIO_VREF_SITE': ['HPIO_VREF'],
        'CONFIG_SITE': ['STARTUPE3', 'ICAPE3', 'BSCANE2', 'DNA_PORTE2',
                        'USR_ACCESSE2', 'EFUSE_USR', 'FRAME_ECCE4',
                        'DCIRESET', 'MASTER_JTAG'],
        'HARD_SYNC': ['HARD_SYNC'],
    },
}

DIRECT_CLOCKS = re.compile(r'(CLK|^C$|^G$)')
# Clock buffers: data inputs must come from the clock network.
CLOCK_BUFFERS = re.compile(r'^(BUFH|BUFHCE|BUFCE_ROW|BUFCE_LEAF|BUFGCE_DIV|'
                           r'BUFGCTRL|BUFGMUX|BUFGCE|BUFG)$')
# Dedicated cascade connections can only go to a neighbouring block.
CASCADE_PINS = re.compile(
    r'(CAS|^(AC|BC|PC)(IN|OUT)|^CARRYCASC|^MULTSIGN|^(A|B|P)CIN|^(A|B|P)COUT)')


# Pins that may only connect to I/O buffers or neighbouring blocks.
DEDICATED = {
    'IDDR': {'D'}, 'IDDR_2CLK': {'D'}, 'IDDRE1': {'D'},
    'ISERDESE2': {'D', 'DDLY', 'OFB', 'SHIFTIN1', 'SHIFTIN2', 'SHIFTOUT1',
                  'SHIFTOUT2'},
    'ISERDESE3': {'D'},
    'OSERDESE2': {'SHIFTIN1', 'SHIFTIN2', 'SHIFTOUT1', 'SHIFTOUT2',
                  'TBYTEIN', 'TBYTEOUT', 'OQ', 'TQ', 'OFB', 'TFB'},
    'OSERDESE3': {'OQ', 'T_OUT'},
    'ODDR': {'Q'}, 'ODDRE1': {'Q'},
    'IDELAYE2': {'IDATAIN'}, 'IDELAYE2_FINEDELAY': {'IDATAIN'},
    'IDELAYE3': {'IDATAIN', 'CASC_IN', 'CASC_OUT', 'CASC_RETURN'},
    'ODELAYE2': {'ODATAIN', 'DATAOUT'}, 'ODELAYE2_FINEDELAY': {'ODATAIN',
                                                             'DATAOUT'},
    'ODELAYE3': {'ODATAIN', 'DATAOUT', 'CASC_IN', 'CASC_OUT',
                 'CASC_RETURN'},
    'BUFIO': {'I'}, 'BUFR': {'I'}, 'BUFMR': {'I'}, 'BUFMRCE': {'I'},
    'RX_BITSLICE': {'DATAIN'}, 'TX_BITSLICE': {'O', 'T_OUT'},
    'RXTX_BITSLICE': {'DATAIN', 'O', 'T_OUT'},
    'IBUFDS_GTE2': {'I', 'IB'}, 'IBUFDS_GTE3': {'I', 'IB'},
    'IBUFDS_GTE4': {'I', 'IB'},
    # Configuration pads.
    'STARTUPE3': {'CCLK', 'DONE', 'FCS_B'} |
                 {f'{p}[{i}]' for p in ('DATA_IN', 'DATA_OUT')
                  for i in range(4)},
    # Analog inputs (dedicated pads / analog I/O pins only).
    'XADC': {'VP', 'VN'} | {f'VAUX{s}[{i}]' for s in 'PN' for i in range(16)},
    'SYSMONE1': {'VP', 'VN'} |
                {f'VAUX{s}[{i}]' for s in 'PN' for i in range(16)},
    'HPIO_VREF': {'VREF'},
    'SYSMONE4': {'VP', 'VN'} |
                {f'VAUX{s}[{i}]' for s in 'PN' for i in range(16)},
}


# MMCM/PLL operating ranges: (VCO MHz, PFD MHz, CLKIN MHz), slowest grade.
CLOCKGEN_RANGES = {
    'MMCME2_ADV': ((600, 1200), (10, 450), (10, 800)),
    'PLLE2_ADV': ((800, 1600), (19, 450), (19, 800)),
    'MMCME3_ADV': ((600, 1200), (10, 450), (10, 800)),
    'PLLE3_ADV': ((600, 1335), (70, 667), (70, 800)),
    'MMCME4_ADV': ((800, 1600), (10, 450), (10, 800)),
    'PLLE4_ADV': ((750, 1500), (70, 667), (70, 800)),
}


def _prange(params, name, lo, hi):
    """Integer range of a parameter from the primitive library."""
    vals = params.get(name, (None, []))[1]
    if len(vals) == 1 and ' to ' in vals[0]:
        a, b = vals[0].split(' to ')
        try:
            return max(lo, int(float(a))), min(hi, int(float(b)))
        except ValueError:
            pass
    return lo, hi


def _fmt(x):
    return f'{x:.3f}'


# Pins (by regexp) that only connect to package pads or other hard blocks.
DEDICATED_RE = {
    'PS8': re.compile(r'_PAD_'),
    # 7-series memory interface blocks: clocks and PHY buses only reach
    # the SERDES / FIFOs / PHY_CONTROL of the byte group (unroutable from
    # or to the fabric: "PHASER_IN_PHY.ICLK -> SLICEL.B5").
    'PHASER_IN_PHY': re.compile(
        r'^(ICLK|ICLKDIV|RCLK|ISERDESRST|WRENABLE|BURSTPENDINGPHY|'
        r'RANKSELPHY|ENCALIBPHY|FREQREFCLK|MEMREFCLK|PHASEREFCLK|SYNCIN)'),
    'PHASER_OUT_PHY': re.compile(
        r'^(OCLK|OSERDESRST|RDENABLE|CTSBUS|DQSBUS|DTSBUS|'
        r'BURSTPENDINGPHY|ENCALIBPHY|FREQREFCLK|MEMREFCLK|PHASEREFCLK|'
        r'SYNCIN)'),
    'PHY_CONTROL': re.compile(
        r'^(INBURSTPENDING|INRANK|OUTBURSTPENDING|PCENABLECALIB|MEMREFCLK|'
        r'SYNCIN|AUXOUTPUT|PHYCTLMSTREMPTY)'),
}
DEDICATED_RE['PHASER_IN'] = DEDICATED_RE['PHASER_IN_PHY']
DEDICATED_RE['PHASER_OUT'] = DEDICATED_RE['PHASER_OUT_PHY']
# Clock outputs of hard blocks that must drive a given clock buffer.
CLOCK_OUT_BUFFERS = {
    'PS8': (re.compile(r'^(PLCLK\[\d\]|DP(AUDIO|VIDEO)REFCLK|FMIO\w*TOPLBUFG|'
                       r'OSCRTCCLK)$'), 'BUFG_PS'),
}


def clockgen_params(ref, params, rng):
    """Legal MMCM/PLL parameters: VCO/PFD/input frequency within range,
    duty cycles and phases on the counter grid (1/8 VCO period, at least
    one VCO cycle high and low, 6 bit high/low/delay counters), and all fine
    phase shift counters with the same fractional phase.

    Every counter setting still varies over the whole legal range: the
    input period is chosen to match the random multiply/divide values."""
    props = {}
    mmcm = ref.startswith('MMCM')
    vco_r, pfd_r, fin_r = CLOCKGEN_RANGES.get(
        ref, ((600, 1200), (10, 450), (10, 800)))
    mname = 'CLKFBOUT_MULT_F' if mmcm else 'CLKFBOUT_MULT'
    mlo, mhi = _prange(params, mname, 2, 64)
    dlo, dhi = _prange(params, 'DIVCLK_DIVIDE', 1, 106)
    # Input period parameter(s).
    pnames = [k for k in ('CLKIN1_PERIOD', 'CLKIN2_PERIOD', 'CLKIN_PERIOD')
              if k in params]
    pmax = 100.0
    for k in pnames:
        pmax = min(pmax, float(params[k][1][0].split(' to ')[1])
                   if params[k][1] and ' to ' in params[k][1][0] else 100.0)
    fin_lo = max(fin_r[0], 1000.0 / pmax)
    best = None
    for _ in range(100):
        m = rng.randint(mlo, mhi)
        dv = rng.randint(dlo, dhi)
        vco = rng.uniform(*vco_r)
        fin = vco * dv / m
        pfd = fin / dv
        if fin_lo <= fin <= fin_r[1] and pfd_r[0] <= pfd <= pfd_r[1]:
            best = (m, dv, fin)
            break
    if best is None:
        m, dv = rng.randint(mlo, min(mhi, 16)), 1
        best = (m, dv, max(fin_lo, min(fin_r[1], vco_r[0] / m)))
    m, dv, fin = best
    frac_fb = mmcm and m < mhi and rng.random() < 0.25
    mval = m + (rng.randint(1, 7) * 0.125 if frac_fb else 0)
    props[mname] = _fmt(mval) if mmcm else str(m)
    props['DIVCLK_DIVIDE'] = str(dv)
    period = 1000.0 / fin
    for k in pnames:
        props[k] = _fmt(min(period if k != 'CLKIN2_PERIOD' else
                            rng.uniform(1000.0 / fin_r[1], pmax), pmax))
    frac = frac_fb
    fine = {}
    for k in params:
        mm = re.match(r'^(CLKOUT\d|CLKFBOUT)_USE_FINE_PS$', k)
        if mm:
            fine[mm.group(1)] = rng.random() < 0.3
    outs = sorted({re.match(r'^(CLKOUT\d)_', k).group(1) for k in params
                   if re.match(r'^CLKOUT\d_DIVIDE', k)})
    frac_residue = rng.randint(0, 7)
    for o in outs:
        olo, ohi = _prange(params, f'{o}_DIVIDE_F' if f'{o}_DIVIDE_F' in
                           params else f'{o}_DIVIDE', 1, 128)
        div = rng.randint(olo, ohi)
        ofrac = False
        if f'{o}_DIVIDE_F' in params:
            if 2 <= div < ohi and rng.random() < 0.25:
                ofrac = True
                frac = True
                props[f'{o}_DIVIDE_F'] = _fmt(div + rng.randint(1, 7) * 0.125)
            else:
                props[f'{o}_DIVIDE_F'] = _fmt(div)
        else:
            props[f'{o}_DIVIDE'] = str(div)
        # Duty cycle: m/2 VCO cycles high, (2*div-m)/2 low, 1..64 cycles.
        if div == 1 or ofrac:
            duty = 0.5
        else:
            lo = max(2, 2 * div - 128)
            hi = min(2 * div - 2, 128)
            duty = rng.randint(lo, hi) / (2 * div) if lo <= hi else 0.5
        props[f'{o}_DUTY_CYCLE'] = _fmt(duty)
        # Phase: k/8 VCO cycles (at most 63 cycles, within +-360 degrees).
        if ofrac:
            k = 0
        else:
            kmax = min(8 * div - 1, 8 * 63)
            k = rng.randint(0, kmax)
            if fine.get(o):
                k -= (k - frac_residue) % 8
                k = max(k, 0) if k >= 0 else frac_residue
        ph = k * 45.0 / div
        if ph > 0 and rng.random() < 0.2:
            ph -= 360.0
        props[f'{o}_PHASE'] = _fmt(ph)
    # Feedback phase (on the multiplier grid).
    if 'CLKFBOUT_PHASE' in params:
        if frac_fb:
            ph = 0.0
        else:
            k = rng.randint(0, min(8 * m - 1, 8 * 63))
            if fine.get('CLKFBOUT'):
                k -= (k - frac_residue) % 8
                k = max(k, frac_residue)
            ph = k * 45.0 / m
        props['CLKFBOUT_PHASE'] = _fmt(ph)
    for o, f in fine.items():
        props[f'{o}_USE_FINE_PS'] = 'TRUE' if f and not frac else 'FALSE'
    return props


def global_buffer(d, site, driver, export=True):
    """A global clock buffer (placed by Vivado) on <driver>; with export its
    output becomes a clock of the design.  (Regional BUFH buffers conflict
    with the slice pblocks.)  None once the design has its maximum number
    of clock buffers."""
    if not d.take_clock_buffer():
        return None
    ref = 'BUFGCE' if is_us(d.die) else 'BUFG'
    if ref not in d.prims:
        ref = 'BUFG'
    props = d.random_params(ref) if ref == 'BUFGCE' else {}
    b = d.cell(ref, None, None, props)
    d.connect(driver, [f'{b}/I'])
    if ref == 'BUFGCE':
        d.add_sink(f'{b}/CE', site)
    if export:
        d.clocks.append(f'{b}/O')
    return b


def recipe_clockgen(d, site, ref):
    """MMCM/PLL with legal counter settings, clocked from a fabric driven
    global clock buffer (no cascades: those need dedicated placement),
    feedback either internal or through a global buffer, and a few of its
    clock outputs driving global buffers (fabric loads directly on a CLKOUT
    pin would be constrained to its clock region; too many buffers exceed
    the buffers / clock tracks of the clock region)."""
    rng = d.rng
    if site in d.sites_used:
        return
    d.sites_used.add(site)
    params = d.prims[ref].params
    pll = ref.startswith('PLL')
    props = d.random_params(ref)
    props.update(clockgen_params(ref, params, rng))
    if props.get('SS_EN') == 'TRUE' and rng.random() < 0.6:
        props['SS_EN'] = 'FALSE'
    fbbuf = rng.random() < (0.15 if pll else 0.3)
    if 'COMPENSATION' in params and rng.random() < 0.8:
        # Compensation matching the feedback path; else random.
        props['COMPENSATION'] = 'INTERNAL' if not fbbuf else rng.choice(
            [v for v in params['COMPENSATION'][1]
             if v in ('ZHOLD', 'BUF_IN', 'AUTO')] or ['INTERNAL'])
    n = d.cell(ref, site, None, props)
    pins = {p: dr for dr, p in pins_of(d.prims, ref)}
    b = fbbuf and global_buffer(d, site, f'{n}/CLKFBOUT', export=False)
    if b:
        d.connect(f'{b}/O', [f'{n}/CLKFBIN'])
    else:
        d.connect(f'{n}/CLKFBOUT', [f'{n}/CLKFBIN'])
    skip = {'CLKFBOUT', 'CLKFBIN', 'CLKOUTPHY'}
    for p in ('CLKIN1', 'CLKIN2', 'CLKIN'):
        if p in pins and (p != 'CLKIN2' or rng.random() < 0.5):
            d.add_sink(f'{n}/{p}', site, 'gclock', hard=True)
        skip.add(p)
    # UltraScale PLLs drive global buffers from CLKOUT0/1 only.
    outs = [p for p in pins if re.match(
        r'^CLKOUT\d+$' if pll or is_us(d.die) else r'^CLKOUT\d+B?$', p)]
    for p in rng.sample(outs, min(len(outs), rng.choice([0, 1, 1, 2, 3]))):
        global_buffer(d, site, f'{n}/{p}')
    skip |= {p for p in pins if re.match(r'^CLK(OUT\d+B?|FBOUTB)$', p)}
    for p, dr in sorted(pins.items()):
        if p in skip:
            continue
        full = f'{n}/{p}'
        if dr == 'OUT':
            if rng.random() < 0.6:
                d.add_source(full, site)
        elif rng.random() < 0.7:
            d.add_sink(full, site, 'clock' if p in ('DCLK', 'PSCLK')
                       else 'data', hard=True)


def pin_mapped(ref, props, pin):
    """False for logical pins without a physical pin in the configuration
    given by props (their nets are unroutable: "Pin mapping failure, cannot
    reach driver pin").  UltraScale FIFOs: the upper data outputs exist
    only at the widest read width, the counters have fewer bits for wider
    ports."""
    m = re.match(r'^(DOUTP?|RDCOUNT|WRCOUNT)\[(\d+)\]$', pin)
    if not m or ref not in ('FIFO18E2', 'FIFO36E2'):
        return True
    wide = 36 if ref == 'FIFO18E2' else 72
    i = int(m.group(2))
    if m.group(1).startswith('DOUT'):
        if props.get('READ_WIDTH', '4') == str(wide):
            return True
        return i < {('DOUT', 36): 16, ('DOUTP', 36): 2,
                    ('DOUT', 72): 32, ('DOUTP', 72): 4}[(m.group(1), wide)]
    w = int(props.get('READ_WIDTH' if m.group(1) == 'RDCOUNT'
                      else 'WRITE_WIDTH', '4'))
    widths = [4, 9, 18, 36, 72] if wide == 72 else [4, 9, 18, 36]
    nbits = 9 + len(widths) - 1 - widths.index(w) if w in widths else 9
    return i < nbits


def recipe_hard(d, site, ref, pconn=0.6):
    """One primitive LOCed onto a hard site, random parameters, random
    connections (clock-like pins preferably to global clocks)."""
    rng = d.rng
    if ref not in d.prims:
        return
    if ref in HARD_RECIPES:
        HARD_RECIPES[ref](d, site, ref)
        return
    props = d.random_params(ref)
    n = d.cell(ref, site, None, props)
    npins = len(pins_of(d.prims, ref))
    if npins > 500:
        # Huge blocks (PS, PCIe, CMAC, VCU): thousands of random nets make
        # placement / routing take longer than the time budget.
        pconn = max(0.1, min(pconn, 300.0 / npins))
    clkbuf = CLOCK_OUT_BUFFERS.get(ref)
    shared = set()
    if props.get('CLOCK_DOMAINS') == 'COMMON' or props.get('EN_SYN') == 'TRUE':
        pins = {p for _, p in pins_of(d.prims, ref)}
        for pair in (('CLKARDCLK', 'CLKBWRCLK'), ('RDCLK', 'WRCLK')):
            if set(pair) <= pins:
                shared |= set(pair)
                d.add_sink([f'{n}/{p}' for p in pair], site, 'clock')
    for direction, pin in pins_of(d.prims, ref):
        full = f'{n}/{pin}'
        if CASCADE_PINS.search(pin) or pin in DEDICATED.get(ref, ()) or \
                pin in shared or (ref in DEDICATED_RE and
                                  DEDICATED_RE[ref].search(pin)) or \
                not pin_mapped(ref, props, pin):
            continue
        if direction == 'IN':
            # (7-series IN/OUT_FIFO clocks left open are tied to VCC by
            # Vivado with VCC cells it cannot place: always clocked.)
            if rng.random() < pconn or CLOCK_BUFFERS.match(ref) or (
                    ref in ('IN_FIFO', 'OUT_FIFO') and
                    pin in ('RDCLK', 'WRCLK')):
                kind = 'clock' if (DIRECT_CLOCKS.search(pin) or (
                    CLOCK_BUFFERS.match(ref) and pin in ('I', 'I0', 'I1'))) \
                    else 'data'
                if kind == 'data' and ref.startswith('DSP48') and \
                        rng.random() < 0.3:
                    # DSP inputs have their own VCC/GND tie-offs (the
                    # DSP_VCC / DSP_GND site pips).
                    d.tie(full, int(rng.random() < 0.7))
                    continue
                d.add_sink(full, site, kind, hard=True)
        elif direction == 'OUT':
            if clkbuf and clkbuf[0].search(pin):
                # Clock outputs that only reach a dedicated buffer.
                if clkbuf[1] in d.prims and rng.random() < 0.5:
                    b = d.cell(clkbuf[1])
                    d.connect(full, [f'{b}/I'])
                    d.clocks.append(f'{b}/O')
            elif rng.random() < pconn:
                d.add_source(full, site)


_SITE_IDX = re.compile(r'^(.*)_X(\d+)Y(\d+)$')


def _gt_quad(d, site):
    """(common site, [channel sites], [IBUFDS_GTE2 sites]) of the GT quad
    holding <site> (a GT channel/common, IBUFDS_GTE2 or BUFG_GT(_SYNC))."""
    stype = d.die.sites[site][0]
    if stype.startswith('BUFG_GT'):
        # UltraScale: the quad in the same tile.
        for s in d.tile_sites(site):
            if d.die.sites[s][0].endswith('_COMMON'):
                return _gt_quad(d, s)
        return None
    _, x, y = _SITE_IDX.match(site).groups()
    x, y = int(x), int(y)
    m = re.match(r'^(GT[A-Z]E\d)_(CHANNEL|COMMON)$', stype)
    if m:
        fam = m.group(1)
        q = y // 4 if m.group(2) == 'CHANNEL' else y
    elif stype == 'IBUFDS_GTE2':
        q = y // 2
        fam = next((f for f in ('GTXE2', 'GTPE2', 'GTHE2')
                    if f + '_COMMON' in d.die.by_type), None)
        if fam is None:
            return None
    else:
        return None
    com = f'{fam}_COMMON_X{x}Y{q}'
    chans = [f'{fam}_CHANNEL_X{x}Y{4 * q + i}' for i in range(4)]
    refs = [f'IBUFDS_GTE2_X{x}Y{2 * q + i}' for i in range(2)]
    return (com if com in d.die.sites else None,
            [c for c in chans if c in d.die.sites],
            [r for r in refs if r in d.die.sites])


_GT_PAD = re.compile(r'^GT[A-Z]?(RX|TX)([PN])$')
_GT_REFCLK = re.compile(r'^GTREFCLK\d\d?$')
_GT_PLLIN = re.compile(r'^(Q?PLL\d?)(REF)?CLK$')
_GT_PLLOUT = re.compile(r'^(Q?PLL\d?)OUT(REF)?CLK$')
_GT_CLKIN = re.compile(r'CLK\d?$')
# (RXRECCLK*SEL: common outputs without a physical pin.)
_GT_SKIP = re.compile(r'^(GT(NORTH|SOUTH|EAST|WEST|G)REFCLK|.*RSVD|RXRECCLK\d_?SEL)')


def gt_clock_buffer(d, site, driver):
    """Clock buffer on a GT clock output: BUFG_GT on UltraScale (CE / CLR
    shared by all BUFG_GTs of the quad, see gt_quad_buffers), BUFG on
    7-series."""
    rng = d.rng
    if 'BUFG_GT' not in d.prims:
        return global_buffer(d, site, driver)
    if not d.take_clock_buffer():
        return None
    b = d.cell('BUFG_GT', None, None, d.random_params('BUFG_GT'))
    d.connect(driver, [f'{b}/I'])
    for p in ('CEMASK', 'CLRMASK', 'DIV[0]', 'DIV[1]', 'DIV[2]'):
        d.tie(f'{b}/{p}', int(rng.random() < 0.3))
    d.gt_bufs.append((b, driver))
    d.clocks.append(f'{b}/O')
    return b


def gt_quad_buffers(d, site):
    """CE / CLR of the BUFG_GTs of a quad: the BUFG_GTs of one GT clock
    output share them (Vivado Opt 31-214/215) through their BUFG_GT_SYNC
    (as the transceiver IP does; otherwise Vivado inserts one it often
    cannot place), or directly from the fabric (constants cannot reach
    them)."""
    bufs, d.gt_bufs = d.gt_bufs, []
    groups = collections.defaultdict(list)
    for b, drv in bufs:
        groups[drv].append(b)
    for drv, bl in groups.items():
        if 'BUFG_GT_SYNC' in d.prims and d.rng.random() < 0.8:
            sy = d.cell('BUFG_GT_SYNC')
            d.connect(drv, [f'{sy}/CLK'])
            d.add_sink(f'{sy}/CE', site, 'fabric')
            d.add_sink(f'{sy}/CLR', site, 'fabric')
            d.connect(f'{sy}/CESYNC', [f'{b}/CE' for b in bl])
            d.connect(f'{sy}/CLRSYNC', [f'{b}/CLR' for b in bl])
        else:
            d.add_sink([f'{b}/CE' for b in bl], site, 'fabric')
            d.add_sink([f'{b}/CLR' for b in bl], site, 'fabric')


def gt_block_pins(d, site, n, ref, refclks, common, chsite=None):
    """Connections of a GT common/channel: dedicated clock inputs from the
    reference clock buffers / common block only, serial pads on ports,
    user clocks from the buffered TX/RXOUTCLK, the rest random."""
    rng = d.rng
    usrclk = {}
    for dr, p in pins_of(d.prims, ref):
        full = f'{n}/{p}'
        m = _GT_PAD.match(p)
        if m:
            if chsite:
                # 7-series: Vivado inserts I/O buffers on GT pad ports (that
                # then fail placement): only check the pads are bonded.
                d.padpin(f'{n}_{p.lower()}', full, chsite,
                         f'^MGT[A-Z]*{m.group(1)}{m.group(2)}\\d',
                         ('IN' if dr == 'IN' else 'OUT') if is_us(d.die)
                         else 'CHECK')
            continue
        if _GT_SKIP.match(p):
            continue
        if dr == 'IN' and _GT_REFCLK.match(p):
            if refclks and rng.random() < 0.8:
                d.connect(rng.choice(refclks), [full])
            continue
        m = _GT_PLLIN.match(p)
        if dr == 'IN' and m:
            if common and rng.random() < 0.8:
                d.connect(f'{common}/{m.group(1)}OUT{m.group(2) or ""}CLK',
                          [full])
            continue
        if dr == 'OUT' and _GT_PLLOUT.match(p):
            continue
        if dr == 'OUT' and p in ('TXOUTCLK', 'RXOUTCLK'):
            if rng.random() < 0.8:
                usrclk[p[:2]] = gt_clock_buffer(d, site, full)
            continue
        if dr == 'IN' and re.match(r'^(TX|RX)USRCLK2?$', p):
            b = usrclk.get(p[:2])
            if b is not None and rng.random() < 0.8:
                d.connect(f'{b}/O', [full])
            elif rng.random() < 0.7:
                d.add_sink(full, site, 'gclock', hard=True)
            continue
        if dr == 'IN':
            if rng.random() < 0.6:
                d.add_sink(full, site, 'gclock' if _GT_CLKIN.search(p)
                           else 'data', hard=True)
        elif dr == 'OUT' and rng.random() < 0.5:
            d.add_source(full, site)


def recipe_gt(d, site, ref):
    """A GT quad around <site>: reference clock buffers on the MGTREFCLK
    pads, the common block feeding the channels, channels with their serial
    pads on ports and their user clocks from buffered TX/RXOUTCLK."""
    rng = d.rng
    quad = _gt_quad(d, site)
    if quad is None:
        return
    com, chans, refsites = quad
    key = (com, tuple(chans))
    if key in d.gt_quads:
        return
    d.gt_quads.add(key)
    stype = d.die.sites[site][0]
    us = is_us(d.die)
    refclks = []
    ibuf = next((r for r in ('IBUFDS_GTE4', 'IBUFDS_GTE3', 'IBUFDS_GTE2')
                 if r in d.prims), None)
    nref = rng.choice([1, 2] if stype == 'IBUFDS_GTE2' else [0, 1, 1, 2])
    for i in (rng.sample(range(2), nref) if ibuf else ()):
        if us:
            if com is None:
                break
            loc, psite, func = None, com, f'^MGTREFCLK{i}'
        else:
            if i >= len(refsites):
                continue
            loc = psite = refsites[i]
            func = '^MGTREFCLK\\d'
        b = d.cell(ibuf, loc, None, d.random_params(ibuf))
        d.padpin(f'{b}_p', f'{b}/I', psite, func + 'P', 'IN')
        d.padpin(f'{b}_n', f'{b}/IB', psite, func + 'N', 'IN')
        if rng.random() < 0.6:
            d.add_sink(f'{b}/CEB', site, hard=True)
        else:
            d.tie(f'{b}/CEB', 0)
        refclks.append(f'{b}/O')
        if rng.random() < 0.5:
            gt_clock_buffer(d, site, f'{b}/ODIV2')
    cn = None
    if com is not None and (stype.endswith('_COMMON') or rng.random() < 0.7):
        comref = d.die.sites[com][0]
        if comref in d.prims:
            cn = d.cell(comref, com, None, d.random_params(comref))
            gt_block_pins(d, site, cn, comref, refclks, None)
    if stype.endswith('_CHANNEL'):
        sel = [site] + [c for c in chans if c != site and rng.random() < 0.4]
    else:
        sel = [c for c in chans if rng.random() < 0.5]
        if not sel and cn is None and chans:
            sel = [rng.choice(chans)]
    for ch in sel:
        chref = d.die.sites[ch][0]
        if chref in d.prims:
            n = d.cell(chref, ch, None, d.random_params(chref))
            gt_block_pins(d, site, n, chref, refclks, cn, ch)
    gt_quad_buffers(d, site)


# Configuration primitives (one site each on 7-series, BELs of the single
# CONFIG_SITE on UltraScale): (primitive, maximum number of instances).
CONFIG_PRIMS = {
    'Series7': [('STARTUPE2', 1), ('BSCANE2', 4), ('ICAPE2', 2),
                ('CAPTUREE2', 1), ('DNA_PORT', 1), ('EFUSE_USR', 1),
                ('FRAME_ECCE2', 1), ('USR_ACCESSE2', 1), ('DCIRESET', 1)],
    'UltraScale': [('STARTUPE3', 1), ('BSCANE2', 4), ('ICAPE3', 2),
                   ('DNA_PORTE2', 1), ('EFUSE_USR', 1), ('FRAME_ECCE3', 1),
                   ('USR_ACCESSE2', 1), ('DCIRESET', 1),
                   ('MASTER_JTAG', 1)],
    'UltraScalePlus': [('STARTUPE3', 1), ('BSCANE2', 4), ('ICAPE3', 2),
                       ('DNA_PORTE2', 1), ('EFUSE_USR', 1),
                       ('FRAME_ECCE4', 1), ('USR_ACCESSE2', 1),
                       ('DCIRESET', 1), ('MASTER_JTAG', 1)],
}


def recipe_config(d, site, ref):
    """A random subset of all configuration primitives (placed by Vivado:
    they share one site on UltraScale), at most once per design."""
    rng = d.rng
    if d.config_done:
        return
    d.config_done = True
    arch = d.die_arch()
    for pref, nmax in CONFIG_PRIMS.get(arch, []):
        if pref not in d.prims or (pref != ref and rng.random() < 0.4):
            continue
        k = rng.randint(1, nmax)
        chains = rng.sample(['1', '2', '3', '4'], k)
        for i in range(k):
            props = d.random_params(pref)
            if pref == 'BSCANE2':
                props['JTAG_CHAIN'] = chains[i]
            n = d.cell(pref, None, None, props)
            for dr, p in pins_of(d.prims, pref):
                full = f'{n}/{p}'
                if p in DEDICATED.get(pref, ()):
                    continue
                if dr == 'IN' and rng.random() < 0.7:
                    d.add_sink(full, site, 'clock' if DIRECT_CLOCKS.search(p)
                               else 'data', hard=True)
                elif dr == 'OUT' and rng.random() < 0.7:
                    d.add_source(full, site)


HARD_RECIPES = {
    'MMCME2_ADV': recipe_clockgen, 'PLLE2_ADV': recipe_clockgen,
    'MMCME3_ADV': recipe_clockgen, 'PLLE3_ADV': recipe_clockgen,
    'MMCME4_ADV': recipe_clockgen, 'PLLE4_ADV': recipe_clockgen,
    'GTPE2_CHANNEL': recipe_gt, 'GTPE2_COMMON': recipe_gt,
    'GTXE2_CHANNEL': recipe_gt, 'GTXE2_COMMON': recipe_gt,
    'GTHE3_CHANNEL': recipe_gt, 'GTHE3_COMMON': recipe_gt,
    'GTHE4_CHANNEL': recipe_gt, 'GTHE4_COMMON': recipe_gt,
    'GTYE4_CHANNEL': recipe_gt, 'GTYE4_COMMON': recipe_gt,
    'IBUFDS_GTE2': recipe_gt, 'BUFG_GT': recipe_gt,
    'BUFG_GT_SYNC': recipe_gt,
}
for _a in CONFIG_PRIMS.values():
    for _r, _ in _a:
        HARD_RECIPES[_r] = recipe_config


# I/O standards (STANDARD:VCCO) by pad site type family.
_S7_HR_SE = ['LVCMOS33:3.3', 'LVTTL:3.3', 'PCI33_3:3.3', 'LVCMOS25:2.5',
             'LVCMOS18:1.8', 'LVCMOS15:1.5', 'LVCMOS12:1.2', 'SSTL135:1.35',
             'SSTL135_R:1.35', 'SSTL15:1.5', 'SSTL15_R:1.5', 'SSTL18_I:1.8',
             'SSTL18_II:1.8', 'HSTL_I:1.5', 'HSTL_II:1.5', 'HSTL_I_18:1.8',
             'HSTL_II_18:1.8', 'HSUL_12:1.2', 'MOBILE_DDR:1.8']
_S7_HR_DIFF = ['LVDS_25:2.5', 'TMDS_33:3.3', 'MINI_LVDS_25:2.5',
               'BLVDS_25:2.5', 'RSDS_25:2.5', 'PPDS_25:2.5',
               'DIFF_SSTL15:1.5', 'DIFF_SSTL15_R:1.5', 'DIFF_SSTL135:1.35',
               'DIFF_SSTL135_R:1.35', 'DIFF_SSTL18_I:1.8',
               'DIFF_SSTL18_II:1.8', 'DIFF_HSTL_I:1.5', 'DIFF_HSTL_II:1.5',
               'DIFF_HSTL_I_18:1.8', 'DIFF_HSTL_II_18:1.8',
               'DIFF_HSUL_12:1.2', 'DIFF_MOBILE_DDR:1.8']
_S7_HP_SE = ['LVCMOS18:1.8', 'LVCMOS15:1.5', 'LVCMOS12:1.2', 'LVDCI_18:1.8',
             'LVDCI_15:1.5', 'LVDCI_DV2_18:1.8', 'LVDCI_DV2_15:1.5',
             'HSLVDCI_18:1.8', 'HSLVDCI_15:1.5', 'SSTL18_I:1.8',
             'SSTL18_II:1.8', 'SSTL18_I_DCI:1.8', 'SSTL18_II_DCI:1.8',
             'SSTL18_II_T_DCI:1.8', 'SSTL15:1.5', 'SSTL15_DCI:1.5',
             'SSTL15_T_DCI:1.5', 'SSTL135:1.35', 'SSTL135_DCI:1.35',
             'SSTL135_T_DCI:1.35', 'SSTL12:1.2', 'SSTL12_DCI:1.2',
             'SSTL12_T_DCI:1.2', 'HSTL_I:1.5', 'HSTL_II:1.5',
             'HSTL_I_18:1.8', 'HSTL_II_18:1.8', 'HSTL_I_DCI:1.5',
             'HSTL_II_DCI:1.5', 'HSTL_II_T_DCI:1.5', 'HSTL_I_DCI_18:1.8',
             'HSTL_II_DCI_18:1.8', 'HSTL_II_T_DCI_18:1.8', 'HSUL_12:1.2',
             'HSUL_12_DCI:1.2']
_S7_HP_DIFF = ['LVDS:1.8', 'DIFF_SSTL18_I:1.8', 'DIFF_SSTL18_II:1.8',
               'DIFF_SSTL18_I_DCI:1.8', 'DIFF_SSTL18_II_T_DCI:1.8',
               'DIFF_SSTL15:1.5', 'DIFF_SSTL15_DCI:1.5',
               'DIFF_SSTL15_T_DCI:1.5', 'DIFF_SSTL135:1.35',
               'DIFF_SSTL135_T_DCI:1.35', 'DIFF_SSTL12:1.2',
               'DIFF_SSTL12_T_DCI:1.2', 'DIFF_HSTL_I:1.5',
               'DIFF_HSTL_II:1.5', 'DIFF_HSTL_I_18:1.8',
               'DIFF_HSTL_II_T_DCI:1.5', 'DIFF_HSUL_12:1.2',
               'DIFF_HSUL_12_DCI:1.2']
_US_HP_SE = ['LVCMOS18:1.8', 'LVCMOS15:1.5', 'LVCMOS12:1.2', 'LVDCI_18:1.8',
             'LVDCI_15:1.5', 'HSLVDCI_18:1.8', 'HSLVDCI_15:1.5',
             'SSTL18_I:1.8', 'SSTL18_I_DCI:1.8', 'SSTL15:1.5',
             'SSTL15_DCI:1.5', 'SSTL135:1.35', 'SSTL135_DCI:1.35',
             'SSTL12:1.2', 'SSTL12_DCI:1.2', 'POD12:1.2', 'POD12_DCI:1.2',
             'POD10:1.0', 'POD10_DCI:1.0', 'HSTL_I:1.5', 'HSTL_I_DCI:1.5',
             'HSTL_I_18:1.8', 'HSTL_I_DCI_18:1.8', 'HSTL_I_12:1.2',
             'HSTL_I_DCI_12:1.2', 'HSUL_12:1.2', 'HSUL_12_DCI:1.2',
             'LVCMOS10:1.0']
_US_HP_DIFF = ['LVDS:1.8', 'SUB_LVDS:1.8', 'SLVS_400_18:1.8', 'LVDS_25:1.8',
               'DIFF_SSTL18_I:1.8', 'DIFF_SSTL15:1.5', 'DIFF_SSTL135:1.35',
               'DIFF_SSTL12:1.2', 'DIFF_SSTL12_DCI:1.2', 'DIFF_POD12:1.2',
               'DIFF_POD12_DCI:1.2', 'DIFF_POD10:1.0', 'DIFF_HSTL_I:1.5',
               'DIFF_HSTL_I_18:1.8', 'DIFF_HSTL_I_12:1.2', 'DIFF_HSUL_12:1.2',
               'MIPI_DPHY_DCI:1.2']
_US_HR_SE = _S7_HR_SE
_US_HR_DIFF = ['LVDS_25:2.5', 'TMDS_33:3.3', 'MINI_LVDS_25:2.5',
               'BLVDS_25:2.5', 'RSDS_25:2.5', 'PPDS_25:2.5', 'SUB_LVDS:2.5',
               'SLVS_400_25:2.5', 'DIFF_SSTL15:1.5', 'DIFF_SSTL135:1.35',
               'DIFF_SSTL18_I:1.8', 'DIFF_SSTL18_II:1.8', 'DIFF_SSTL12:1.2',
               'DIFF_HSTL_I:1.5', 'DIFF_HSTL_I_18:1.8', 'DIFF_HSUL_12:1.2']
_HD_SE = ['LVCMOS33:3.3', 'LVTTL:3.3', 'LVCMOS25:2.5', 'LVCMOS18:1.8',
          'LVCMOS15:1.5', 'LVCMOS12:1.2', 'SSTL18_I:1.8', 'SSTL18_II:1.8',
          'SSTL15:1.5', 'SSTL15_II:1.5', 'SSTL135:1.35', 'SSTL135_II:1.35',
          'SSTL12:1.2', 'HSTL_I:1.5', 'HSTL_I_18:1.8', 'HSUL_12:1.2']
_HD_DIFF = ['LVDS_25:2.5', 'SUB_LVDS:2.5', 'SLVS_400_25:2.5',
            'DIFF_SSTL18_I:1.8', 'DIFF_SSTL18_II:1.8', 'DIFF_SSTL15:1.5',
            'DIFF_SSTL15_II:1.5', 'DIFF_SSTL135:1.35', 'DIFF_SSTL12:1.2',
            'DIFF_HSTL_I:1.5', 'DIFF_HSTL_I_18:1.8', 'DIFF_HSUL_12:1.2',
            'TMDS_33:3.3', 'MINI_LVDS_25:2.5', 'RSDS_25:2.5', 'PPDS_25:2.5',
            'BLVDS_25:2.5', 'LVPECL:2.5']
IO_SITES = {
    'IOB33': (_S7_HR_SE, _S7_HR_DIFF),
    'IOB33M': (_S7_HR_SE, _S7_HR_DIFF),
    'IOB33S': (_S7_HR_SE, _S7_HR_DIFF),
    'IOB18': (_S7_HP_SE, _S7_HP_DIFF),
    'IOB18M': (_S7_HP_SE, _S7_HP_DIFF),
    'IOB18S': (_S7_HP_SE, _S7_HP_DIFF),
    'HPIOB': (_US_HP_SE, _US_HP_DIFF),
    'HPIOB_M': (_US_HP_SE, _US_HP_DIFF),
    'HPIOB_S': (_US_HP_SE, _US_HP_DIFF),
    'HPIOB_SNGL': (_US_HP_SE, []),
    'HRIO': (_US_HR_SE, _US_HR_DIFF),
    'HDIOB_M': (_HD_SE, _HD_DIFF),
    'HDIOB_S': (_HD_SE, _HD_DIFF),
}
IO_PROPS = [
    ('DRIVE', ['2', '4', '6', '8', '12', '16', '24']),
    ('SLEW', ['SLOW', 'FAST', 'MEDIUM']),
    ('PULLTYPE', ['PULLUP', 'PULLDOWN', 'KEEPER', 'NONE']),
    ('IN_TERM', ['NONE', 'UNTUNED_SPLIT_40', 'UNTUNED_SPLIT_50',
                 'UNTUNED_SPLIT_60']),
    ('DIFF_TERM', ['TRUE', 'FALSE']),
    ('DIFF_TERM_ADV', ['TERM_100', 'TERM_NONE']),
    ('IBUF_LOW_PWR', ['TRUE', 'FALSE']),
    ('ODT', ['RTT_NONE', 'RTT_40', 'RTT_48', 'RTT_60', 'RTT_120',
             'RTT_240']),
    ('OUTPUT_IMPEDANCE', ['RDRV_40_40', 'RDRV_48_48', 'RDRV_60_60',
                          'RDRV_NONE_NONE']),
    ('PRE_EMPHASIS', ['RDRV_240', 'RDRV_NONE']),
    ('LVDS_PRE_EMPHASIS', ['TRUE', 'FALSE']),
    ('EQUALIZATION', ['EQ_LEVEL0', 'EQ_LEVEL1', 'EQ_LEVEL2', 'EQ_LEVEL3',
                      'EQ_LEVEL4', 'EQ_NONE']),
    ('DQS_BIAS', ['TRUE', 'FALSE']),
    ('IOB', ['TRUE', 'FALSE']),
    ('IOBDELAY', ['NONE', 'BOTH', 'IBUF', 'IFD']),
    ('DCI_CASCADE', []),
]
IO_REFS = {
    'in': ['IBUF', 'IBUF', 'IBUF_IBUFDISABLE', 'IBUF_INTERMDISABLE'],
    'out': ['OBUF'],
    'tri': ['OBUFT', 'OBUFT', 'OBUFT_DCIEN'],
    'inout': ['IOBUF', 'IOBUF', 'IOBUF_INTERMDISABLE', 'IOBUF_DCIEN'],
    'diffin': ['IBUFDS'],
    'diffout': ['OBUFDS'],
    'difftri': ['OBUFTDS', 'OBUFTDS', 'OBUFTDS_DCIEN'],
}
# Buffers that are macros (not in the primitive library dump) on some
# architectures (e.g. IBUF = INBUF + IBUFCTRL on UltraScale).
IO_MACROS = {'IBUF', 'IBUFDS', 'IOBUF', 'IBUF_IBUFDISABLE',
             'IBUF_INTERMDISABLE', 'IOBUF_INTERMDISABLE', 'IOBUF_DCIEN'}
# Buffer control inputs (input buffer / termination disable).
IO_CTRL_PINS = {
    'IBUF_IBUFDISABLE': ['IBUFDISABLE'],
    'IBUF_INTERMDISABLE': ['IBUFDISABLE', 'INTERMDISABLE'],
    'IOBUF_INTERMDISABLE': ['IBUFDISABLE', 'INTERMDISABLE'],
    'IOBUF_DCIEN': ['IBUFDISABLE', 'DCITERMDISABLE'],
    'OBUFT_DCIEN': ['DCITERMDISABLE'],
    'OBUFTDS_DCIEN': ['DCITERMDISABLE'],
}


def recipe_io(d, site, stype):
    rng = d.rng
    se, diff = IO_SITES[stype]
    modes = ['in', 'out', 'tri', 'inout']
    if diff:
        modes += ['diffin', 'diffout', 'difftri']
    mode = rng.choice(modes)
    ref = rng.choice(IO_REFS[mode])
    if ref.endswith('_DCIEN') and not stype.startswith(('IOB18', 'HPIOB')):
        # DCI only exists in High Performance banks.
        ref = IO_REFS[mode][0]
    if ref not in d.prims and ref not in IO_MACROS:
        return
    stds = diff if mode.startswith('diff') else se
    if mode in ('diffout', 'difftri'):
        # Receiver only standards.
        stds = [s for s in stds if not s.startswith('SLVS') and not (
            stype.startswith('HPIOB') and s.startswith('LVDS_25'))]
    if mode != 'inout':
        # *_T_DCI (split termination) standards are bidirectional only.
        stds = [s for s in stds if '_T_DCI' not in s]
    props = []
    us = is_us(d.die)
    if ref in IO_CTRL_PINS and 'IBUFDISABLE' in IO_CTRL_PINS[ref]:
        props += ['USE_IBUFDISABLE', rng.choice(['TRUE', 'FALSE'])]
    for k, vals in IO_PROPS:
        if k == 'SLEW' and not stype.startswith('HPIOB'):
            vals = ['SLOW', 'FAST']
        if k == 'IN_TERM' and not (stype.startswith(('IOB33', 'HRIO')) and
                                   mode in ('in', 'inout', 'diffin')):
            # Input termination: High Range banks, inputs only.
            continue
        if k == 'IOBDELAY' and us:
            # UltraScale has no IOB delay: Vivado inserts a ZHOLD_DELAY
            # the device does not support, failing the whole design.
            continue
        if vals and rng.random() < 0.35:
            props += [k, rng.choice(vals)]
    ref2 = None
    if mode in ('in', 'diffin'):
        # (Bitslices need their BITSLICE_CONTROL: see recipe_native.)
        opts = (['IDDRE1', 'ISERDESE3', 'IDELAYE3'] if us else
                ['IDDR', 'IDDR_2CLK', 'ISERDESE2', 'IDELAYE2'])
        if stype.startswith('HDIOB'):
            # HD banks: only IDDR (no delays / SERDES).
            opts = ['IDDRE1']
        opts = [o for o in opts if o in d.prims]
        if rng.random() < 0.5 and opts:
            ref2 = rng.choice(opts)
        if ref2 and ref2.startswith('IDELAY') and 'IOBDELAY' in props:
            # An IOB delay setting on a buffer feeding an IDELAY makes
            # Vivado insert a ZHOLD_DELAY between them (unroutable).
            i = props.index('IOBDELAY')
            del props[i:i + 2]
    name = d.name('io')
    d.lines.append(f'nl_iob {name} {site} {mode} {ref} {{{" ".join(stds)}}} '
                   f'{{{" ".join(props)}}}')
    for p in IO_CTRL_PINS.get(ref, ()):
        d.add_sink(f'{name}/{p}', site, hard=True)
    if mode in ('in', 'diffin'):
        if ref2:
            n2 = d.cell(ref2, None, None, d.random_params(ref2))
            din = {'RX_BITSLICE': 'DATAIN', 'RXTX_BITSLICE': 'DATAIN'}.get(
                ref2, 'IDATAIN' if ref2.startswith('IDELAY') else 'D')
            d.connect(f'{name}/O', [f'{n2}/{din}'])
            for direction, pin in pins_of(d.prims, ref2):
                if pin == din or CASCADE_PINS.search(pin) or \
                        pin in DEDICATED.get(ref2, ()):
                    continue
                full = f'{n2}/{pin}'
                if direction == 'IN' and rng.random() < 0.7:
                    d.add_sink(full, site, 'ioclock' if DIRECT_CLOCKS.search(pin)
                               else 'data')
                elif direction == 'OUT' and rng.random() < 0.7:
                    d.add_source(full, site)
        elif rng.random() < 0.25:
            # Pad driven clock (clock capable pins use dedicated routes).
            d.clocks.append(f'{name}/O')
        else:
            d.add_source(f'{name}/O', site)
    elif mode == 'inout':
        d.add_source(f'{name}/O', site)
        d.add_sink(f'{name}/I', site, hard=True)
        d.add_sink(f'{name}/T', site, hard=True)
    else:
        opts = (['ODDRE1', 'OSERDESE3'] if us else ['ODDR', 'OSERDESE2'])
        if stype.startswith('HDIOB'):
            opts = ['ODDRE1']
        opts = [o for o in opts if o in d.prims]
        if rng.random() < 0.5 and opts:
            ref2 = rng.choice(opts)
            n2 = d.cell(ref2, None, None, d.random_params(ref2))
            dout = {'TX_BITSLICE': 'O', 'RXTX_BITSLICE': 'O'}.get(
                ref2, 'Q' if 'DDR' in ref2 else 'OQ')
            d.connect(f'{n2}/{dout}', [f'{name}/I'])
            tq = {'OSERDESE2': 'TQ', 'OSERDESE3': 'T_OUT',
                  'TX_BITSLICE': 'T_OUT', 'RXTX_BITSLICE': 'T_OUT'}.get(ref2)
            if tq and mode in ('tri', 'difftri'):
                d.connect(f'{n2}/{tq}', [f'{name}/T'])
            for direction, pin in pins_of(d.prims, ref2):
                if CASCADE_PINS.search(pin) or pin in DEDICATED.get(ref2, ()):
                    continue
                full = f'{n2}/{pin}'
                if direction == 'IN' and rng.random() < 0.7:
                    d.add_sink(full, site, 'ioclock' if DIRECT_CLOCKS.search(pin)
                               else 'data')
                elif direction == 'OUT' and rng.random() < 0.7:
                    d.add_source(full, site)
            if mode in ('tri', 'difftri') and not tq:
                d.add_sink(f'{name}/T', site, hard=True)
        else:
            d.add_sink(f'{name}/I', site, hard=True)
            if mode in ('tri', 'difftri'):
                d.add_sink(f'{name}/T', site, hard=True)


def _bus(d, ref, pin):
    """Indices of the bits of bus <pin> of <ref>."""
    out = []
    for _, p in pins_of(d.prims, ref):
        m = re.match(re.escape(pin) + r'\[(\d+)\]$', p)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def _connect_bus(d, src, sref, spin, dst, dref, dpin):
    for b in sorted(set(_bus(d, sref, spin)) & set(_bus(d, dref, dpin))):
        d.connect(f'{src}/{spin}[{b}]', [f'{dst}/{dpin}[{b}]'])


def _native_bytes(d):
    """HP I/O pads and their bitslices grouped by byte:
    {(bitslice column, byte): {bit: (pad site, pad type, bitslice site)}}.

    Each clock region row of an I/O column has one bank: 52 pads and 52
    bitslices (4 bytes of 13, bits 0-5 lower nibble, 6-12 upper nibble),
    matched in site order; pad columns pair with the nearest bitslice
    column."""
    if getattr(d, '_nbytes', None) is not None:
        return d._nbytes
    d._nbytes = {}
    die = d.die

    def cols(types):
        out = collections.defaultdict(lambda: collections.defaultdict(list))
        for st in types:
            for s in die.by_type.get(st, []):
                _, x, y = _SITE_IDX.match(s).groups()
                v = die.sites[s]
                out[int(x)][die.tile_cr.get(v[1], '-')].append(
                    (int(y), s, st, v[3]))
        return out
    pads = cols(('HPIOB', 'HPIOB_M', 'HPIOB_S', 'HPIOB_SNGL'))
    bss = cols(('BITSLICE_RX_TX',))
    if not bss:
        return d._nbytes
    bsgx = {x: next(iter(v.values()))[0][3] for x, v in bss.items()}
    for px, crs in pads.items():
        pgx = next(iter(crs.values()))[0][3]
        bx = min(bsgx, key=lambda x: abs(bsgx[x] - pgx))
        for cr, plist in crs.items():
            blist = sorted(bss[bx].get(cr, []))
            plist = sorted(plist)
            if len(plist) != 52 or len(blist) != 52:
                continue
            for (_, ps, pst, _), (by, bs, _, _) in zip(plist, blist):
                d._nbytes.setdefault((bx, by // 13), {})[by % 13] = \
                    (ps, pst, bs)
    return d._nbytes


# Pins of native mode bitslices / controls driven by dedicated routes only.
_NATIVE_SKIP = re.compile(r'^(RX_BIT_CTRL|TX_BIT_CTRL|BIT_CTRL|DATAIN$|O$|'
                          r'T_OUT$|TBYTE_IN|RX_DIV\d_CLK_Q|CLK_FROM_EXT|'
                          r'[PN]CLK_NIBBLE_IN|PLL_CLK|CLK_TO_EXT|'
                          r'[PN]CLK_NIBBLE_OUT|RIU_RD_DATA|RIU_VALID|.*_EXT)')


def _native_fabric_pins(d, n, ref, site, clk_pins):
    rng = d.rng
    for dr, p in pins_of(d.prims, ref):
        if _NATIVE_SKIP.match(p):
            continue
        full = f'{n}/{p}'
        if dr == 'IN' and re.search(r'CLK$', p):
            if rng.random() < 0.8:
                clk_pins.append(full)
        elif dr == 'IN' and rng.random() < 0.7:
            d.add_sink(full, site, 'data', hard=True)
        elif dr == 'OUT' and rng.random() < 0.6:
            d.add_source(full, site)


def recipe_native(d, site, ref):
    """One byte of native mode I/O: bitslices on the pads of one or both
    nibbles, each nibble's BITSLICE_CONTROL (clocked by a PLL CLKOUTPHY or
    its reference clock input) and TX_BITSLICE_TRI, and the byte's RIU_OR
    joining the two controls' register interface outputs."""
    rng = d.rng
    nbytes = _native_bytes(d)
    free = [k for k in nbytes if k not in d.native_used]
    if not free or len(d.native_used) >= 8:
        return
    key = rng.choice(free)
    d.native_used.add(key)
    pads = nbytes[key]
    bx, byte = key

    def loc(prefix, y):
        s = f'{prefix}_X{bx}Y{y}'
        return s if s in d.die.sites else None
    # One fabric clock for all bitslice clocks of the byte (at most 6
    # clocks per half bank).
    clk_pins = []
    pll = None
    pllref = next((r for r in ('PLLE4_ADV', 'PLLE3_ADV') if r in d.prims),
                  None)
    # The PLL feeding PLL_CLK must be in the clock region of the byte.
    anybs = next(iter(pads.values()))[2]
    cr = d.die.tile_cr.get(d.die.sites[anybs][1])
    pllsites = [s for st in ('PLLE3_ADV', 'PLL') for s in d.die.by_type.get(st, [])
                if d.die.tile_cr.get(d.die.sites[s][1]) == cr and
                s not in d.sites_used]
    if pllref and pllsites and rng.random() < 0.8:
        props = d.random_params(pllref)
        props.update(clockgen_params(pllref, d.prims[pllref].params, rng))
        psite = rng.choice(pllsites)
        d.sites_used.add(psite)
        pll = d.cell(pllref, psite, None, props)
        d.connect(f'{pll}/CLKFBOUT', [f'{pll}/CLKFBIN'])
        d.add_sink(f'{pll}/CLKIN', site, 'gclock', hard=True)
        for p in ('RST', 'PWRDWN', 'CLKOUTPHYEN'):
            d.add_sink(f'{pll}/{p}', site, hard=True)
        if rng.random() < 0.5:
            global_buffer(d, site, f'{pll}/CLKOUT0')
    nibbles = rng.choice([[0], [1], [0, 1], [0, 1]])
    ctrls = {}
    for nib in nibbles:
        bits = range(0, 6) if nib == 0 else range(6, 13)
        avail = [b for b in bits if b in pads]
        if not avail:
            continue
        cprops = d.random_params('BITSLICE_CONTROL')
        for k in ('EN_OTHER_PCLK', 'EN_OTHER_NCLK'):
            cprops[k] = 'FALSE'
        cprops['REFCLK_SRC'] = 'PLLCLK' if pll else 'REFCLK'
        ctl = d.cell('BITSLICE_CONTROL', loc('BITSLICE_CONTROL',
                                             2 * byte + nib), None, cprops)
        ctrls[nib] = ctl
        if pll:
            d.connect(f'{pll}/CLKOUTPHY', [f'{ctl}/PLL_CLK'])
        _native_fabric_pins(d, ctl, 'BITSLICE_CONTROL', site, clk_pins)
        tri = None
        if rng.random() < 0.6:
            tri = d.cell('TX_BITSLICE_TRI', loc('BITSLICE_TX', 2 * byte + nib),
                         None, d.random_params('TX_BITSLICE_TRI'))
            _connect_bus(d, ctl, 'BITSLICE_CONTROL', 'TX_BIT_CTRL_OUT_TRI',
                         tri, 'TX_BITSLICE_TRI', 'BIT_CTRL_IN')
            _connect_bus(d, tri, 'TX_BITSLICE_TRI', 'BIT_CTRL_OUT',
                         ctl, 'BITSLICE_CONTROL', 'TX_BIT_CTRL_IN_TRI')
            _native_fabric_pins(d, tri, 'TX_BITSLICE_TRI', site, clk_pins)
        tbyte = []
        for b in rng.sample(avail, rng.randint(1, len(avail))):
            i = b - bits[0]
            psite, stype, bsite = pads[b]
            mode = rng.choice(['in', 'out', 'tri', 'inout'])
            sref = {'in': rng.choice(['RX_BITSLICE', 'RXTX_BITSLICE']),
                    'out': rng.choice(['TX_BITSLICE', 'RXTX_BITSLICE']),
                    'tri': rng.choice(['TX_BITSLICE', 'RXTX_BITSLICE']),
                    'inout': 'RXTX_BITSLICE'}[mode]
            iref = {'in': 'IBUF', 'out': 'OBUF', 'tri': 'OBUFT',
                    'inout': 'IOBUF'}[mode]
            props = d.random_params(sref)
            for k in ('RX_DATA_TYPE', 'DATA_TYPE'):
                if k in props and i != 0 and props[k] in (
                        'CLOCK', 'DATA_AND_CLOCK'):
                    props[k] = 'DATA'
            if 'CASCADE' in props:
                props['CASCADE'] = 'FALSE'
            # Native mode bitslices must be LOCed (Vivado does not place
            # them next to their pads).
            sl = d.cell(sref, bsite, None, props)
            io = d.name('io')
            d.lines.append(f'nl_iob {io} {psite} {mode} {iref} '
                           f'{{{" ".join(IO_SITES[stype][0])}}} {{}}')
            if mode in ('in', 'inout'):
                d.connect(f'{io}/O', [f'{sl}/DATAIN'])
            if mode != 'in':
                d.connect(f'{sl}/O', [f'{io}/I'])
            if mode in ('tri', 'inout'):
                d.connect(f'{sl}/T_OUT', [f'{io}/T'])
            for bus in ('RX_BIT_CTRL', 'TX_BIT_CTRL'):
                _connect_bus(d, ctl, 'BITSLICE_CONTROL', f'{bus}_OUT{i}',
                             sl, sref, f'{bus}_IN')
                _connect_bus(d, sl, sref, f'{bus}_OUT',
                             ctl, 'BITSLICE_CONTROL', f'{bus}_IN{i}')
            if tri and props.get('TBYTE_CTL') == 'TBYTE_IN':
                tbyte.append(f'{sl}/TBYTE_IN')
            _native_fabric_pins(d, sl, sref, site, clk_pins)
        if tbyte:
            d.connect(f'{tri}/TRI_OUT', tbyte)
    if ctrls and rng.random() < 0.8:
        riu = d.cell('RIU_OR', loc('RIU_OR', byte))
        for nib, side in ((0, 'LOW'), (1, 'UPP')):
            if nib in ctrls:
                _connect_bus(d, ctrls[nib], 'BITSLICE_CONTROL', 'RIU_RD_DATA',
                             riu, 'RIU_OR', f'RIU_RD_DATA_{side}')
                d.connect(f'{ctrls[nib]}/RIU_VALID',
                          [f'{riu}/RIU_RD_VALID_{side}'])
        for dr, p in pins_of(d.prims, 'RIU_OR'):
            if dr == 'OUT' and rng.random() < 0.6:
                d.add_source(f'{riu}/{p}', site)
    else:
        for c in ctrls.values():
            for b in _bus(d, 'BITSLICE_CONTROL', 'RIU_RD_DATA'):
                if rng.random() < 0.5:
                    d.add_source(f'{c}/RIU_RD_DATA[{b}]', site)
    if clk_pins:
        d.add_sink(clk_pins, site, 'ioclock')


for _r in ('BITSLICE_CONTROL', 'RIU_OR', 'TX_BITSLICE_TRI', 'RX_BITSLICE',
           'TX_BITSLICE', 'RXTX_BITSLICE'):
    HARD_RECIPES[_r] = recipe_native


def recipe_pips(d, targets, n=300):
    """Directed PIP coverage: for each target (tile type, wire0, wire1, dir)
    pick a random tile of that type, a source LUT and a sink LUT in nearby
    slices, and ask for the net to be routed through the PIP."""
    rng = d.rng
    die = d.die
    by_type = collections.defaultdict(list)
    for tile, (ttype, gx, gy) in die.tile_pos.items():
        by_type[ttype].append((tile, gx, gy))
    slices = [(s, v[3], v[4]) for s, v in die.sites.items()
              if v[0] in ('SLICEL', 'SLICEM')]
    grid = collections.defaultdict(list)
    for s, gx, gy in slices:
        grid[(gx // 8, gy // 8)].append(s)
    used = set()
    rng.shuffle(targets)
    k = 0
    for tt, w0, w1, dirn in targets:
        if k >= n:
            break
        # find_routing_path crashes Vivado on some clock network PIPs.
        if re.search(r'GCLK|HCLK|CLK', w0 + w1):
            continue
        tiles = by_type.get(tt)
        if not tiles:
            continue
        tile, gx, gy = rng.choice(tiles)
        near = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                near += grid.get((gx // 8 + dx, gy // 8 + dy), [])
        near = [s for s in near if s not in used]
        if len(near) < 2:
            continue
        a, b = rng.sample(near, 2)
        used.update((a, b))
        src = d.cell('LUT1', a, None, {'INIT': "2'h1"})
        dst = d.cell('LUT1', b, None, {'INIT': "2'h2"})
        d.add_sink(f'{src}/I0', a)
        net = f'np{k}'
        sep = '->>' if dirn == '1' else '<<->>'
        d.lines_post.append(f'nl_net {net} {{{src}/O {dst}/I0}}')
        d.lines_post.append(f'nl_want_pip {net} {tile}/{tt}.{w0}{sep}{w1}')
        d.add_source(f'{dst}/O', b)
        k += 1


def generate(die, prims, seed, out, density, hard=True, pips=None,
             focus=None):
    rng = random.Random(seed)
    if density is None:
        density = rng.uniform(0.02, 0.35)
    d = Design(die, prims, rng)
    if pips:
        targets = [tuple(l.split()[:4]) for l in open(pips) if l.strip()]
        recipe_pips(d, targets)
        d.emit(out)
        return
    arch = {'kintexu': 'UltraScale', 'kintexuplus': 'UltraScalePlus',
            'zynquplus': 'UltraScalePlus', 'artixuplus': 'UltraScalePlus'}.get(
                die.arch, 'Series7')
    bufg_sites = die.by_type.get('BUFGCE') or die.by_type.get(
        'BUFGCTRL') or []
    for s in rng.sample(bufg_sites, min(len(bufg_sites), rng.randint(1, 6))):
        recipe_bufg(d, s)
    nslices = sum(len(die.by_type.get(st, [])) for st in ('SLICEL', 'SLICEM'))
    # Bound the design size on large dies.
    density = min(density, 4000.0 / max(1, nslices))
    for st in ('SLICEL', 'SLICEM'):
        for s in die.by_type.get(st, []):
            if rng.random() < density:
                recipe_slice(d, s, st == 'SLICEM')
    if hard:
        refs = SITE_REFS[arch]
        # A few hard site types per design, so that an infeasible
        # combination only spoils a fraction of the designs.
        res = {}
        try:
            import dies as dieslib
            res = dieslib.part_resources().get(die.part, {})
        except Exception:
            pass
        avail = sorted(st for st in die.by_type
                       if st in refs or st in IO_SITES)
        if res.get('gt', 1) == 0:
            # No transceivers in this package: GT sites are not usable.
            avail = [st for st in avail if not re.match(
                r'^(GT|IBUFDS_GTE|PCIE|BUFG_GT|CMAC|ILKN)', st)]
        io_cap = max(8, int(0.8 * res.get('io', 10**6)))
        io_used = rng.random() < 0.6 and not focus
        if io_used:
            # I/O logic sites are used through the I/O buffers then.
            avail = [st for st in avail if not re.match(
                r'^(ILOGIC|OLOGIC|IDELAY|ODELAY|BITSLICE|HDIOLOGIC)', st)]
        chosen = rng.sample(avail, min(len(avail), rng.randint(1, 4)))
        if focus:
            # Experiments: only the site types matching the focus pattern.
            chosen = [st for st in avail if re.search(focus, st)]
        # Core blocks (block RAM, DSP) in about half of the designs, clock
        # generators (few sites, many configuration bits) in 40%.
        for st in avail if not focus else ():
            if st in chosen:
                continue
            if re.match(r'^(RAMB|RAMBFIFO|DSP|URAM)', st) and \
                    rng.random() < 0.5:
                chosen.append(st)
            elif re.match(r'^(MMCM|PLL$|PLLE)', st) and rng.random() < 0.4:
                chosen.append(st)
        # 18 Kb and 36 Kb block RAM sites share tiles and the first type
        # processed takes them: alternate which one goes first (RAMB18
        # sorted first, leaving few RAMB36/FIFO36 tiles).
        if rng.random() < 0.5:
            chosen.reverse()
        used = collections.Counter()
        bram_tiles = {}
        for st in chosen:
            p = rng.choice([0.1, 0.3, 0.6, 1.0])
            cap = len(die.by_type[st])
            if cap <= 4:
                # Blocks with one or a few sites (PCIe, PS, SYSMON, CMAC,
                # configuration, ...): mostly used once the type is chosen.
                p = max(p, 0.7)
            if re.match(r'^(ILOGIC|OLOGIC|IDELAY|ODELAY|IOB|HPIOB|HRIO|HDIOB|'
                        r'BITSLICE|IN_FIFO|OUT_FIFO)', st):
                cap = min(cap, io_cap // 3)
            if st.startswith('BUFGCTRL'):
                cap = min(cap, 20)
            n_st = 0
            for s in die.by_type[st]:
                if n_st >= cap:
                    break
                if rng.random() < p:
                    # A block RAM tile is either one 36 Kb RAM or two 18 Kb
                    # RAMs (the sites overlap physically).
                    if re.match(r'^(RAMB|RAMBFIFO)', st):
                        tile = die.sites[s][1]
                        kind = '36' if '36' in st else '18'
                        if bram_tiles.setdefault(tile, kind) != kind:
                            continue
                    n_st += 1
                    if st in IO_SITES:
                        recipe_io(d, s, st)
                        used[st] += 1
                        continue
                    ref = rng.choice(refs[st])
                    recipe_hard(d, s, ref)
                    used[ref] += 1
        # I/O in most designs.
        if io_used:
            # Moderate density: dense I/O designs mostly fail on I/O placer
            # conflicts; coverage comes from many designs instead.
            p = rng.choice([0.05, 0.15, 0.3])
            pads = [(s, st) for st in IO_SITES for s in die.by_type.get(st, [])]
            rng.shuffle(pads)
            n_io = 0
            for s, st in pads:
                if rng.random() < p and n_io < 40:
                    recipe_io(d, s, st)
                    used[st] += 1
                    n_io += 1
        delays = any(r.startswith(('IDELAYE', 'ODELAYE')) for r in used) or \
            any(re.search(r' (IDELAYE|ODELAYE)\w* ', l) for l in d.lines)
        if delays and 'IDELAYCTRL' in prims and 'IDELAYCTRL' not in used:
            n = d.cell('IDELAYCTRL')
            # REFCLK: from a global buffer (not a pad: unroutable).
            d.add_sink(f'{n}/REFCLK', die.by_type['SLICEL'][0], 'gclock')
            d.add_sink(f'{n}/RST', die.by_type['SLICEL'][0])
        with open(os.path.join(os.path.dirname(os.path.abspath(out)),
                               'design.meta'), 'w') as f:
            f.write(' '.join(f'{k}:{v}' for k, v in sorted(used.items())) +
                    '\n')
    d.emit(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tiles', required=True)
    ap.add_argument('--prims', required=True)
    ap.add_argument('--seed', type=int, required=True)
    ap.add_argument('--density', type=float, default=None)
    ap.add_argument('--out', required=True)
    ap.add_argument('--part', default=None)
    ap.add_argument('--pips', default=None, help='target pip list')
    ap.add_argument('--focus', default=None,
                    help='regexp: only use hard site types matching it')
    args = ap.parse_args()
    die = Die(args.tiles)
    if args.part:
        die.part = args.part
    prims = primlib.load(args.prims)
    generate(die, prims, args.seed, args.out, args.density, pips=args.pips,
             focus=args.focus)


if __name__ == '__main__':
    main()
