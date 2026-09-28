#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Primitive (UNISIM library cell) information and parameter randomisation.

Reads the output of generic/tcl/dump_prims.tcl.
"""
import collections
import re


def _tcl_list(s):
    """Very small Tcl list parser (handles {} grouping)."""
    out = []
    i = 0
    n = len(s)
    while i < n:
        while i < n and s[i] == ' ':
            i += 1
        if i >= n:
            break
        if s[i] == '{':
            depth = 1
            j = i + 1
            while j < n and depth:
                if s[j] == '{':
                    depth += 1
                elif s[j] == '}':
                    depth -= 1
                j += 1
            out.append(s[i + 1:j - 1])
            i = j
        else:
            j = s.find(' ', i)
            if j < 0:
                j = n
            out.append(s[i:j])
            i = j
    return out


class Prim:
    def __init__(self, name, group, subgroup, pins):
        self.name = name
        self.group = group
        self.subgroup = subgroup
        self.pins = pins  # list of (dir, name)
        self.props = {}  # name -> (default, values list)

    def inputs(self):
        return [n for d, n in self.pins if d == 'IN']

    def outputs(self):
        return [n for d, n in self.pins if d == 'OUT']

    def inouts(self):
        return [n for d, n in self.pins if d == 'INOUT']


def load(path):
    prims = {}
    for line in open(path):
        line = line.rstrip('\n')
        if line.startswith('prim '):
            p = line.split(' ')
            pins = [tuple(x.split(':', 1)) for x in p[4:]]
            prims[p[1]] = Prim(p[1], p[2], p[3], pins)
        elif line.startswith('prop '):
            _, prim, rest = line.split(' ', 2)
            name, rest = rest.split(' ', 1)
            parts = _tcl_list(rest)
            default = parts[0] if parts else ''
            values = _tcl_list(parts[1]) if len(parts) > 1 else []
            prims[prim].props[name] = (default, values)
    # Properties present on (almost) every primitive are Vivado tool
    # properties, not primitive parameters.
    count = collections.Counter()
    for p in prims.values():
        count.update(p.props.keys())
    generic = {k for k, v in count.items() if v >= 0.8 * len(prims)}
    for p in prims.values():
        p.params = {k: v for k, v in p.props.items() if k not in generic}
    return prims


_VEC = re.compile(r"^(\d+)'([bhd])([0-9a-fA-FxX_]+)$")


def random_value(rng, default, values):
    """Returns a random legal-looking value for a parameter, or None to leave
    it at its default."""
    if values:
        if len(values) == 1 and ' to ' in values[0]:
            lo, hi = values[0].split(' to ')
            m = _VEC.match(lo)
            if m:
                return random_value(rng, lo, [])
            if '.' in lo or '.' in hi or '.' in default:
                try:
                    lo_f, hi_f = float(lo), float(hi)
                except ValueError:
                    return None
                return f'{rng.uniform(lo_f, hi_f):.3f}'
            try:
                lo, hi = int(lo), int(hi)
            except ValueError:
                return None
            return str(rng.randint(lo, min(hi, lo + 4096)))
        vals = [v for v in values if ' to ' not in v and v != '']
        if not vals:
            return None
        return rng.choice(vals)
    m = _VEC.match(default)
    if m:
        width = int(m.group(1))
        base = m.group(2)
        v = rng.getrandbits(width)
        if base == 'b':
            return f"{width}'b{v:0{width}b}"
        return f"{width}'h{v:0{(width + 3) // 4}X}"
    return None
