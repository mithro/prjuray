#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Tile grid (tile -> configuration frame region) inference.

The geometry within a clock region row is architectural:

  arch            rows/CR  bits/row  centre (HCLK/RCLK) bits   words/frame
  Series7           50        64          32                     101
  UltraScale        60        64          96                     123
  UltraScalePlus    60        48          96                      93

A tile anchored at INT row r (counted from the bottom of its clock region)
starts at bit r*bits_per_row (+ centre bits if r >= rows/2).  Tiles in the
HCLK/RCLK row own the centre bits.

The frame row of every clock region row and the frame column ("major") of
every grid column are inferred from fuzzing designs: a tile's usage pattern
across designs must match the change pattern of its window in the frame
column it belongs to.
"""
import argparse
import collections
import json
import os
import re

import numpy as np

import bitstream
import designdata as DD
import dies as dieslib
import features as featlib

GEOM = {
    'Series7': dict(rows=50, bpr=64, centre=32),
    'UltraScale': dict(rows=60, bpr=64, centre=96),
    'UltraScalePlus': dict(rows=60, bpr=48, centre=96),
}

INT_TYPES = re.compile(r'^INT(_L|_R)?$')
CENTRE_TYPES = re.compile(r'^(HCLK|RCLK)')


class Grid:
    def __init__(self, die):
        self.die = die
        self.arch = die.arch
        g = GEOM[die.arch]
        self.rows_per_cr = g['rows']
        self.bpr = g['bpr']
        self.centre = g['centre']
        self.frame_bits = bitstream.ARCHES[die.arch]['words'] * 32
        self.tiles = {}
        self.at = {}
        # tile type -> (first bit, end bit) relative to the tile's grid row
        # (row_bit), overriding the structural window (see windows.py)
        self.type_windows = {}
        self.probe = False
        for line in open(die.tiles_tsv):
            p = line.split()
            if p[0] != 'tile':
                continue
            t = dict(name=p[1],
                     type=p[2],
                     gx=int(p[3]),
                     gy=int(p[4]),
                     cr=p[5],
                     sites=p[6])
            self.tiles[p[1]] = t
            self.at[(t['gx'], t['gy'])] = p[1]
        self._rows()

    def _rows(self):
        # Clock region row (Y) of each grid row, by majority of sited tiles.
        votes = collections.defaultdict(collections.Counter)
        int_rows = set()
        centre_rows = set()
        for t in self.tiles.values():
            if t['cr'] != '-':
                y = int(t['cr'].split('Y')[1])
                votes[t['gy']][y] += 1
            if INT_TYPES.match(t['type']):
                int_rows.add(t['gy'])
            if CENTRE_TYPES.match(t['type']):
                centre_rows.add(t['gy'])
        ys = sorted({t['gy'] for t in self.tiles.values()})
        known = {gy: v.most_common(1)[0][0] for gy, v in votes.items()}
        crrow = {}
        for gy in ys:
            if gy in known:
                crrow[gy] = known[gy]
            else:
                # nearest grid row with a known clock region
                best = min(known, key=lambda k: abs(k - gy))
                crrow[gy] = known[best]
        self.crrow = crrow
        # INT row index, from the bottom of each clock region row.
        self.rowidx = {}
        per_cr = collections.defaultdict(list)
        for gy in int_rows:
            per_cr[crrow[gy]].append(gy)
        for cr, lst in per_cr.items():
            for i, gy in enumerate(sorted(lst, reverse=True)):
                self.rowidx[gy] = i
        self.centre_rows = centre_rows
        self.int_rows = int_rows

    def row_bit(self, gy):
        """First bit of grid row gy within its clock region row's frames:
        INT rows by their index, the centre (HCLK / RCLK) row at the centre
        bits; None for other rows (e.g. breaks between clock regions)."""
        half = self.rows_per_cr // 2
        if gy in self.rowidx:
            r = self.rowidx[gy]
            return r * self.bpr + (self.centre if r >= half else 0)
        if gy in self.centre_rows:
            return half * self.bpr
        return None

    def tile_window(self, name):
        """(clock region row, first bit, number of bits) or None: the
        structural window, widened to the learnt window of the tile type
        (type_windows, clipped to the frame unless probing)."""
        w = self.structural_window(name)
        t = self.tiles[name]
        tw = self.type_windows.get(t['type'])
        base = self.row_bit(t['gy'])
        if tw is None or base is None:
            return w
        lo, hi = base + tw[0], base + tw[1]
        if not self.probe:
            # Never over the structural window of another configurable tile
            # (with sites, of a type without a learnt window) of the grid
            # column: e.g. an I/O tile variant of a bank must not take the
            # rows of the plain I/O tiles around it (whose bank wide
            # features made its learnt window wide).
            for n in self._column_owners(t):
                ow = self.structural_window(n)
                if ow is None or ow[0] != self.crrow[t['gy']]:
                    continue
                o_lo, o_hi = ow[1], ow[1] + ow[2]
                if o_hi <= base:
                    lo = max(lo, o_hi)
                elif o_lo >= base:
                    hi = min(hi, o_lo)
        if w is not None:
            lo, hi = min(lo, w[1]), max(hi, w[1] + w[2])
        if not self.probe:
            lo, hi = max(lo, 0), min(hi, self.frame_bits)
        return self.crrow[t['gy']], lo, hi - lo

    def _column_owners(self, t):
        """Tiles of t's grid column (other than t) with sites whose type has
        no learnt window."""
        if not hasattr(self, '_col_tiles'):
            self._col_tiles = collections.defaultdict(list)
            for n, x in self.tiles.items():
                if x['type'] != 'NULL' and x['sites'] != '-':
                    self._col_tiles[x['gx']].append(n)
        return [n for n in self._col_tiles[t['gx']]
                if n != t['name']
                and self.tiles[n]['type'] not in self.type_windows]

    def structural_window(self, name):
        """The tile's INT row and the empty grid rows above it; the centre
        bits for HCLK / RCLK tiles in the centre row."""
        t = self.tiles[name]
        if t['type'] == 'NULL':
            return None
        gy = t['gy']
        if gy in self.centre_rows and CENTRE_TYPES.match(t['type']):
            half = self.rows_per_cr // 2
            return self.crrow[gy], half * self.bpr, self.centre
        if gy not in self.rowidx:
            return None
        r = self.rowidx[gy]
        # Height: grid rows above the anchor occupied by nothing else.
        h = 1
        cr = self.crrow[gy]
        y = gy - 1
        while (y in self.rowidx and self.crrow[y] == cr and
               self.tiles.get(self.at.get((t['gx'], y)),
                              {}).get('type', 'NULL') == 'NULL'):
            h += 1
            y -= 1
        off = r * self.bpr
        half = self.rows_per_cr // 2
        if r >= half:
            off += self.centre
        end = (r + h) * self.bpr + (self.centre if r + h > half else 0)
        return cr, off, end - off


def probe_types(grid):
    """Tile types (with sites) whose structural window is doubtful: taller
    than one INT row, in the centre (HCLK / RCLK) row, or without a window
    although the tile has a grid row with bits; only hard blocks (at most
    PROBE_PER_ROW tiles per clock region row on average), whose windows are
    cheap to learn and which the structural rule does not describe."""
    out = collections.Counter()
    for name, t in grid.tiles.items():
        if t['type'] == 'NULL' or t['sites'] == '-':
            continue
        w = grid.structural_window(name)
        if w is None:
            if grid.row_bit(t['gy']) is not None:
                out[t['type']] += 1
        elif w[2] > grid.bpr or CENTRE_TYPES.match(t['type']):
            out[t['type']] += 1
    nrows = len(set(grid.crrow.values()))
    return sorted(t for t, n in out.items() if n <= PROBE_PER_ROW * nrows)


PROBE_PER_ROW = 3


def frame_columns(dframes):
    """{(block, half, row): [(col, first frame index, nframes), ...]}"""
    cols = collections.OrderedDict()
    for i, f in enumerate(dframes.frames):
        blk, half, row, col, minor = bitstream.far_fields(dframes.arch, f)
        key = (blk, half, row)
        lst = cols.setdefault(key, [])
        if lst and lst[-1][0] == col:
            lst[-1][2] += 1
        else:
            lst.append([col, i, 1])
    return cols


def activity(die, dframes, design_root, maxd=192):
    """Design activity: (act, use, number of designs).
    act[frame index, bit offset, word] = mask of the designs in which that
    bit differs from the baseline; use[tile] = mask of the designs using the
    tile.  Up to maxd designs, newest tags first (later tags use larger
    parts of the die and better generators)."""
    sk = featlib.SiteKeys(die.tiles_tsv)
    roots = design_root.split(',') if isinstance(design_root, str) \
        else list(design_root)
    dirs = []
    for r in reversed(roots):
        dirs += DD.design_dirs(r)
    dirs = dirs[:maxd]
    D = len(dirs)
    NW = max(1, (D + 63) // 64)
    act = np.zeros((len(dframes.frames), dframes.wpf * 32, NW),
                   dtype=np.uint64)
    flat = act.reshape(-1, NW)
    use = collections.defaultdict(int)
    for i, d in enumerate(dirs):
        b = DD.load_bits(dframes, d)
        diff = np.setxor1d(b, dframes.base, assume_unique=True)
        flat[diff, i // 64] |= np.uint64(1 << (i % 64))
        for t, fs in DD.load_features(d, sk).items():
            # Design wide pseudo features (unused pad pulls) do not mean the
            # tile is used.
            if any('.UNUSEDPIN=' not in f for f in fs):
                use[t] |= 1 << i
    return act, use, D


def collect(die, design_root, verbose=False):
    """Design activity evidence for the frame row / frame column learners.

    Returns a dict:
      crmap:  clock region row -> (block, half, row) of its frames
      scores: clock region row -> {grid x: {frame column index in the
              row's block 0 list: score}} (sum over the used tiles of the
              grid column of their near perfect usage / activity matches)
      bram1:  (clock region row, grid x) -> Counter(block 1 column)
      ndesigns: number of designs
    """
    grid = Grid(die)
    dframes = DD.DieFrames(die)
    act, use, D = activity(die, dframes, design_root)
    NW = act.shape[2]

    def to_int(words):
        v = 0
        for k, w in enumerate(words):
            v |= int(w) << (64 * k)
        return v

    cols = frame_columns(dframes)
    block0 = {k: v for k, v in cols.items() if k[0] == 0}

    def col_score(u, col, lo, n):
        """Similarity between a tile usage mask and a column window: the
        better of the whole window change pattern and the best single frame
        change pattern (tiles sharing a column use different frames)."""
        _, first, nfr = col
        masks = np.bitwise_or.reduce(act[first:first + nfr, lo:lo + n],
                                     axis=1)
        best = 0.0
        whole = 0
        for m in set(to_int(x) for x in masks):
            whole |= m
            if m:
                best = max(best, bin(u & m).count('1') / bin(u | m).count('1'))
        if whole:
            best = max(best,
                       bin(u & whole).count('1') / bin(u | whole).count('1'))
        return best

    # Candidate (tile, window) pairs from used tiles: need a usage pattern
    # that is informative (used in some but not almost all designs).
    tiles_used = []
    for t, u in use.items():
        n = bin(u).count('1')
        if n < 1 or n > 0.85 * D:
            continue
        w = grid.tile_window(t)
        if w is None:
            continue
        tiles_used.append((t, u, w))
    # 1. Frame row of each clock region row: vote with sparse used tiles.
    rowvote = collections.defaultdict(collections.Counter)
    for t, u, (cr, lo, n) in tiles_used:
        if bin(u).count('1') > 0.5 * D:
            continue
        # Best score per frame row; vote only when one row clearly wins.
        per_row = {}
        for key, clist in block0.items():
            per_row[key] = max(col_score(u, col, lo, n) for col in clist)
        ranked = sorted(per_row.items(), key=lambda kv: -kv[1])
        if not ranked or ranked[0][1] <= 0.9:
            continue
        if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 0.05:
            continue
        rowvote[cr][ranked[0][0]] += 1
    crmap = {cr: v.most_common(1)[0][0] for cr, v in rowvote.items()}
    # 2. Score every (grid column, frame column) pair of each clock region
    # row.
    scores = {}
    for t, u, (cr, lo, n) in tiles_used:
        key = crmap.get(cr)
        if key is None:
            continue
        clist = block0[key]
        gx = grid.tiles[t]['gx']
        v = np.array([col_score(u, col, lo, n) for col in clist])
        row = scores.setdefault(cr, {})
        acc = row.setdefault(gx, np.zeros(len(clist)))
        acc += (v >= 0.95) * v
    # 3. Block RAM content columns (block type 1), same scoring.
    votes1 = collections.defaultdict(collections.Counter)
    for t, u, (cr, lo, n) in tiles_used:
        tt = grid.tiles[t]
        if not ('RAMB36' in tt['sites'] or 'RAMBFIFO36' in tt['sites']):
            continue
        key = crmap.get(cr)
        if key is None:
            continue
        clist = cols.get((1, key[1], key[2]), [])
        best = None
        for col in clist:
            sc = col_score(u, col, lo, n)
            if best is None or sc > best[0]:
                best = (sc, col[0])
        if best and best[0] > 0.9:
            votes1[(cr, tt['gx'])][best[1]] += 1
    if verbose:
        print('frame rows', crmap)
    return dict(crmap=crmap, scores=scores, bram1=votes1, ndesigns=D)


def save_evidence(ev, path):
    out = dict(
        ndesigns=ev['ndesigns'],
        crmap={str(cr): list(k) for cr, k in ev['crmap'].items()},
        scores={
            str(cr): {
                str(gx): {str(j): round(float(x), 4)
                          for j, x in enumerate(v) if x}
                for gx, v in row.items()
            }
            for cr, row in ev['scores'].items()
        },
        bram1=[[cr, gx, dict((str(c), n) for c, n in v.items())]
               for (cr, gx), v in ev['bram1'].items()])
    with open(path, 'w') as f:
        json.dump(out, f, sort_keys=True)


def load_evidence(path, cols):
    """Inverse of save_evidence; score vectors are rebuilt with the length of
    the row's frame column list."""
    with open(path) as f:
        d = json.load(f)
    crmap = {int(cr): tuple(k) for cr, k in d['crmap'].items()}
    scores = {}
    for cr, row in d['scores'].items():
        cr = int(cr)
        M = len(cols[crmap[cr]])
        scores[cr] = {}
        for gx, sv in row.items():
            v = np.zeros(M)
            for j, x in sv.items():
                v[int(j)] = x
            scores[cr][int(gx)] = v
    bram1 = collections.defaultdict(collections.Counter)
    for cr, gx, v in d['bram1']:
        bram1[(cr, gx)].update({int(c): n for c, n in v.items()})
    return dict(crmap=crmap, scores=scores, bram1=bram1,
                ndesigns=d['ndesigns'])


def assign_activity(grid, cols, ev, verbose=False, fill=True):
    """Frame column of each grid column from design activity alone: per
    clock region row a monotonic assignment maximising the activity scores,
    then propagation between rows and gap filling."""
    crmap = ev['crmap']
    block0 = {k: v for k, v in cols.items() if k[0] == 0}
    colmap = {}
    for cr, scores in ev['scores'].items():
        key = crmap.get(cr)
        if key is None:
            continue
        clist = block0[key]
        M = len(clist)
        gxs = sorted(scores)
        # DP: best[j] = best total with last used major <= j.
        prev = np.zeros(M)
        choice = []
        for gx in gxs:
            sc = scores[gx]
            # take major j for this column: prev_best(<= j) + sc[j]
            pm = np.maximum.accumulate(prev)
            take = pm + sc
            cur = np.maximum(pm, take)
            choice.append((pm, take))
            prev = cur
        # backtrack
        j = int(np.argmax(np.maximum.accumulate(prev)))
        for idx in range(len(gxs) - 1, -1, -1):
            pm, take = choice[idx]
            gx = gxs[idx]
            if take[j] >= pm[j] and scores[gx][j] > 0:
                colmap[(cr, gx)] = clist[j][0]
            # previous columns must use majors <= j
            j = int(np.argmax(pm[:j + 1] == pm[j])) if pm[j] > 0 else j
    if fill:
        propagate_columns(grid, crmap, colmap, block0, verbose)
        fill_gaps(grid, crmap, colmap, block0, verbose)
    colmap1 = {k: v.most_common(1)[0][0] for k, v in ev['bram1'].items()}
    if verbose:
        print('frame rows', crmap)
        print('columns', len(colmap))
    return colmap, colmap1


def propagate_columns(grid, crmap, colmap, block0, verbose=False):
    """A grid column unassigned in one clock region row takes the frame
    column index it has in other rows (the column structure repeats), as
    long as that keeps the row's assignment monotonic."""
    by_gx = collections.defaultdict(collections.Counter)
    for (cr, gx), m in colmap.items():
        by_gx[gx][m] += 1
    gx_types = collections.defaultdict(set)
    for t in grid.tiles.values():
        if t['type'] != 'NULL':
            gx_types[(grid.crrow.get(t['gy']), t['gx'])].add(t['type'])
    added = 0
    for cr, key in crmap.items():
        majors = {c[0] for c in block0[key]}
        row = {gx: m for (c, gx), m in colmap.items() if c == cr}
        for gx, cnt in by_gx.items():
            if gx in row or not gx_types.get((cr, gx)):
                continue
            m = cnt.most_common(1)[0][0]
            if m not in majors:
                continue
            lower = [mm for g, mm in row.items() if g < gx]
            upper = [mm for g, mm in row.items() if g > gx]
            if (lower and max(lower) > m) or (upper and min(upper) < m):
                continue
            colmap[(cr, gx)] = m
            row[gx] = m
            added += 1
    if verbose:
        print('propagated columns', added)


def fill_gaps(grid, crmap, colmap, block0, verbose=False):
    """Assign frame columns no design exercised: between two assigned
    (grid column, frame column) pairs, unassigned frame columns go in order
    to unassigned grid columns holding tiles, when the counts agree."""
    NOCFG = re.compile(r'^(NULL|VBRK.*|.*TERM.*|INT_FEEDTHRU.*|BRKH.*)$')
    for cr, key in crmap.items():
        majors = [c[0] for c in block0[key]]
        assigned = sorted((gx, m) for (c, gx), m in colmap.items() if c == cr)
        if not assigned:
            continue
        used_m = {m for _, m in assigned}
        gxs_cfg = sorted({
            t['gx']
            for n, t in grid.tiles.items()
            if grid.crrow.get(t['gy']) == cr and not NOCFG.match(t['type'])
        })
        bounds = [(-1, -1)] + assigned + [(10**9, 10**9)]
        for (g1, m1), (g2, m2) in zip(bounds, bounds[1:]):
            free_m = [m for m in majors if m1 < m < m2 and m not in used_m]
            free_g = [g for g in gxs_cfg if g1 < g < g2 and (cr, g) not in
                      colmap]
            if not free_m:
                continue
            if len(free_m) == len(free_g):
                for g, m in zip(free_g, free_m):
                    colmap[(cr, g)] = m
            elif verbose:
                print('gap', cr, (g1, m1), (g2, m2), 'majors', free_m,
                      'columns', free_g)


def virtual_bram_shift(clist, colmap, cr, gxs, extra):
    """A clock region row with more BRAM content frame columns than BRAM
    grid columns (e.g. the PS replaces part of the fabric): the extra frame
    columns belong to frame columns of block 0 without grid columns whose
    frame count is that of the BRAM columns' frame columns.  Returns the
    number of extra columns before the first BRAM grid column when that is
    unambiguous (all candidates on one side), else None."""
    idx = {c[0]: j for j, c in enumerate(clist)}
    real = [idx.get(colmap.get((cr, gx))) for gx in gxs]
    if None in real:
        return None
    nfs = {clist[j][2] for j in real}
    used = {idx[m] for (c, _), m in colmap.items() if c == cr and m in idx}
    cand = [j for j, c in enumerate(clist) if j not in used and c[2] in nfs]
    left = [j for j in cand if j < min(real)]
    right = [j for j in cand if j > max(real)]
    if len(left) + len(right) != len(cand):
        return None
    if len(left) >= extra and not right:
        return extra
    if len(right) >= extra and not left:
        return 0
    return None


CORE_SHARE = 0.2  # rows with this share of a type's busiest row are core


def resolve_overlaps(grid, out, verbose=False):
    """Tiles with learnt windows (a hard block stack, e.g. the 7-series CMT
    column: CMT_TOP_*, HCLK_CMT, CMT_FIFO) may overlap in their frame
    column.  Among the tiles overlapping each other, each tile's core is the
    hull of the rows where its type has at least CORE_SHARE of the features
    of its busiest row (learnt row feature counts, relative to the tile's
    grid row), and its structural window.  Centre row tiles (HCLK / RCLK)
    keep their centre bits; the other tiles take their rows in the order of
    their types' feature counts: the contiguous run of core rows not taken
    yet and not in the structural window of another tile of the same grid
    column (tile heights stand), holding most of its structural rows, or no
    region at all (e.g. the FIFO tiles next to a CMT stack, whose rows the
    MMCM / PLL tiles own).  Rows of the original windows nobody took then
    go to the adjacent tile that had them."""
    bpr = grid.bpr
    half = grid.rows_per_cr // 2
    cbits = half * bpr  # first centre bit

    def row_of(b):
        """INT row of a bit (-1: the centre bits)"""
        if b < cbits:
            return b // bpr
        if b < cbits + grid.centre:
            return -1
        return (b - grid.centre) // bpr

    def bit_of(row):
        return row * bpr + (grid.centre if row >= half else 0)

    groups = collections.defaultdict(list)
    for name, e in out.items():
        tw = grid.type_windows.get(e['type'])
        if tw is None or len(tw) < 3 or not tw[2]:
            continue
        base = grid.row_bit(e['gy'])
        for r in e['bits']:
            if r['block'] == 0:
                groups[(r['base'], r['half'], r['row'])].append(
                    (name, r, row_of(base) if base is not None else None,
                     tw[2]))
    dropped = changed = 0
    for key, tiles in groups.items():
        # only tiles overlapping another one of the group
        tiles = [t for t in tiles if any(
            u is not t and t[1]['offset'] < u[1]['offset'] + u[1]['nbits']
            and u[1]['offset'] < t[1]['offset'] + t[1]['nbits']
            for u in tiles)]
        if len(tiles) < 2:
            continue
        taken = set()
        order = []
        for t in tiles:
            name, r = t[0], t[1]
            if grid.tiles[name]['gy'] in grid.centre_rows and \
                    CENTRE_TYPES.match(grid.tiles[name]['type']):
                w = grid.structural_window(name)
                if (r['offset'], r['nbits']) != (w[1], w[2]):
                    r['offset'], r['nbits'] = w[1], w[2]
                    changed += 1
            elif t[2] is not None and t[2] >= 0:
                order.append(t)
        order.sort(key=lambda x: -sum(x[3].values()))
        span = {}

        def lo_row0(r):
            return row_of(r['offset'])

        def hi_row0(r):
            h = row_of(r['offset'] + r['nbits'] - 1)
            return h if h >= 0 else half - 1

        # Rows of the structural windows (tile heights) of the tiles: never
        # taken by another tile of the same grid column.
        srows = {}
        for name, r, row0, hist in order:
            w = grid.structural_window(name)
            srows[name] = set()
            if w is not None:
                srows[name] = {row_of(b) for b in range(w[1], w[1] + w[2])} \
                    - {-1}
        for name, r, row0, hist in order:
            gx = grid.tiles[name]['gx']
            fenced = set()
            for other, rows in srows.items():
                if other != name and grid.tiles[other]['gx'] == gx:
                    fenced |= rows
            top = max(hist.values())
            core = [int(k) for k, n in hist.items() if n >= CORE_SHARE * top]
            lo_row = max(row_of(r['offset']), row0 + min(core))
            hi_row = min(row_of(r['offset'] + r['nbits'] - 1) if
                         row_of(r['offset'] + r['nbits'] - 1) >= 0 else half - 1,
                         row0 + max(core))
            lo_row = min(lo_row, min(srows[name], default=lo_row))
            hi_row = max(hi_row, max(srows[name], default=hi_row))
            runs = []
            for row in range(lo_row, hi_row + 1):
                if row in taken or row in fenced:
                    continue
                if runs and row == runs[-1][-1] + 1:
                    runs[-1].append(row)
                else:
                    runs.append([row])
            if not runs:
                out[name]['bits'].remove(r)
                dropped += 1
                continue
            # the run holding most of its structural rows, else the longest
            best = max(runs, key=lambda x: (len(srows[name] & set(x)),
                                            len(x)))
            taken.update(best)
            span[name] = [best[0], best[-1], r, fenced,
                          set(range(lo_row0(r), hi_row0(r) + 1))]
        # Rows of the original (overlapping) windows nobody took go to the
        # adjacent tile that had them, so no bit loses its owners.
        grown = True
        while grown:
            grown = False
            for name, sp in span.items():
                lo_r, hi_r, r, fenced, orig = sp
                for row, side in ((lo_r - 1, 0), (hi_r + 1, 1)):
                    if row in orig and row not in taken and \
                            row not in fenced:
                        taken.add(row)
                        sp[side] = row
                        grown = True
        for name, (lo_r, hi_r, r, fenced, orig) in span.items():
            lo = bit_of(lo_r)
            hi = bit_of(hi_r) + bpr
            # a run across the centre includes the centre bits
            lo = max(r['offset'], lo)
            hi = min(r['offset'] + r['nbits'], hi)
            if (lo, hi - lo) != (r['offset'], r['nbits']):
                r['offset'], r['nbits'] = lo, hi - lo
                changed += 1
    if verbose:
        print(f'overlaps: {changed} regions narrowed, {dropped} dropped')


def edge_variants(grid, out, verbose=False):
    """Tile types with sites found only in the bottom and the top INT row of
    clock region rows (e.g. the 7-series single I/O tiles LIOB33_SING,
    LIOI3_SING: half of a two row I/O tile, the bottom one holding the
    upper half's pad, the top one the lower half's) have a different
    layout at each edge, which one bit database entry cannot describe: the
    top instances get their own type, <type>@TOP."""
    rows = collections.defaultdict(set)
    sited = set()
    for name, t in grid.tiles.items():
        r = grid.rowidx.get(t['gy'])
        if r is not None:
            rows[t['type']].add(r)
        if t['sites'] != '-':
            sited.add(t['type'])
    top = grid.rows_per_cr - 1
    split = {t for t, r in rows.items() if t in sited and r == {0, top}}
    for name, t in grid.tiles.items():
        if t['type'] in split and grid.rowidx.get(t['gy']) == top:
            out[name]['type'] = t['type'] + '@TOP'
    if verbose and split:
        print('edge variants', ' '.join(sorted(split)))


def build(grid, dframes, cols, crmap, colmap, colmap1, verbose=False,
          tilemap=None, frame_caps=None):
    """Returns the tilegrid dict: tile -> {type, gx, gy, bits: [...]}.
    crmap: clock region row -> (block, half, row); colmap: (clock region
    row, grid x) -> block 0 frame column; colmap1: (clock region row, grid
    x) -> block 1 (BRAM content) frame column learnt from activity (used when
    the BRAM columns cannot be mapped by rank); tilemap: tile -> block 0
    frame column overriding its grid column's."""
    tilemap = tilemap or {}
    # tile type -> frames it uses of its (shared) frame column
    frame_caps = frame_caps or {}
    colinfo = {}
    for key, clist in cols.items():
        for col, first, nfr in clist:
            colinfo[(key, col)] = (first, nfr)
    out = {}
    for name, t in grid.tiles.items():
        w = grid.tile_window(name)
        entry = dict(type=t['type'], gx=t['gx'], gy=t['gy'], bits=[])
        if w is not None:
            cr, lo, n = w
            key = crmap.get(cr)
            col = tilemap.get(name, colmap.get((cr, t['gx'])))
            if key is not None and col is not None:
                first, nfr = colinfo[(key, col)]
                nfr = min(nfr, frame_caps.get(t['type'], nfr))
                entry['bits'].append(
                    dict(block=key[0],
                         half=key[1],
                         row=key[2],
                         col=col,
                         base=f'0x{dframes.frames[first]:08x}',
                         frames=nfr,
                         offset=lo,
                         nbits=n))
        out[name] = entry
    resolve_overlaps(grid, out, verbose)
    edge_variants(grid, out, verbose)
    # Block RAM content frames (block type 1): the k-th BRAM column of a
    # clock region row owns the k-th block type 1 frame column of that row.
    # Frame columns may also exist for BRAM grid columns of other rows (or
    # none: e.g. where the PS replaces the BRAM tiles); then the die wide
    # BRAM column list is used, or a constant shift learnt from activity.
    def is_bram(t):
        return 'RAMB36' in t['sites'] or 'RAMBFIFO36' in t['sites']

    all_bram_gx = sorted({t['gx'] for t in grid.tiles.values() if is_bram(t)})
    row_bram_gx = collections.defaultdict(set)
    for t in grid.tiles.values():
        if is_bram(t):
            row_bram_gx[grid.crrow[t['gy']]].add(t['gx'])
    for cr in sorted(set(grid.crrow.values())):
        key = crmap.get(cr)
        if key is None:
            continue
        b1 = cols.get((1, key[1], key[2]), [])
        gxs = sorted(row_bram_gx.get(cr, ()))
        if len(b1) > len(gxs) and gxs:
            shift = virtual_bram_shift(cols[key], colmap, cr, gxs,
                                       len(b1) - len(gxs))
            if shift is not None:
                b1 = b1[shift:shift + len(gxs)]
        if len(b1) != len(gxs):
            gxs = all_bram_gx
        if len(b1) != len(gxs):
            if verbose:
                print('bram column mismatch', cr, len(b1), len(gxs))
            # Learned columns (any clock region row) as a constant shift of
            # the BRAM grid column rank.
            c1 = colmap1
            idx = {c[0]: i for i, c in enumerate(b1)}
            shifts = collections.Counter(
                idx[c] - all_bram_gx.index(gx) for (_, gx), c in c1.items()
                if c in idx and gx in all_bram_gx)
            if not shifts:
                continue
            shift = shifts.most_common(1)[0][0]
            if shift < 0 or shift + len(all_bram_gx) > len(b1):
                continue
            b1 = b1[shift:shift + len(all_bram_gx)]
        rank = {gx: i for i, gx in enumerate(gxs)}
        for name, t in grid.tiles.items():
            if t['gx'] not in rank or not ('RAMB36' in t['sites'] or
                                           'RAMBFIFO36' in t['sites']):
                continue
            w = grid.tile_window(name)
            if not w or w[0] != cr:
                continue
            col, first, nfr = b1[rank[t['gx']]]
            out[name]['bits'].append(
                dict(block=1,
                     half=key[1],
                     row=key[2],
                     col=col,
                     base=f'0x{dframes.frames[first]:08x}',
                     frames=nfr,
                     offset=w[1],
                     nbits=w[2]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--die', required=True)
    ap.add_argument('--designs', help='design roots (comma separated); '
                    'collects the activity evidence')
    ap.add_argument('--evidence', help='evidence file: written when '
                    '--designs is given, read otherwise')
    ap.add_argument('--colmap', help='frame column assignment from '
                    'colalign.py (default: activity only)')
    ap.add_argument('--windows', help='tile type windows (windows.py '
                    '--merge output)')
    ap.add_argument('--frames', help='frames used by tile types sharing '
                    'frame columns (colalign.py frames.json)')
    ap.add_argument('--probe', help='comma separated tile types (or "auto": '
                    'tall and windowless ones) given a window of +-'
                    '--probe-span bits around their grid row, to learn their '
                    'windows from a bit database built with it')
    ap.add_argument('--probe-span', type=int, default=None)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    die = dieslib.load()[args.die]
    grid = Grid(die)
    if args.windows:
        with open(args.windows) as f:
            grid.type_windows = {t: tuple(w) for t, w in json.load(f).items()}
    if args.probe:
        span = args.probe_span or grid.rows_per_cr * grid.bpr + grid.centre
        types = probe_types(grid) if args.probe == 'auto' else \
            args.probe.split(',')
        grid.probe = True
        for t in types:
            grid.type_windows[t] = (-span, span)
        print('probe', ' '.join(sorted(types)))
    dframes = DD.DieFrames(die)
    cols = frame_columns(dframes)
    if args.designs:
        ev = collect(die, args.designs, True)
        if args.evidence:
            save_evidence(ev, args.evidence)
            ev = load_evidence(args.evidence, cols)
    elif args.evidence:
        ev = load_evidence(args.evidence, cols)
    else:
        ap.error('--designs or --evidence required')
    if args.colmap:
        with open(args.colmap) as f:
            cm = json.load(f)
        crmap = {int(cr): tuple(k) for cr, k in cm['crmap'].items()}
        colmap = {(cr, gx): col for cr, gx, col in cm['colmap']}
        tilemap = cm.get('tiles', {})
        colmap1 = {k: v.most_common(1)[0][0]
                   for k, v in ev['bram1'].items()}
    else:
        crmap = ev['crmap']
        colmap, colmap1 = assign_activity(grid, cols, ev, True)
        tilemap = {}
    frame_caps = {}
    if args.frames:
        with open(args.frames) as f:
            frame_caps = json.load(f)
    tg = build(grid, dframes, cols, crmap, colmap, colmap1, True, tilemap,
               frame_caps)
    with open(args.out, 'w') as f:
        json.dump(tg, f, indent=0, sort_keys=True)
    nb = sum(1 for t in tg.values() if t['bits'])
    print('tiles with bits', nb, 'of', len(tg))


if __name__ == '__main__':
    main()
