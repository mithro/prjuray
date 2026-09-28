#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Convert raw feature dumps (generic/tcl/dump_features.tcl) into per-tile
feature sets.

Feature naming (relative to the tile, prefixed by the tile type in the DB):
  <wire0>-><wire1>                 routing pip (dir 1)
  <wire0><->< wire1>               bidirectional pip (dir 0)
  <SITEKEY>.TYPE.<site type>       site in use with that site type
  <SITEKEY>.<BEL>.SP.<from>.<to>   site pip (routing BEL input selection)
  <SITEKEY>.<BEL>.<CFG>=<value>    enumerated BEL configuration
  <SITEKEY>.<BEL>.<CFG>[i]         bit i of a vector BEL configuration is 1
  <SITEKEY>.<BEL>.<CFG>[i]=0       bit i is 0 (vectors of <= 64 bits)
  <SITEKEY>.<BEL>.INIT[i]          LUT truth table bit (from EQN)
  <SITEKEY>.BANK.IOSTD=<std>       an I/O standard used in the pad's bank
  <SITEKEY>.BANK.VCCO=<volts>      the bank's VCCO (from its I/O standards)
  <SITEKEY>.PAD.PULLTYPE=<v>       pull resistor of a used pad
  <SITEKEY>.<BEL>.<CFG>=<v>@<STD>=<s>  I/O buffer setting together with its
                                   I/O standard (SLEW, DRIVE, IN_TERM, ...)
  <vector bit feature>@<W>=<v>     vector bit together with a WIDTH setting
                                   <W> of the same BEL (same port suffix)

SITEKEY is <site prefix>_X<dx>Y<dy>, relative to the lowest coordinates of
same-prefix sites in the tile.
"""
import collections
import gzip
import os
import re

import numpy as np

# Feature cache hooks (designdata.feature_inputs_stamp): environment
# variables that change the features (A/B switches) and a function
# returning the other files a die's features depend on (e.g. per die meta
# files read by a derived pass).  features.py itself, generic/data/*,
# clockgen_tables.json and the die's tiles / bonded files are always
# covered.
STAMP_ENV = ('URAY_PARK',)


def stamp_files(tiles_tsv):
    # the die's PIP list (build/meta/pips/<die>.txt): _gclk_feeds,
    # parked_imux_features
    meta = os.path.dirname(os.path.dirname(tiles_tsv))
    name = os.path.splitext(os.path.basename(tiles_tsv))[0]
    return [os.path.join(meta, 'pips', name + '.txt')]


_XY = re.compile(r'^(.*)_X(\d+)Y(\d+)$')
_VEC = re.compile(r"^(\d+)'([bh])([0-9a-fA-F_]+)$")


# Widest vector configuration whose 0 bits are features too.
MAX_ZERO_VEC = 64

def _std_vcco():
    """I/O standard -> VCCO (None when it differs between bank types)."""
    import gen_design
    out = {}
    for se, diff in gen_design.IO_SITES.values():
        for sv in se + diff:
            std, v = sv.split(':')
            out[std] = v if out.get(std, v) == v else None
    return out


_VCCO = None

_DSP_REG = re.compile(r'^(DSP48_X\d+Y\d+)\.DSP48E1\.([AB])REG=(\d)$')
_OLOGIC_CLKINV = re.compile(r'^(OLOGIC_X\d+Y\d+)\.CLKINV\.SP\.(CLK|CLK_B)\.OUT$')
_OCLK_TO_OLOGIC = re.compile(r'^IOI_OCLK_(\d)->IOI_OLOGIC\1_CLK$')
_TO_OCLK = re.compile(r'^(\S+)->IOI_OCLK_(\d)$')
_TO_OCLKM = re.compile(r'^\S+->IOI_OCLKM_(\d)$')

# BEL -> (setting, conditioning setting) pairs, see tile_features.
_STD_CONDITIONED = {
    'OUTBUF': (('SLEW', 'OSTANDARD'), ('DRIVE', 'OSTANDARD')),
    'INBUF_EN': (('IN_TERM', 'ISTANDARD'), ('IBUF_LOW_PWR', 'ISTANDARD')),
    # 7-series DSP: one bit per port encodes AREG=2 with ACASCREG=1
    # (prjxray ZAREG_2_ACASCREG_1 / ZBREG_2_BCASCREG_1).
    'DSP48E1': (('AREG', 'ACASCREG'), ('BREG', 'BCASCREG')),
}

PAD_SITE = re.compile(r'^(IOB|HPIOB|HRIO|HDIOB|IOPAD|IPAD|OPAD)')


class SiteKeys:
    """Maps site names to (tile, relative site key) for a die."""

    def __init__(self, tiles_tsv):
        self.key = {}
        self.tile_type = {}
        self.pads = collections.defaultdict(list)  # tile -> pad sites
        # Bonded pad sites (build/meta/bonded/<die>.txt from
        # tcl/dump_bonded.tcl), None when not known.
        self.bonded = None
        bp = os.path.join(os.path.dirname(os.path.dirname(tiles_tsv)),
                          'bonded', os.path.basename(tiles_tsv)[:-4] + '.txt')
        if os.path.exists(bp):
            with open(bp) as f:
                self.bonded = set(f.read().split())
        self.region = {}  # tile -> clock region (X<c>Y<r>, '-')
        self.xy = {}  # tile -> (grid x, grid y)
        self.tiles_tsv = tiles_tsv
        for line in open(tiles_tsv):
            p = line.split()
            if p[0] != 'tile':
                continue
            self.tile_type[p[1]] = p[2]
            self.region[p[1]] = p[5]
            self.xy[p[1]] = (int(p[3]), int(p[4]))
            if p[6] == '-':
                continue
            groups = collections.defaultdict(list)
            for s in p[6].split(','):
                name, stype = s.split(':')
                if PAD_SITE.match(stype):
                    self.pads[p[1]].append(name)
                m = _XY.match(name)
                if not m:
                    self.key[name] = (p[1], name)
                    continue
                groups[m.group(1)].append((name, int(m.group(2)),
                                           int(m.group(3))))
            for prefix, lst in groups.items():
                mx = min(x for _, x, _ in lst)
                my = min(y for _, _, y in lst)
                for name, x, y in lst:
                    self.key[name] = (p[1], f'{prefix}_X{x - mx}Y{y - my}')


_EQN_CACHE = {}
_IDX = np.arange(64, dtype=np.uint8)
_ENV = {f'A{k + 1}': ((_IDX >> k) & 1).astype(np.uint8) for k in range(6)}
_IENV = {f'A{k + 1}': sum(1 << i for i in range(64) if (i >> k) & 1)
         for k in range(6)}
_IENV['_T'] = (1 << 64) - 1
_LUT_LIT = re.compile(r'(?<![A\d])\d+')
_LUT_LEAD0 = re.compile(r'(?<![A\d])0+[1-9]')
_MASK64 = (1 << 64) - 1
_SOP_LIT = r'(?:\(~A[1-6]\)|A[1-6])'
_SOP_TERM = rf'\({_SOP_LIT}(?:\*{_SOP_LIT})*\)'
_SOP = re.compile(rf'{_SOP_TERM}(?:\+{_SOP_TERM})*')


def lut_eqn_bits(eqn):
    """'O6=(A1*A2)+(~A3)' -> list of set truth-table indices."""
    if eqn in _EQN_CACHE:
        return _EQN_CACHE[eqn]
    if '=' not in eqn:
        return None
    out, expr = eqn.split('=', 1)
    n = 64 if out == 'O6' else 32
    py = expr.replace('*', '&').replace('+', '|').replace('@', '^')
    # Only LUT pin names, digits, parentheses and bit operators may reach
    # eval() below (no names other than A1..A6, no calls/attributes).
    if not re.fullmatch(r'[A1-6&|^~() 01]*', py):
        return None
    # Evaluated on 64 bit truth table masks (bit i = row i).
    if _SOP.fullmatch(expr):
        # Sum of products of (inverted) pins, the usual form: no eval.
        v = 0
        for term in expr[1:-1].split(')+('):
            t = _MASK64
            for lit in term.split('*'):
                t &= _IENV[lit] if lit[0] == 'A' else ~_IENV[lit[2:4]]
            v |= t
        bits = [i for i in range(n) if (v >> i) & 1]
        if len(_EQN_CACHE) < 200000:
            _EQN_CACHE[eqn] = bits
        return bits
    # Otherwise eval() with the same bitwise operators on the masks; a
    # literal is a constant row value (its bit 0): an odd literal is all
    # rows, an even one none.  A literal with leading zeros (e.g. 01) is a
    # syntax error in Python.
    if _LUT_LEAD0.search(py):
        return None
    try:
        v = eval(_LUT_LIT.sub(lambda m: '_T' if int(m.group()) & 1
                              else '0', py), {}, dict(_IENV))
    except (SyntaxError, TypeError, NameError):
        return None
    bits = [i for i in range(n) if (v >> i) & 1]
    if len(_EQN_CACHE) < 200000:
        _EQN_CACHE[eqn] = bits
    return bits


def cfg_features(prefix, name, value):
    if name == 'EQN':
        bits = lut_eqn_bits(value)
        if bits is None:
            return []
        return [f'{prefix}.INIT[{i}]' for i in bits]
    m = _VEC.match(value)
    if m:
        width = int(m.group(1))
        digits = m.group(3).replace('_', '')
        v = int(digits, 2 if m.group(2) == 'b' else 16)
        out = [f'{prefix}.{name}[{i}]' for i in range(width) if (v >> i) & 1]
        # Many attributes are stored inverted (a bit set when the value bit
        # is 0, e.g. BRAM INIT_A/SRVAL_A): name the 0 bits of short vectors
        # too.  Long content vectors (BRAM INIT_xx) are stored plainly.
        if width <= MAX_ZERO_VEC:
            out += [f'{prefix}.{name}[{i}]=0' for i in range(width)
                    if not (v >> i) & 1]
        return out
    out = [f'{prefix}.{name}={value}']
    if value.isdigit() and name.endswith('DELAY_VALUE'):
        # Delay taps are stored as binary (7-series IDELAY/ODELAY_VALUE,
        # 5 bits, prjxray [Z]IDELAY_VALUE[i]): name the bits too.
        v = int(value)
        width = 5 if v < 32 else max(11, v.bit_length())
        out += [f'{prefix}.{name}[{i}]' + ('' if (v >> i) & 1 else '=0')
                for i in range(width)]
    return out


# Clock generator (MMCM/PLL) dynamic reconfiguration register fields.
# Vivado's BEL configuration reports default counter settings; the values
# bitgen writes are derived from the divide / duty cycle / phase
# parameters as in Xilinx XAPP888 (mmcm_pll_drp_func_*.vh), reproduced
# here in the same fixed point arithmetic (10 fractional bits).
_FRAC = 10


def _round_frac(v, precision):
    if v >> (_FRAC - precision - 1) & 1:
        v += 1 << (_FRAC - precision)
    return v


def _drp_divider(divide, duty):
    """(high_time, low_time, edge, no_count) of a counter (mod 64)."""
    if divide == 1:
        return 1, 1, 0, 1
    duty_fix = (round(duty * 100000) << _FRAC) // 100000
    t = _round_frac(duty_fix * divide, 1)
    ht = (t >> _FRAC) & 0x7f
    edge = (t >> (_FRAC - 1)) & 1
    if ht == 0:
        ht, edge = 1, 0
    if ht == divide:
        ht, edge = divide - 1, 1
    return ht & 63, (divide - ht) & 63, edge, 0


def _milli_phase(phase):
    p = round(phase * 1000)
    return p + 360000 if p < 0 else p


def _drp_phase(divide, phase):
    """(delay_time, phase_mux) of an integer counter (phase in degrees)."""
    fixed = (_milli_phase(phase) << _FRAC) // 1000
    t = _round_frac(fixed * divide // 360, 3)
    return (t >> _FRAC) & 63, (t >> (_FRAC - 3)) & 7


def _drp_frac(divide_f, phase):
    """Fractional counter fields (CLKOUT0 / CLKFBOUT, .125 steps)."""
    d = int(divide_f)
    frac = int(round((divide_f - d) * 8)) & 7
    even = d >> 1
    odd = d - 2 * even
    odd_and_frac = 8 * odd + frac
    lt = even - (odd_and_frac <= 9)
    ht = even - (odd_and_frac <= 8)
    pm_fall = (odd << 2) + (frac >> 1)
    wf_fall = int(2 <= odd_and_frac <= 9 or (frac == 1 and d == 2))
    wf_rise = int(1 <= odd_and_frac <= 8)
    octets = 8 * d + frac
    p = _milli_phase(phase) + 10
    a = p * octets // 360000
    pm_rise = 0 if a & 0xff == 0 else a & 7
    dt = (p * octets // 8) // 360000
    return dict(HIGH_TIME=ht & 63, LOW_TIME=lt & 63, FRAC=frac,
                FRAC_WF_R=wf_rise, FRAC_WF_F=wf_fall, PHASE_MUX=pm_rise,
                PHASE_MUX_F=(pm_fall + pm_rise) & 7, DELAY_TIME=dt & 63)


# Lock / loop filter lookup tables, from clockgen_tables.py.
_TABLES = None
_TABLE_FAMILY = {'MMCME2_ADV': '7s_mmcm', 'PLLE2_ADV': '7s_pll',
                 'MMCME3_ADV': 'us_mmcm', 'PLLE3_ADV': 'us_pll',
                 'MMCME4_ADV': 'usp_mmcm', 'PLLE4_ADV': 'usp_pll'}
# Field layout of the entries (most significant first).
_LOCK_FIELDS = (('LOCK_REF_DLY', 5), ('LOCK_FB_DLY', 5), ('LOCK_CNT', 10),
                ('LOCK_SAT_HIGH', 10), ('UNLOCK_CNT', 10))
_FILTER_FIELDS = (('CP', 4), ('RES', 4), ('LFHF', 2))


def _tables():
    global _TABLES
    if _TABLES is None:
        import json
        import os
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'clockgen_tables.json')
        with open(p) as f:
            _TABLES = json.load(f)
    return _TABLES


def _drp_bits(prefix, name, value, width):
    return [f'{prefix}.DRP.{name}[{i}]' + ('' if (value >> i) & 1 else '=0')
            for i in range(width)]


def _drp_fields(prefix, entry, fields):
    out = []
    shift = sum(w for _, w in fields)
    for name, w in fields:
        shift -= w
        out += _drp_bits(prefix, name, (entry >> shift) & ((1 << w) - 1), w)
    return out


def clockgen_family(bel, sitekeys):
    """Lookup table family of a clock generator BEL: the 7-series BELs are
    named after the primitive, UltraScale(+) ones just MMCM / PLL (the
    architecture then from the slice tile types: CLE_M on UltraScale,
    CLEM on UltraScale+)."""
    if bel in _TABLE_FAMILY:
        return _TABLE_FAMILY[bel]
    if bel not in ('MMCM', 'PLL'):
        return None
    arch = getattr(sitekeys, '_clockgen_arch', None)
    if arch is None:
        types = set(sitekeys.tile_type.values())
        arch = 'usp' if 'CLEM' in types else 'us' if 'CLE_M' in types \
            else ''
        sitekeys._clockgen_arch = arch
    return f'{arch}_{bel.lower()}' if arch else None


def clockgen_drp_features(prefix, cfgs, fam=None):
    """Derived counter / lock / filter register features of an MMCM or PLL
    BEL (<prefix> = <SITEKEY>.<BEL>, <cfgs> = its BEL configuration, <fam>
    its clockgen_tables.json family)."""
    out = []

    def num(k, default=None):
        try:
            return float(cfgs[k])
        except (KeyError, ValueError):
            return default
    counters = []
    for i in range(7):
        d = num(f'CLKOUT{i}_DIVIDE_F') if i == 0 else None
        if d is None:
            d = num(f'CLKOUT{i}_DIVIDE')
        if d is None:
            continue
        counters.append((f'CLKOUT{i}', d, num(f'CLKOUT{i}_DUTY_CYCLE', 0.5),
                         num(f'CLKOUT{i}_PHASE', 0.0)))
    m = num('CLKFBOUT_MULT_F')
    if m is None:
        m = num('CLKFBOUT_MULT')
    if m is not None:
        counters.append(('CLKFBOUT', m, 0.5, num('CLKFBOUT_PHASE', 0.0)))
    dv = num('DIVCLK_DIVIDE')
    if dv is not None:
        counters.append(('DIVCLK', dv, 0.5, 0.0))
    ss = cfgs.get('SS_EN') == 'TRUE'
    for name, d, duty, phase in counters:
        if ss and name in ('CLKOUT2', 'CLKOUT3', 'CLKFBOUT'):
            continue  # spread spectrum programs these counters itself
        # Dynamic (fine) phase shift: the static phase mux stays at 0.
        fine = cfgs.get(f'{name}_USE_FINE_PS') == 'TRUE'
        is_frac = d != int(d)
        # Edge / no count come from the integer part in both modes.
        ht, lt, edge, nc = _drp_divider(int(d), duty)
        dt, pm = _drp_phase(int(d), phase)
        if fine:
            pm = 0
        if name in ('CLKOUT0', 'CLKFBOUT'):
            # Counters with a fractional mode.  Its falling edge phase mux
            # (in the CLKOUT5 / CLKOUT6 registers) repeats the phase mux in
            # integer mode, the wave form bits are 0.
            if is_frac:
                fields = _drp_frac(d, phase)
                ht, lt = fields['HIGH_TIME'], fields['LOW_TIME']
                dt = fields['DELAY_TIME']
                if not fine:
                    pm = fields['PHASE_MUX']
            else:
                fields = dict(FRAC=0, PHASE_MUX_F=pm, FRAC_WF_R=0,
                              FRAC_WF_F=0)
            out += _drp_bits(prefix, f'{name}_FRAC', fields['FRAC'], 3)
            out += _drp_bits(prefix, f'{name}_PHASE_MUX_F',
                             fields['PHASE_MUX_F'], 3)
            for k in ('FRAC_WF_R', 'FRAC_WF_F'):
                out.append(f'{prefix}.DRP.{name}_{k}={fields[k]}')
        out += _drp_bits(prefix, f'{name}_HIGH_TIME', ht, 6)
        out += _drp_bits(prefix, f'{name}_LOW_TIME', lt, 6)
        out += _drp_bits(prefix, f'{name}_DELAY_TIME', dt, 6)
        out += _drp_bits(prefix, f'{name}_PHASE_MUX', pm, 3)
        out.append(f'{prefix}.DRP.{name}_EDGE={edge}')
        out.append(f'{prefix}.DRP.{name}_NO_COUNT={nc}')
        out.append(f'{prefix}.DRP.{name}_FRAC_EN={int(is_frac)}')
    if fam is None:
        fam = _TABLE_FAMILY.get(prefix.rsplit('.', 1)[-1])
    if m is not None and fam:
        # Lock and loop filter tables are looked up from the (integer)
        # multiplier and the bandwidth.
        t = _tables()[fam]
        mi = int(m)
        if 1 <= mi <= len(t['lock']):
            out += _drp_fields(prefix, t['lock'][mi - 1], _LOCK_FIELDS)
        filt = t.get('filter_low' if cfgs.get('BANDWIDTH') == 'LOW'
                     else 'filter_high', t.get('filter'))
        if filt and 1 <= mi <= len(filt):
            out += _drp_fields(prefix, filt[mi - 1], _FILTER_FIELDS)
    return out


def open_any(path):
    if path.endswith('.gz'):
        return gzip.open(path, 'rt')
    return open(path)


def tile_features(path, sitekeys):
    """Returns {tile: set(features)} for one design."""
    return derive(parse_dump(path, sitekeys), sitekeys)


def parse_dump(path, sitekeys):
    """The features read directly from the dump lines, and what the derived
    features need from it: (feats, site_map, glob_opts, bel_cfgs,
    bank_stds).  (designdata caches this per design, keyed by the source
    of this function and of everything it uses: keep derived features in
    derive().)"""
    feats = collections.defaultdict(set)
    site_map = {}
    glob_opts = {}
    bel_cfgs = collections.defaultdict(dict)
    bank_stds = collections.defaultdict(set)
    with open_any(path) as f:
        for line in f:
            p = line.rstrip('\n').split(' ')
            kind = p[0]
            if kind == 'cfg' and len(p) >= 5 and p[2] == 'PAD' and \
                    p[1] not in site_map and p[1] in sitekeys.key:
                # A pad in use without a site of its own in the dump (the N
                # side of a differential input goes through the P site):
                # it is used (no UNUSEDPIN pull), with its own pull setting.
                site_map[p[1]] = sitekeys.key[p[1]]
            # Skip malformed lines (older dumps could misalign values).
            if kind in ('sp', 'cfg') and (len(p) < 5 or p[1] not in site_map):
                continue
            if kind in ('pip', 'site') and len(p) < 4:
                continue
            if kind == 'global':
                glob_opts[p[1]] = p[2]
            elif kind == 'pip':
                tile, w0, w1, d = p[1], p[3], p[4], p[5]
                # d: 1 directional, 2 bidirectional with known direction
                # (w0 drives w1), 0 bidirectional of unknown direction.
                sep = '<->' if d == '0' else '->'
                feats[tile].add(f'{w0}{sep}{w1}')
            elif kind == 'site':
                site, stype = p[1], p[2]
                tile, key = sitekeys.key.get(site, (p[3], site))
                site_map[site] = (tile, key)
                feats[tile].add(f'{key}.TYPE.{stype}')
            elif kind == 'sp':
                tile, key = site_map[p[1]]
                feats[tile].add(f'{key}.{p[2]}.SP.{p[3]}.{p[4]}')
            elif kind == 'bank':
                # I/O standards used in the pad's bank (pad may be unused).
                if len(p) >= 4 and p[1] in sitekeys.key:
                    tile, key = sitekeys.key[p[1]]
                    feats[tile].add(f'{key}.BANK.{p[2]}={p[3]}')
                    if p[2] == 'IOSTD':
                        bank_stds[(tile, key)].add(p[3])
            elif kind == 'cfg':
                tile, key = site_map[p[1]]
                value = ' '.join(p[4:])
                feats[tile].update(cfg_features(f'{key}.{p[2]}', p[3], value))
                bel_cfgs[(tile, f'{key}.{p[2]}')][p[3]] = value
    return feats, site_map, glob_opts, bel_cfgs, bank_stds


def derive(state, sitekeys):
    """Derived features added to parse_dump's state (modified in place);
    returns {tile: set(features)}."""
    feats, site_map, glob_opts, bel_cfgs, bank_stds = state
    # Bank VCCO (bank wide settings such as the 7-series STEPDOWN depend on
    # it rather than on single standards).
    global _VCCO
    if bank_stds and _VCCO is None:
        _VCCO = _std_vcco()
    for (tile, key), stds in bank_stds.items():
        vs = {_VCCO.get(x) for x in stds}
        if len(vs) == 1 and None not in vs:
            feats[tile].add(f'{key}.BANK.VCCO={vs.pop()}')
    # 7-series IOI: an OLOGIC clocked through IOI_OCLK_<n> also gets the
    # OCLKM_<n> mux (its inverted clock) set to the same source, a pip the
    # route does not report (prjxray IOI_OCLKM_1.IOI_LEAF_GCLK5 = 30_30
    # 30_38 30_44: set with GCLK5->OCLK_1 only when OLOGIC1 uses OCLK_1).
    for tile, fs in feats.items():
        # (substring tests first: the patterns match few features)
        if 'IOI_OCLK_' not in '\n'.join(fs):
            continue
        used = {m.group(1) for f in fs if f.startswith('IOI_OCLK_')
                for m in [_OCLK_TO_OLOGIC.match(f)] if m}
        if not used:
            continue
        have_m = {m.group(1) for f in fs if '->IOI_OCLKM_' in f
                  for m in [_TO_OCLKM.match(f)] if m}
        for f in list(fs):
            if '->IOI_OCLK_' not in f:
                continue
            m = _TO_OCLK.match(f)
            if m and m.group(2) in used and m.group(2) not in have_m:
                fs.add(f'{m.group(1)}->IOI_OCLKM_{m.group(2)}')
    # 7-series OLOGIC: one bit per OLOGIC (prjxray "ODDR.DDR_CLK_EDGE.
    # SAME_EDGE", LIOI3 31_92 / 30_35) is set when the clock edge setting
    # and the clock inversion agree: SAME_EDGE (ODDR, and OSERDES / FF
    # which are same edge) with CLK, or OPPOSITE_EDGE with CLK_B (xa7a12t:
    # exact over 41 set / 36 clear samples).  Name the pair.
    for tile, fs in feats.items():
        if '.CLKINV.SP.' not in '\n'.join(fs):
            continue
        for f in [f for f in fs if '.CLKINV.SP.' in f]:
            m = _OLOGIC_CLKINV.match(f)
            if not m:
                continue
            key, inv = m.group(1), m.group(2)
            edge = 'SAME_EDGE'
            if f'{key}.OUTFF.ODDR_CLK_EDGE=OPPOSITE_EDGE' in fs and \
                    f'{key}.OUTFF.OUTFFTYPE=DDR' in fs:
                edge = 'OPPOSITE_EDGE'
            fs.add(f'{key}.CLKINV.SP.{inv}.OUT@CLK_EDGE={edge}')
    # 7-series block RAM port widths as programmed: in simple dual port
    # mode port A only reads and port B only writes, and the full width
    # (36 / 72) splits over both ports' fields; the other width settings
    # are ignored (xa7s15 BRAM_R 27_275-277, upper RAMB18 READ_WIDTH_B:
    # SDP with READ_WIDTH_A=36 -> 18, other SDP -> 0 whatever
    # READ_WIDTH_B).
    for (tile, prefix), cfgs in bel_cfgs.items():
        bel = prefix.rsplit('.', 1)[-1]
        if bel not in ('RAMB18E1', 'RAMB36E1', 'FIFO18E1', 'FIFO36E1'):
            continue
        full = '72' if '36' in bel else '36'
        half = '36' if full == '72' else '18'
        if bel.startswith('FIFO'):
            # A FIFO reads on port A and writes on port B, DATA_WIDTH wide
            # (the full width: simple dual port).
            dw = cfgs.get('DATA_WIDTH')
            if dw is None:
                continue
            eff = {'READ_WIDTH_A': dw, 'WRITE_WIDTH_B': dw,
                   'READ_WIDTH_B': '0', 'WRITE_WIDTH_A': '0'}
            sdp = True
        else:
            eff = {k: cfgs.get(k) for k in ('READ_WIDTH_A', 'READ_WIDTH_B',
                                            'WRITE_WIDTH_A', 'WRITE_WIDTH_B')}
            sdp = cfgs.get('RAM_MODE') == 'SDP'
        r = w = False
        if sdp:
            r, w = eff['READ_WIDTH_A'] == full, eff['WRITE_WIDTH_B'] == full
            eff['READ_WIDTH_B'] = half if r else '0'
            eff['WRITE_WIDTH_A'] = half if w else '0'
            if r:
                eff['READ_WIDTH_A'] = half
            if w:
                eff['WRITE_WIDTH_B'] = half
        feats[tile].add(f'{prefix}.EFF_SDP_READ_FULL={int(r)}')
        feats[tile].add(f'{prefix}.EFF_SDP_WRITE_FULL={int(w)}')
        for k, v in eff.items():
            if v is None:
                continue
            feats[tile].add(f'{prefix}.EFF_{k}={v}')
            # 3 bit width code of an 18 Kb half (prjxray READ_WIDTH_*):
            # 1 (or unused) 0, 2 1, 4 2, 9 3, 18 4.
            enc = {'0': 0, '1': 0, '2': 1, '4': 2, '9': 3, '18': 4}.get(v)
            if full == '36' and enc is not None:
                feats[tile].update(
                    f'{prefix}.EFF_{k}.ENC[{i}]' + ('' if enc >> i & 1
                                                    else '=0')
                    for i in range(3))
    # Block RAM output register settings as programmed: with a full width
    # (simple dual port) read or write port, port B's output register
    # follows port A's (xcku025 BRAM 03_072 = DOB_REG, 03_125 =
    # RSTREG_PRIORITY_B: exact over 1367 lower RAMB18 samples with this
    # rule, ~70 errors without).
    for (tile, prefix), cfgs in bel_cfgs.items():
        if 'DOB_REG' not in cfgs or 'DOA_REG' not in cfgs:
            continue
        full = '72' if '36' in prefix.rsplit('.', 1)[-1] else '36'
        sdp = cfgs.get('RAM_MODE') == 'SDP' or \
            cfgs.get('READ_WIDTH_A') == full or \
            cfgs.get('WRITE_WIDTH_B') == full
        for a, b in (('DOA_REG', 'DOB_REG'),
                     ('RSTREG_PRIORITY_A', 'RSTREG_PRIORITY_B')):
            v = cfgs.get(a if sdp else b)
            if v is not None:
                feats[tile].add(f'{prefix}.EFF_{b}={v}')
    # Clock generator counter registers (derived, see clockgen_drp_features).
    for (tile, prefix), cfgs in bel_cfgs.items():
        if re.search(r'\.(MMCME\d_ADV|PLLE\d_ADV|MMCM|PLL)$', prefix):
            feats[tile].update(clockgen_drp_features(
                prefix, cfgs,
                clockgen_family(prefix.rsplit('.', 1)[-1], sitekeys)))
    # 7-series DSP48E1: the "AREG_0" / "BREG_0" bits (prjxray, DSP_L 27_111
    # / 27_038, 27_271 / 27_198) are set for AREG=0, and for AREG=1 when
    # INMODE[0] (A1/A2 select; INMODE[4] for B) is tied to ground
    # (xa7a12t r9: exact, 93 / 84 set samples).  Name the register setting
    # together with the source of its select input.
    for tile, fs in feats.items():
        if '.DSP48E1.' not in '\n'.join(fs):
            continue
        for f in [f for f in fs if '.DSP48E1.' in f]:
            m = _DSP_REG.match(f)
            if not m:
                continue
            key, port, val = m.group(1), m.group(2), m.group(3)
            k = key[-1]
            pin = f'DSP_{k}_INMODE{0 if port == "A" else 4}'
            src = 'NONE'
            for g in fs:
                if g.endswith('->' + pin):
                    src = ('GND' if '_GND_' in g else 'VCC' if '_VCC_' in g
                           else 'FABRIC')
                    break
            fs.add(f'{key}.DSP48E1.{port}REG={val}@{pin[6:]}={src}')
    # 7-series FIFO almost full / empty offsets are stored adjusted (and
    # inverted): full = ALMOST_FULL_OFFSET + 1 without EN_SYN, empty =
    # ALMOST_EMPTY_OFFSET - 1 without EN_SYN with FIRST_WORD_FALL_THROUGH
    # (xa7s15: 86 FIFO18 samples, prjxray ZALMOST_*_OFFSET bits).  The
    # carry of the adjustment defeats per bit features: also name the
    # stored value (<name>_STORED).
    for (tile, prefix), cfgs in bel_cfgs.items():
        if not prefix.endswith(('.FIFO18E1', '.FIFO36E1')):
            continue
        async_ = cfgs.get('EN_SYN') == 'FALSE'
        fwft = cfgs.get('FIRST_WORD_FALL_THROUGH') == 'TRUE'
        for name, adj in (('ALMOST_FULL_OFFSET', 1 if async_ else 0),
                          ('ALMOST_EMPTY_OFFSET',
                           -1 if async_ and fwft else 0)):
            m = _VEC.match(cfgs.get(name, ''))
            if not m:
                continue
            width = int(m.group(1))
            v = int(m.group(3).replace('_', ''),
                    2 if m.group(2) == 'b' else 16)
            v = (v + adj) % (1 << width)
            feats[tile].update(cfg_features(prefix, name + '_STORED',
                                            f"{width}'h{v:X}"))
    # Settings whose bits depend on another setting of the same BEL (7-series
    # SLEW / DRIVE / IN_TERM bits differ between I/O standard families, DSP
    # register / cascade register pairs): also name them together.
    for (tile, prefix), cfgs in bel_cfgs.items():
        bel = prefix.rsplit('.', 1)[-1]
        for name, cond in _STD_CONDITIONED.get(bel, ()):
            if name in cfgs and cond in cfgs:
                feats[tile].add(f'{prefix}.{name}={cfgs[name]}@{cond}='
                                f'{cfgs[cond]}')
    # The physical layout of some vector settings depends on a width setting
    # of the same BEL (e.g. BRAM INIT_A/SRVAL_A are replicated for narrow
    # READ_WIDTH_A): also name the vector bits together with the width.
    for (tile, prefix), cfgs in bel_cfgs.items():
        widths = {k: v for k, v in cfgs.items()
                  if re.search(r'(^|_)WIDTH(_|$)', k)}
        if not widths:
            continue
        for name, value in cfgs.items():
            m = _VEC.match(value)
            if not m or int(m.group(1)) > MAX_ZERO_VEC:
                continue
            port = name.rsplit('_', 1)[-1] if '_' in name else ''
            for wname, wval in widths.items():
                if len(port) == 1 and not wname.endswith('_' + port):
                    continue
                for f in cfg_features(prefix, name, value):
                    feats[tile].add(f'{f}@{wname}={wval}')
    # A fractured LUT (both the 6LUT and the 5LUT of a site letter in use)
    # holds the O5 function in the low half of the physical truth table:
    # the O6 equation (independent of A6) only describes the high half.
    # Name the physical low half of <x>6LUT.INIT after the O5 equation.
    for (tile, prefix), cfgs in bel_cfgs.items():
        if not prefix.endswith('6LUT') or 'EQN' not in cfgs:
            continue
        o5 = bel_cfgs.get((tile, prefix[:-4] + '5LUT'), {}).get('EQN')
        if o5 is None:
            continue
        o5bits = lut_eqn_bits(o5)
        if o5bits is None:
            continue
        fs = feats[tile]
        for i in range(32):
            fs.discard(f'{prefix}.INIT[{i}]')
        fs.update(f'{prefix}.INIT[{i}]' for i in o5bits if i < 32)
    # Older dumps report a chain carry input (PRECYINIT CIN) even when
    # Vivado routed CI through AX; the real PRECYINIT site pip wins.
    for tile, fs in feats.items():
        cin = [f for f in fs if f.endswith('.PRECYINIT.SP.CIN.OUT')]
        for f in cin:
            pre = f[:-len('CIN.OUT')]
            if any(g.startswith(pre) and g != f for g in fs):
                fs.discard(f)
    # Unused pads take the design wide UNUSEDPIN pull setting (Vivado's
    # default is Pulldown, older dumps do not record it).
    glob_opts.setdefault('UNUSEDPIN', 'Pulldown')
    if 'UNUSEDPIN' in glob_opts:
        v = glob_opts['UNUSEDPIN'].upper()
        for tile, pads in sitekeys.pads.items():
            for s in pads:
                key = sitekeys.key[s][1]
                if s not in site_map:
                    feats[tile].add(f'{key}.UNUSEDPIN={v}')
                    pull = v
                else:
                    pull = bel_cfgs.get((tile, f'{key}.PAD'), {}).get(
                        'PULLTYPE', 'NONE').upper()
                # The pad's pull as programmed, used or not: the pull down
                # is the all-zero setting (xcku025 HPIO_L 04_1858 /
                # 08_1538 / 00_898 set with PULLNONE and PULLUP alike).
                pull = {'PULLNONE': 'NONE', 'PULLUP': 'UP',
                        'PULLDOWN': 'DOWN', 'PULLKEEPER': 'KEEPER'}.get(
                            pull, pull)
                if sitekeys.bonded is not None and \
                        s not in sitekeys.bonded:
                    continue  # (unbonded: no pull programming)
                feats[tile].add(f'{key}.EFF_PULL={pull}')
                feats[tile].add(f'{key}.EFF_PULLDOWN={int(pull == "DOWN")}')
    hclk_row_features(feats, sitekeys)
    # (parked IMUX first: the leaf clock PIPs are implied, not routed)
    parked_imux_features(feats, sitekeys)
    leaf_clock_features(feats, sitekeys)
    return feats


_REGION = re.compile(r'X(\d+)Y(\d+)$')
_HROW_HCLK = re.compile(r'CLK_HROW_CK_HCLK_OUT_([LR])(\d+)->CLK_HROW_CK_BUFHCLK_\1\2$')


def hclk_row_features(feats, sitekeys):
    """7-series: the HCLK_CMT tile of a CMT column enables the horizontal
    clock rows its clock region's HROW drives towards it (xa7s15 CMT
    26_1621 / 27_1621 / 29_1626 = rows 9 / 8 / 11, exact in 80 designs):
    HCLK_CMT.BUFHCLK<n>.ACTIVE when the HROW of the clock region row drives
    CLK_HROW_CK_BUFHCLK_<side><n>, <side> L for the left clock region
    column (X0), R for the right one."""
    rows = collections.defaultdict(set)
    for tile, fs in feats.items():
        if not sitekeys.tile_type.get(tile, '').startswith('CLK_HROW_'):
            continue
        m = _REGION.match(sitekeys.region.get(tile, ''))
        if not m:
            continue
        for f in fs:
            h = _HROW_HCLK.match(f)
            if h:
                rows[int(m.group(2))].add((h.group(1), int(h.group(2))))
    if not rows:
        return
    for tile, tt in sitekeys.tile_type.items():
        if tt not in ('HCLK_CMT', 'HCLK_CMT_L'):
            continue
        m = _REGION.match(sitekeys.region.get(tile, ''))
        if not m:
            continue
        side = 'L' if m.group(1) == '0' else 'R'
        for s, n in rows.get(int(m.group(2)), ()):
            if s == side:
                feats[tile].add(f'HCLK_CMT.BUFHCLK{n}.ACTIVE')


# UltraScale(+) leaf clock buffers of an RCLK_INT tile: X16_0 drives the INT
# rows below the RCLK row (grid y larger), X16_1 those above.
_LEAF = re.compile(r'^CLK_BUFCE_LEAF_X16_([01])_CLK_IN\d+->'
                   r'CLK_BUFCE_LEAF_X16_\1_CLK_OUT(\d+)$')
LEAF_ROWS = 30  # INT rows per half clock region
_INT_COLS = {}


def leaf_clock_features(feats, sitekeys):
    """Adds LEAF_CLK_OUT<k> to every INT tile of the half-column whose
    RCLK_INT leaf clock buffer output k is used: the INTs' clock enable bits
    (xcku025 INT 27_011 / 25_011: output 12, 27_026 / 25_026: output 13)
    follow the leaf buffer, not the INT's own PIPs."""
    key = id(sitekeys)
    cols = _INT_COLS.get(key)
    if cols is None:
        cols = collections.defaultdict(list)  # grid x -> sorted INT grid ys
        for t, tt in sitekeys.tile_type.items():
            if tt == 'INT':
                x, y = sitekeys.xy[t]
                cols[x].append((y, t))
        for v in cols.values():
            v.sort()
        cols = _INT_COLS[key] = dict(cols)
    touched = set()
    for tile, fs in list(feats.items()):
        if not sitekeys.tile_type.get(tile, '').startswith('RCLK_INT'):
            continue
        outs = collections.defaultdict(set)
        for f in fs:
            m = _LEAF.match(f)
            if m:
                outs[m.group(1)].add(m.group(2))
        if not outs:
            continue
        x, y = sitekeys.xy[tile]
        ints = cols.get(x, [])
        below = [t for yy, t in ints if yy > y][:LEAF_ROWS]
        above = [t for yy, t in reversed(ints) if yy < y][:LEAF_ROWS]
        for leaf, tiles in (('0', below), ('1', above)):
            for k in outs.get(leaf, ()):
                for t in tiles:
                    feats[t].add(f'LEAF_CLK_OUT{k}')
                    touched.add(t)
    # A live leaf clock k reaches the INTs on GCLK_B_0_<g(k)>; the INT's
    # global node mux of the lowest numbered node it feeds is set to it
    # whenever no PIP of the INT drives that node (xcku025: 27_011 / 25_011
    # = GCLK_B_0_9 -> INT_NODE_GLOBAL_13_OUT1 / OUT0 for leaf output 12,
    # 3 exceptions in 22253; 27_026 / 25_026 = GCLK_B_0_11 -> node 2 for
    # output 13): the implied PIP is added as if routed.
    feeds = _gclk_feeds(sitekeys)
    for t in touched:
        fs = feats[t]
        used = {f.split('->', 1)[1] for f in fs if '->' in f}
        for f in list(fs):
            if not f.startswith('LEAF_CLK_OUT'):
                continue
            k = int(f[len('LEAF_CLK_OUT'):])
            g = (2 * k) % 16 + (1 if k >= 8 else 0)
            nodes = feeds.get(g)
            if not nodes:
                continue
            low = min(n for n, _ in nodes)
            for n, wire in nodes:
                if n == low and wire not in used:
                    fs.add(f'GCLK_B_0_{g}->{wire}')


_GNODE = re.compile(r'^INT_NODE_GLOBAL_(\d+)_(?:INT_)?OUT\d$')
_FEEDS = {}


def _gclk_feeds(sitekeys):
    """GCLK_B_0_g -> {(node number, node wire)} of the INT tile type (the
    die's PIP list next to its tile list)."""
    key = id(sitekeys)
    if key not in _FEEDS:
        meta = os.path.dirname(os.path.dirname(sitekeys.tiles_tsv))
        name = os.path.splitext(os.path.basename(sitekeys.tiles_tsv))[0]
        out = collections.defaultdict(set)
        path = os.path.join(meta, 'pips', name + '.txt')
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    p = line.split()
                    if len(p) > 3 and p[0] == 'pip' and p[1] == 'INT' and \
                            p[2].startswith('GCLK_B_0_'):
                        m = _GNODE.match(p[3])
                        if m:
                            out[int(p[2].rsplit('_', 1)[1])].add(
                                (int(m.group(1)), p[3]))
        _FEEDS[key] = dict(out)
    return _FEEDS[key]


# Unused INT input multiplexers are parked by Vivado: an IMUX node no PIP
# drives selects its first input by wire name (the all-zero setting, e.g.
# BOUNCE_E_BLS_5_FTN for INT_NODE_IMUX_18), unless that input carries a
# net; then it selects the first input (by name) that carries none.
# xcku025 (Vivado, s315): IMUX_18 with BOUNCE_E_BLS_5 live -> the
# INT_NODE_GLOBAL_7_OUT0 setting without its global stage bit (45_053
# 47_053), with GLOBAL_7 live too -> NN2_E_END5's (45_053 48_052); IMUX_50
# likewise (10_052 11_052 / 11_052 13_052).  Added as PARK.<input>-><node>.
# Live wires: the ends of the tile's PIPs and, through the node map
# (generic/data/int_nodes_<arch>.txt), those of the other INT tiles.
_INT_XY = re.compile(r'^INT_X(\d+)Y(\d+)$')
_IMUX = re.compile(r'^INT_NODE_IMUX_\d+_INT_OUT$')
_PARK = {}


def _park_data(sitekeys):
    """(node map {wire: [(dx, dy, wire)]}, {IMUX node: sorted inputs}) of
    the die's architecture, or None without a node map."""
    key = id(sitekeys)
    if key in _PARK:
        return _PARK[key]
    meta = os.path.dirname(os.path.dirname(sitekeys.tiles_tsv))
    name = os.path.splitext(os.path.basename(sitekeys.tiles_tsv))[0]
    import dies as dieslib
    d = dieslib.load().get(name)
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'data',
        f'int_nodes_{d.arch if d else None}.txt')
    pips = os.path.join(meta, 'pips', name + '.txt')
    if d is None or not os.path.exists(path) or not os.path.exists(pips):
        _PARK[key] = None
        return None
    nodes = collections.defaultdict(list)
    with open(path) as f:
        for line in f:
            if line.startswith('#'):
                continue
            w, dx, dy, w2 = line.split()
            nodes[w].append((int(dx), int(dy), w2))
    inputs = collections.defaultdict(set)
    with open(pips) as f:
        for line in f:
            p = line.split()
            if len(p) > 3 and p[0] == 'pip' and p[1] == 'INT' and \
                    _IMUX.match(p[3]):
                inputs[p[3]].add(p[2])
    _PARK[key] = (dict(nodes), {n: sorted(v) for n, v in inputs.items()})
    return _PARK[key]


def parked_imux_features(feats, sitekeys):
    """Adds PARK.<input>-><IMUX node> to INT tiles (see above);
    URAY_PARK=0 disables it (A/B tests)."""
    if os.environ.get('URAY_PARK', '1') == '0':
        return
    data = _park_data(sitekeys)
    if data is None:
        return
    nodes, inputs = data
    live = collections.defaultdict(set)
    for tile, fs in list(feats.items()):
        if sitekeys.tile_type.get(tile) != 'INT':
            continue
        m = _INT_XY.match(tile)
        if not m:
            continue
        x, y = int(m.group(1)), int(m.group(2))
        for f in fs:
            if '->' not in f:
                continue
            a, b = f.split('->', 1)
            if a.endswith('<'):
                a = a[:-1]
            if '.' in a:  # PARK / site features
                continue
            live[(x, y)].update((a, b))
    spread = collections.defaultdict(set)
    for (x, y), ws in live.items():
        for w in ws:
            for dx, dy, w2 in nodes.get(w, ()):
                spread[(x + dx, y + dy)].add(w2)
    for xy, ws in spread.items():
        live[xy] |= ws
    for (x, y), ws in live.items():
        tile = f'INT_X{x}Y{y}'
        if sitekeys.tile_type.get(tile) != 'INT':
            continue
        fs = feats[tile]
        by = collections.defaultdict(set)  # node -> PIP sources driving it
        for f in fs:
            if '->' in f and not f.startswith('PARK'):
                a, b = f.split('->', 1)
                by[b].add(a.rstrip('<'))
        for node, ins in inputs.items():
            drv = by.get(node, set())
            if drv or ins[0] not in ws:
                continue
            for src in ins[1:]:
                if src not in ws:
                    fs.add(f'PARK.{src}->{node}')
                    break
