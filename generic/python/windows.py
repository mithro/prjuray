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


def from_probe(dbdir, grid, span, types, mincount=3, minshare=0.02,
               spread_retry=True):
    """Windows from a bit database built with probe windows (tilegrid.py
    --probe: +-span bits around the tile's grid row): among the rows (bits
    per row units) holding bits of at least max(3, minshare x features)
    features of the type, the contiguous run around the tile's row of rows
    with at least CORE_FRAC of the busiest row's when it holds CORE_MASS of
    them (a GT channel between its neighbours), else their hull.  Only features seen at
    least mincount times, and not spread over more than half a clock region
    (e.g. bank wide settings replicated in every row), count; if none is
    left (a type whose features all catch bits of neighbouring tiles, e.g.
    GTX_CHANNEL_0), all of them.  Returns {type: (first
    bit, end bit, {row: features})} relative to the tile's grid row (the
    rows' feature counts decide which of overlapping tiles owns a row)."""
    import os
    bpr = grid.bpr
    maxspread = grid.rows_per_cr // 2
    # The probe window is clipped at the frame ends (e.g. Series7
    # GTX_CHANNEL_0 five rows above the bottom): the offset of a type's
    # region relative to its grid row, from the probe tile grid.
    shift = collections.defaultdict(set)
    tgpath = os.path.join(dbdir, grid.die.name, 'tilegrid.json')
    if os.path.exists(tgpath):
        with open(tgpath) as f:
            ptg = json.load(f)
        for name, e in ptg.items():
            base = grid.row_bit(e['gy'])
            if base is not None and e['bits'] and e['type'] in types:
                shift[e['type']].add(base - e['bits'][0]['offset'])
    out = {}
    for tt in types:
        suf = tt.lower()
        path = os.path.join(dbdir, f'segbits_{suf}.db')
        if not os.path.exists(path):
            continue
        sh = shift.get(tt, {span})
        if len(sh) != 1:
            continue  # tiles of the type clipped differently
        sh = sh.pop()
        counts = {}
        for line in open(os.path.join(dbdir, f'counts_{suf}.txt')):
            p = line.split()
            counts[p[0]] = int(p[1])
        rows, nf = _probe_rows(path, counts, mincount, sh, bpr, maxspread)
        retried = False
        if not rows and spread_retry:
            rows, nf = _probe_rows(path, counts, mincount, sh, bpr, None)
            retried = True
        need = max(3, minshare * nf)
        keep = {r: n for r, n in rows.items() if n >= need}
        if keep:
            # The contiguous run of rows around the tile's own row (or the
            # busiest row) with at least CORE_FRAC of the busiest row's
            # features: rows of other tiles caught in the probe window
            # (neighbouring GT channels, ...) stay out.
            mx = max(keep.values())
            core = {r for r, n in keep.items() if n >= CORE_FRAC * mx}
            start = 0 if 0 in core else max(keep, key=keep.get)
            lo = hi = start
            while lo - 1 in core:
                lo -= 1
            while hi + 1 in core:
                hi += 1
            run = {r: n for r, n in keep.items() if lo <= r <= hi}
            if retried or sum(run.values()) >= CORE_MASS * sum(keep.values()):
                keep = run
            else:  # no dominant block (e.g. CMT): the hull
                lo, hi = min(keep), max(keep)
            out[tt] = (lo * bpr, (hi + 1) * bpr,
                       {str(r): n for r, n in sorted(keep.items())})
    return out


CORE_FRAC = 0.25
CORE_MASS = 0.8  # share of the features' rows the run must hold


def _probe_rows(path, counts, mincount, sh, bpr, maxspread):
    rows = collections.Counter()
    nf = 0
    for line in open(path):
        p = line.split()
        if counts.get(p[0], 0) < mincount:
            continue
        r = {(int(b.split('_')[1]) - sh) // bpr for b in p[1:]
             if not b.startswith('!')}
        if not r or (maxspread is not None and max(r) - min(r) > maxspread):
            continue
        nf += 1
        rows.update(r)
    return rows, nf


def merge(paths):
    """Per tile type, the median first and end bit over the dies' windows
    (robust against a die with few samples of the type) and the summed row
    feature counts."""
    per = collections.defaultdict(list)
    for p in paths:
        with open(p) as f:
            for tt, w in json.load(f).items():
                per[tt].append(w)
    out = {}
    for tt, ws in per.items():
        los = sorted(w[0] for w in ws)
        his = sorted(w[1] for w in ws)
        hist = collections.Counter()
        for w in ws:
            if len(w) > 2:
                hist.update({int(r): n for r, n in w[2].items()})
        out[tt] = (los[len(los) // 2], his[(len(his) - 1) // 2],
                   {str(r): n for r, n in sorted(hist.items())})
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
    for tt, w in sorted(out.items()):
        print(f'{tt}: {w[0]}..{w[1]}')


if __name__ == '__main__':
    main()
