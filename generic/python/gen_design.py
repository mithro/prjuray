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
            for s in p[6].split(','):
                name, stype = s.split(':')
                self.sites[name] = (stype, tile, ttype, gx, gy)
                self.by_type[stype].append(name)


# Parameter values that are listed as legal but break Vivado.
BAD_VALUES = {('CE_TYPE', 'HARDSYNC')}


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
        self.pblocks = []  # (range, cells)
        self.pblock_used = set()
        self.lines_post = []

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
        hard: pin of a hard block (not tied to constants)."""
        gx, gy = self.loc_of(site)
        self.sinks.append((pin, gx, gy, kind if not hard else kind + '_hard'))

    def connect(self, driver, sinks):
        self.fixed_nets.append((driver, list(sinks)))

    def emit(self, path):
        rng = self.rng
        # Bucket sources spatially so most connections are local.
        grid = collections.defaultdict(list)
        for pin, gx, gy in self.sources:
            grid[(gx // 16, gy // 16)].append(pin)
        allsrc = [s[0] for s in self.sources]
        drive = collections.defaultdict(list)
        const0, const1 = [], []
        for pin, gx, gy, kind in self.sinks:
            pins = pin if isinstance(pin, list) else [pin]
            hard = kind.endswith('_hard')
            kind = kind.replace('_hard', '')
            if kind == 'clock' and self.clocks:
                drive[rng.choice(self.clocks)].extend(pins)
                continue
            r = rng.random()
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
    # Carry chain element, S inputs driven by dedicated LUTs.
    if rng.random() < 0.3:
        ref = 'CARRY8' if us else 'CARRY4'
        props = d.random_params(ref) if us else {}
        n = cell(ref, props)
        for direction, pin in pins_of(d.prims, ref):
            full = f'{n}/{pin}'
            if direction == 'IN':
                if pin.startswith('S[') and rng.random() < 0.7:
                    k = rng.randint(1, 6)
                    l = cell(f'LUT{k}', d.random_params(f'LUT{k}'))
                    add_generic_pins(d, f'LUT{k}', l, site, skip=('O', ))
                    d.connect(f'{l}/O', [full])
                    continue
                d.add_sink(full, site)
            else:
                d.add_source(full, site)
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
    clk, ce, sr = [], [], []
    for i in range(nff):
        ref = rng.choice(choices)
        props = {'INIT': rng.choice(["1'b0", "1'b1"])}
        if ref[0] == 'F':
            props['IS_C_INVERTED'] = cinv
        else:
            props['IS_G_INVERTED'] = cinv
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


# Site type -> candidate primitives, per architecture.  Sites not listed
# here are handled by dedicated recipes (slices, IO) or have no primitive.
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
        'BUFCE_ROW': ['BUFCE_ROW'],
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
        'BUFCE_ROW': ['BUFCE_ROW'],
        'BUFCE_ROW_FSR': ['BUFCE_ROW'],
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
}


def clockgen_params(ref, props, rng):
    """Mostly legal MMCM/PLL parameters (random ones rarely pass bitgen)."""
    mmcm = ref.startswith('MMCM')
    frac = mmcm and rng.random() < 0.3
    mult = rng.randint(2, 64)
    if frac:
        mult += rng.randint(1, 7) * 0.125
    if mmcm:
        props['CLKFBOUT_MULT_F'] = f'{mult:.3f}'
    else:
        props['CLKFBOUT_MULT'] = str(int(mult))
    props['DIVCLK_DIVIDE'] = str(rng.randint(1, 8))
    for i in range(7):
        div = rng.randint(1, 128)
        if i == 0 and mmcm:
            if rng.random() < 0.3:
                div = max(2, div) + rng.randint(1, 7) * 0.125
                frac = True
            props['CLKOUT0_DIVIDE_F'] = f'{div:.3f}'
        else:
            props[f'CLKOUT{i}_DIVIDE'] = str(int(div))
        props[f'CLKOUT{i}_PHASE'] = rng.choice(
            ['0.000', '45.000', '90.000', '180.000', '-90.000'])
        props[f'CLKOUT{i}_DUTY_CYCLE'] = rng.choice(['0.500', '0.250',
                                                     '0.750'])
    props['CLKFBOUT_PHASE'] = rng.choice(['0.000', '90.000'])
    props['CLKIN1_PERIOD'] = f'{rng.uniform(1.0, 50.0):.3f}'
    props['CLKIN2_PERIOD'] = f'{rng.uniform(1.0, 50.0):.3f}'
    for k in list(props):
        if k.endswith('_USE_FINE_PS'):
            props[k] = 'FALSE' if frac else rng.choice(['TRUE', 'FALSE'])
    # Only keep parameters the primitive has.
    return props


CLOCK_SOURCES = re.compile(r'^(MMCM|PLL|BUFR|BUFH|BUFIO|BUFMR|BUFCE_ROW|'
                           r'BUFGCE_DIV|BUFG_GT|BUFG_PS)')
CLOCK_OUT_PINS = re.compile(r'^(CLKOUT\d+B?|CLKFBOUTB?|O|CLKOUTPHY)$')


def recipe_hard(d, site, ref, pconn=0.6):
    """One primitive LOCed onto a hard site, random parameters, random
    connections (clock-like pins preferably to global clocks)."""
    rng = d.rng
    if ref not in d.prims:
        return
    props = d.random_params(ref)
    if ref.startswith(('MMCM', 'PLL')):
        props = clockgen_params(ref, props, rng)
        props = {k: v for k, v in props.items() if k in d.prims[ref].params}
    n = d.cell(ref, site, None, props)
    shared = set()
    if ref.startswith(('MMCM', 'PLL')) and rng.random() < 0.5:
        # Internal feedback loop, as in most real designs.
        d.connect(f'{n}/CLKFBOUT', [f'{n}/CLKFBIN'])
        shared |= {'CLKFBOUT', 'CLKFBIN'}
    if props.get('CLOCK_DOMAINS') == 'COMMON' or props.get('EN_SYN') == 'TRUE':
        pins = {p for _, p in pins_of(d.prims, ref)}
        for pair in (('CLKARDCLK', 'CLKBWRCLK'), ('RDCLK', 'WRCLK')):
            if set(pair) <= pins:
                shared |= set(pair)
                d.add_sink([f'{n}/{p}' for p in pair], site, 'clock')
    for direction, pin in pins_of(d.prims, ref):
        full = f'{n}/{pin}'
        if CASCADE_PINS.search(pin) or pin in DEDICATED.get(ref, ()) or \
                pin in shared:
            continue
        if direction == 'IN':
            if rng.random() < pconn or CLOCK_BUFFERS.match(ref):
                kind = 'clock' if (DIRECT_CLOCKS.search(pin) or (
                    CLOCK_BUFFERS.match(ref) and pin in ('I', 'I0', 'I1'))) \
                    else 'data'
                d.add_sink(full, site, kind, hard=True)
        elif direction == 'OUT':
            if rng.random() < pconn:
                d.add_source(full, site)


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
    name = d.name('io')
    d.lines.append(f'nl_iob {name} {site} {mode} {ref} {{{" ".join(stds)}}} '
                   f'{{{" ".join(props)}}}')
    for p in IO_CTRL_PINS.get(ref, ()):
        d.add_sink(f'{name}/{p}', site, hard=True)
    if mode in ('in', 'diffin'):
        r = rng.random()
        opts = (['IDDRE1', 'ISERDESE3', 'IDELAYE3', 'RX_BITSLICE',
                 'RXTX_BITSLICE'] if us else
                ['IDDR', 'IDDR_2CLK', 'ISERDESE2', 'IDELAYE2'])
        opts = [o for o in opts if o in d.prims]
        if r < 0.5 and opts:
            ref2 = rng.choice(opts)
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
                    d.add_sink(full, site, 'clock' if DIRECT_CLOCKS.search(pin)
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
        opts = (['ODDRE1', 'OSERDESE3', 'TX_BITSLICE', 'RXTX_BITSLICE']
                if us else ['ODDR', 'OSERDESE2'])
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
                    d.add_sink(full, site, 'clock' if DIRECT_CLOCKS.search(pin)
                               else 'data')
                elif direction == 'OUT' and rng.random() < 0.7:
                    d.add_source(full, site)
            if mode in ('tri', 'difftri') and not tq:
                d.add_sink(f'{name}/T', site, hard=True)
        else:
            d.add_sink(f'{name}/I', site, hard=True)
            if mode in ('tri', 'difftri'):
                d.add_sink(f'{name}/T', site, hard=True)


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
        # Core blocks (block RAM, DSP) in about half of the designs.
        for st in avail if not focus else ():
            if re.match(r'^(RAMB|RAMBFIFO|DSP|URAM)', st) and \
                    st not in chosen and rng.random() < 0.5:
                chosen.append(st)
        used = collections.Counter()
        bram_tiles = {}
        for st in chosen:
            p = rng.choice([0.1, 0.3, 0.6, 1.0])
            cap = len(die.by_type[st])
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
            d.add_sink(f'{n}/REFCLK', die.by_type['SLICEL'][0], 'clock')
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
