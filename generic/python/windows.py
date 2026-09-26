#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Tile bit windows learnt from design activity.

The structural window of a tile (tilegrid.Grid.tile_window: its INT row and
the empty grid rows above it) is right for fabric tiles but not for tall
hard blocks (CMT, CFG, PCIE, GT, ...) or tiles outside the INT rows (e.g.
clock rows).  Here every bit of a frame column that changes in some design is
attributed to the most specific tile of that frame column (used in the
fewest designs) that is used in (nearly) every design changing the bit;
the window of a tile type is the range of bits attributed to its tiles,
relative to the tile's grid row (tilegrid.Grid.row_bit).

  windows.py --die <die> --designs <roots> --tilegrid <tilegrid.json>
      --out <windows.json>

windows.json: {tile type: {"lo", "hi" (relative bit range), "tiles" (tiles
with attributed bits), "bits" (attributed bits), "hist" (attributed bits per
relative 32 bit word)}}.
"""
import argparse
import collections
import json

import numpy as np

import designdata as DD
import dies as dieslib
import tilegrid as TG

PREC = 0.95  # share of a bit's changes in designs using the tile
MAXUSE = 0.5  # explaining tiles are used in at most this share of designs
MIN_DESIGNS = 2  # a bit must change in at least this many designs


def to_words(mask, nw):
    return np.array([(mask >> (64 * k)) & ((1 << 64) - 1) for k in range(nw)],
                    dtype=np.uint64)


def learn(die, design_root, tg, verbose=False):
    grid = TG.Grid(die)
    df = DD.DieFrames(die)
    act, use, D = TG.activity(die, df, design_root)
    NW = act.shape[2]
    nbf = df.wpf * 32
    findex = df.index
    # Frame column (first frame index, number of frames) of every tile;
    # tiles without a region take their grid column's.
    fc_of = {}
    by_col = {}
    for name, t in tg.items():
        for r in t['bits']:
            if r['block'] == 0:
                fc = (findex[int(r['base'], 16)], r['frames'])
                fc_of[name] = fc
                cr = grid.crrow[t['gy']]
                by_col.setdefault((cr, t['gx']), fc)
    for name, t in grid.tiles.items():
        if name not in fc_of and t['type'] != 'NULL':
            fc = by_col.get((grid.crrow[t['gy']], t['gx']))
            if fc is not None:
                fc_of[name] = fc
    members = collections.defaultdict(list)
    for name, fc in fc_of.items():
        u = use.get(name, 0)
        if u and grid.row_bit(grid.tiles[name]['gy']) is not None:
            members[fc].append(name)
    anyact = act.any(axis=2)
    owned = collections.defaultdict(collections.Counter)  # type -> rel
    tiles_with = collections.defaultdict(set)
    for (first, nfr), names in members.items():
        sub = anyact[first:first + nfr]
        fi, off = np.nonzero(sub)
        if not len(fi):
            continue
        masks = act[first + fi, off]  # (B, NW)
        support = np.bitwise_count(masks).sum(axis=1)
        keep = support >= MIN_DESIGNS
        fi, off, masks, support = fi[keep], off[keep], masks[keep], \
            support[keep]
        if not len(fi):
            continue
        U = np.stack([to_words(use[n], NW) for n in names])  # (T, NW)
        nu = np.bitwise_count(U).sum(axis=1)
        # The bit is explained by the most specific tile (used in the
        # fewest designs) that is used whenever the bit changes: a
        # configurable block changes different bits in different designs,
        # but only in designs using it.
        best = np.full(len(fi), np.inf)
        arg = np.zeros(len(fi), dtype=int)
        for k in range(len(names)):
            inter = np.bitwise_count(masks & U[k][None, :]).sum(axis=1)
            ok = (inter >= PREC * support) & (nu[k] < best)
            best[ok] = nu[k]
            arg[ok] = k
        # the explaining tile must be informative: not used in (almost)
        # every design changing bits of the frame column
        sel = best <= MAXUSE * D
        for k, o in zip(arg[sel].tolist(), off[sel].tolist()):
            n = names[k]
            t = grid.tiles[n]
            rel = o - grid.row_bit(t['gy'])
            owned[t['type']][rel] += 1
            tiles_with[t['type']].add(n)
    out = {}
    for tt, c in owned.items():
        rels = sorted(c)
        hist = collections.Counter()
        for r, n in c.items():
            hist[r // 32] += n
        out[tt] = dict(lo=rels[0], hi=rels[-1] + 1,
                       tiles=len(tiles_with[tt]), bits=sum(c.values()),
                       hist={str(k): v for k, v in sorted(hist.items())})
    return out


def from_probe(dbdir, grid, span, types, mincount=3, minshare=0.02):
    """Windows from a bit database built with probe windows (tilegrid.py
    --probe: +-span bits around the tile's grid row): the hull of the rows
    (bits per row units) holding bits of at least max(3, minshare x
    features) features of the type.  Only features seen at least mincount
    times, and not spread over more than half a clock region (e.g. bank
    wide settings replicated in every row), count.  Returns {type: (first
    bit, end bit)} relative to the tile's grid row."""
    import os
    bpr = grid.bpr
    maxspread = grid.rows_per_cr // 2
    out = {}
    for tt in types:
        suf = tt.lower()
        path = os.path.join(dbdir, f'segbits_{suf}.db')
        if not os.path.exists(path):
            continue
        counts = {}
        for line in open(os.path.join(dbdir, f'counts_{suf}.txt')):
            p = line.split()
            counts[p[0]] = int(p[1])
        rows = collections.Counter()
        nf = 0
        for line in open(path):
            p = line.split()
            if counts.get(p[0], 0) < mincount:
                continue
            r = {(int(b.split('_')[1]) - span) // bpr for b in p[1:]
                 if not b.startswith('!')}
            if not r or max(r) - min(r) > maxspread:
                continue
            nf += 1
            rows.update(r)
        need = max(3, minshare * nf)
        keep = [r for r, n in rows.items() if n >= need]
        if keep:
            out[tt] = (min(keep) * bpr, (max(keep) + 1) * bpr)
    return out


def merge(paths):
    """Hull of the windows of several files."""
    out = {}
    for p in paths:
        with open(p) as f:
            for tt, (lo, hi) in json.load(f).items():
                if tt in out:
                    lo, hi = min(lo, out[tt][0]), max(hi, out[tt][1])
                out[tt] = (lo, hi)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--die', required=True)
    ap.add_argument('--designs')
    ap.add_argument('--tilegrid')
    ap.add_argument('--probe-db', help='bit database directory built with '
                    'the probe tile grid of the die')
    ap.add_argument('--probe-span', type=int)
    ap.add_argument('--probe-types', help='comma separated')
    ap.add_argument('--merge', nargs='*', help='window files to merge')
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    die = dieslib.load()[args.die]
    if args.merge:
        out = merge(args.merge)
    elif args.probe_db:
        grid = TG.Grid(die)
        span = args.probe_span or grid.rows_per_cr * grid.bpr + grid.centre
        out = from_probe(args.probe_db, grid, span,
                         args.probe_types.split(','))
    else:
        with open(args.tilegrid) as f:
            tg = json.load(f)
        out = learn(die, args.designs, tg, True)
        with open(args.out, 'w') as f:
            json.dump(out, f, indent=1, sort_keys=True)
        for tt, w in sorted(out.items()):
            print(f'{tt}: {w["lo"]}..{w["hi"]} tiles {w["tiles"]} '
                  f'bits {w["bits"]}')
        return
    with open(args.out, 'w') as f:
        json.dump(out, f, indent=1, sort_keys=True)
    for tt, (lo, hi) in sorted(out.items()):
        print(f'{tt}: {lo}..{hi}')


if __name__ == '__main__':
    main()
