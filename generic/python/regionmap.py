# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Vectorised mapping of die bit addresses to tile regions.

A region (tile, type, first frame index, frames, offset, nbits) covers, in
each of its frames, the die bit ids [f * nbf + off, f * nbf + off + n).
RegionMap turns a sorted array of set bit ids into (bit, region) pairs with
numpy (one interval per region frame), replacing per bit Python loops.

Relative bit codes: frame within the region's frame column * CODE_M + bit
offset within the region; name "<frame:02d>_<bit:03d>".
"""
import numpy as np

CODE_M = 1 << 16


def code_name(code):
    f, o = divmod(int(code), CODE_M)
    return f'{f:02d}_{o:03d}'


def name_code(name):
    f, o = name.split('_')
    return int(f) * CODE_M + int(o)


class RegionMap:
    def __init__(self, regions, nbf):
        self.nbf = nbf
        nfr = np.array([r[3] for r in regions], dtype=np.int64)
        fi = np.array([r[2] for r in regions], dtype=np.int64)
        off = np.array([r[4] for r in regions], dtype=np.int64)
        n = np.array([r[5] for r in regions], dtype=np.int64)
        self.nregions = len(regions)
        self.r_fi = fi
        self.r_off = off
        # One interval per (region, frame), region index order.
        reg = np.repeat(np.arange(len(regions), dtype=np.int64), nfr)
        first = np.repeat(np.cumsum(nfr) - nfr, nfr)
        frame = np.repeat(fi, nfr) + (np.arange(len(reg)) - first)
        self.i_reg = reg
        self.i_lo = frame * nbf + off[reg]
        self.i_hi = self.i_lo + n[reg]
        self._names = {}

    def pairs(self, ids):
        """ids: sorted die bit ids.  Returns (pos, reg, code): the index
        into ids, the region and the relative bit code of every (set bit,
        owning region) pair, ordered like the dict of Collector.region_bits
        (regions by first set bit, then region index; bits ascending)."""
        ids = np.asarray(ids, dtype=np.int64)
        lo = np.searchsorted(ids, self.i_lo, 'left')
        hi = np.searchsorted(ids, self.i_hi, 'left')
        cnt = hi - lo
        nz = cnt > 0
        lo, cnt, ireg = lo[nz], cnt[nz], self.i_reg[nz]
        tot = int(cnt.sum())
        start = np.repeat(np.cumsum(cnt) - cnt, cnt)
        pos = np.repeat(lo, cnt) + (np.arange(tot, dtype=np.int64) - start)
        reg = np.repeat(ireg, cnt)
        # First set bit of every region (intervals of one region are
        # contiguous and in frame order, so a minimum per region).
        firstpos = np.full(self.nregions, np.iinfo(np.int64).max,
                           dtype=np.int64)
        np.minimum.at(firstpos, reg, pos)
        order = np.lexsort((pos, reg, firstpos[reg]))
        pos, reg = pos[order], reg[order]
        g = ids[pos]
        f, o = np.divmod(g, self.nbf)
        code = (f - self.r_fi[reg]) * CODE_M + (o - self.r_off[reg])
        return pos, reg, code

    def names(self, codes):
        """Relative bit names of codes (memoised)."""
        tab = self._names
        uniq, inv = np.unique(codes, return_inverse=True)
        names = np.empty(len(uniq), dtype=object)
        for i, c in enumerate(uniq.tolist()):
            s = tab.get(c)
            if s is None:
                s = tab[c] = code_name(c)
            names[i] = s
        return names[inv.reshape(-1)].tolist()

    def owned(self, ids, pos=None):
        """Boolean mask: ids[i] lies in some region."""
        if pos is None:
            pos = self.pairs(ids)[0]
        m = np.zeros(len(ids), dtype=bool)
        m[pos] = True
        return m
