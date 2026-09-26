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
        self.tiles = {}
        self.at = {}
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

    def tile_window(self, name):
        """(clock region row, first bit, number of bits) or None."""
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


def learn(die, design_root, verbose=False):
    grid = Grid(die)
    dframes = DD.DieFrames(die)
    wpf = dframes.wpf
    sk = featlib.SiteKeys(die.tiles_tsv)
    # Up to MAXD designs (one bit each in the activity masks), newest tags
    # first (later tags use larger parts of the die and better generators).
    roots = design_root.split(',') if isinstance(design_root, str) \
        else list(design_root)
    dirs = []
    for r in reversed(roots):
        dirs += DD.design_dirs(r)
    MAXD = 192
    dirs = dirs[:MAXD]
    D = len(dirs)
    NW = (D + 63) // 64
    nbits_frame = wpf * 32
    # Activity: act[frame index, bit offset, word] = mask of designs in which
    # that bit differs from the baseline.
    act = np.zeros((len(dframes.frames), nbits_frame, max(1, NW)),
                   dtype=np.uint64)
    flat = act.reshape(-1, max(1, NW))
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
    # 2. Frame column of each grid column: per clock region row, score every
    # (grid column, frame column) pair, then pick a monotonic assignment.
    colmap = {}
    by_cr = collections.defaultdict(list)
    for t, u, w in tiles_used:
        by_cr[w[0]].append((t, u, w))
    for cr, lst in by_cr.items():
        key = crmap.get(cr)
        if key is None:
            continue
        clist = block0[key]
        M = len(clist)
        scores = collections.defaultdict(lambda: np.zeros(M))
        for t, u, (_, lo, n) in lst:
            gx = grid.tiles[t]['gx']
            v = np.array([col_score(u, col, lo, n) for col in clist])
            scores[gx] += (v >= 0.95) * v
        gxs = sorted(scores)
        # DP: best[j] = best total with last used major <= j.
        NEG = -1e18
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
            else:
                pass
            # previous columns must use majors <= j
            j = int(np.argmax(pm[:j + 1] == pm[j])) if pm[j] > 0 else j
    propagate_columns(grid, crmap, colmap, block0, verbose)
    fill_gaps(grid, crmap, colmap, block0, verbose)
    # Block RAM content columns (block type 1), same scoring.
    colmap1 = {}
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
    for k, v in votes1.items():
        colmap1[k] = v.most_common(1)[0][0]
    learn.colmap1 = colmap1
    if verbose:
        print('frame rows', crmap)
        print('columns', len(colmap))
    return grid, dframes, crmap, colmap, cols


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


def build(die, design_root, verbose=False):
    """Returns the tilegrid dict: tile -> {type, gx, gy, bits: [...]}"""
    grid, dframes, crmap, colmap, cols = learn(die, design_root, verbose)
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
            col = colmap.get((cr, t['gx']))
            if key is not None and col is not None:
                first, nfr = colinfo[(key, col)]
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
    # Block RAM content frames (block type 1): the k-th BRAM column of a
    # clock region row owns the k-th block type 1 frame column of that row.
    # Frame columns exist for every BRAM grid column of the die, even in
    # rows where other blocks (e.g. the PS) replace the BRAM tiles.
    all_bram_gx = sorted({
        t['gx']
        for t in grid.tiles.values()
        if 'RAMB36' in t['sites'] or 'RAMBFIFO36' in t['sites']
    })
    for cr in sorted(set(grid.crrow.values())):
        key = crmap.get(cr)
        if key is None:
            continue
        b1 = cols.get((1, key[1], key[2]), [])
        gxs = all_bram_gx
        if len(b1) != len(gxs):
            if verbose:
                print('bram column mismatch', cr, len(b1), len(gxs))
            # Learned columns (any clock region row) as a constant shift of
            # the BRAM grid column rank.
            c1 = getattr(learn, 'colmap1', {})
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
    ap.add_argument('--designs', required=True)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    die = dieslib.load()[args.die]
    tg = build(die, args.designs, True)
    with open(args.out, 'w') as f:
        json.dump(tg, f, indent=0, sort_keys=True)
    nb = sum(1 for t in tg.values() if t['bits'])
    print('tiles with bits', nb, 'of', len(tg))


if __name__ == '__main__':
    main()
