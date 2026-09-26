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
    return [f'{prefix}.{name}={value}']


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
    with open_any(path) as f:
        for line in f:
            p = line.rstrip('\n').split(' ')
            kind = p[0]
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
            elif kind == 'cfg':
                tile, key = site_map[p[1]]
                value = ' '.join(p[4:])
                feats[tile].update(cfg_features(f'{key}.{p[2]}', p[3], value))
                bel_cfgs[(tile, f'{key}.{p[2]}')][p[3]] = value
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
