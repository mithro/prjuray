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
import re

import numpy as np

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
        for line in open(tiles_tsv):
            p = line.split()
            if p[0] != 'tile':
                continue
            self.tile_type[p[1]] = p[2]
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
    try:
        v = eval(py, {}, dict(_ENV))
    except (SyntaxError, TypeError, NameError):
        return None
    v = np.broadcast_to(np.asarray(v, dtype=np.int64) & 1, (64, ))
    bits = [int(i) for i in np.nonzero(v[:n])[0]]
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


def open_any(path):
    if path.endswith('.gz'):
        return gzip.open(path, 'rt')
    return open(path)


def tile_features(path, sitekeys):
    """Returns {tile: set(features)} for one design."""
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
        used = {m.group(1) for f in fs
                for m in [_OCLK_TO_OLOGIC.match(f)] if m}
        if not used:
            continue
        have_m = {m.group(1) for f in fs for m in [_TO_OCLKM.match(f)] if m}
        for f in list(fs):
            m = _TO_OCLK.match(f)
            if m and m.group(2) in used and m.group(2) not in have_m:
                fs.add(f'{m.group(1)}->IOI_OCLKM_{m.group(2)}')
    # 7-series OLOGIC: one bit per OLOGIC (prjxray "ODDR.DDR_CLK_EDGE.
    # SAME_EDGE", LIOI3 31_92 / 30_35) is set when the clock edge setting
    # and the clock inversion agree: SAME_EDGE (ODDR, and OSERDES / FF
    # which are same edge) with CLK, or OPPOSITE_EDGE with CLK_B (xa7a12t:
    # exact over 41 set / 36 clear samples).  Name the pair.
    for tile, fs in feats.items():
        for f in list(fs):
            m = _OLOGIC_CLKINV.match(f)
            if not m:
                continue
            key, inv = m.group(1), m.group(2)
            edge = 'SAME_EDGE'
            if f'{key}.OUTFF.ODDR_CLK_EDGE=OPPOSITE_EDGE' in fs and \
                    f'{key}.OUTFF.OUTFFTYPE=DDR' in fs:
                edge = 'OPPOSITE_EDGE'
            fs.add(f'{key}.CLKINV.SP.{inv}.OUT@CLK_EDGE={edge}')
    # 7-series DSP48E1: the "AREG_0" / "BREG_0" bits (prjxray, DSP_L 27_111
    # / 27_038, 27_271 / 27_198) are set for AREG=0, and for AREG=1 when
    # INMODE[0] (A1/A2 select; INMODE[4] for B) is tied to ground
    # (xa7a12t r9: exact, 93 / 84 set samples).  Name the register setting
    # together with the source of its select input.
    for tile, fs in feats.items():
        for f in list(fs):
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
                if s not in site_map:
                    feats[tile].add(f'{sitekeys.key[s][1]}.UNUSEDPIN={v}')
    return feats
