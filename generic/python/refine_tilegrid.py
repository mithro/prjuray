#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Database guided tile grid refinement.

The tile grid learnt from design activity (tilegrid.py) can be wrong for
columns few designs exercised.  With a bit database built from dies whose
tile grids are trusted, every candidate frame column of a grid column can be
scored by how many of the set bits of its tiles the database documents; the
best one is kept.

  refine_tilegrid.py --die <die> --designs <roots> [--max 20]
"""
import argparse
import collections
import json
import os

import numpy as np

import check as CK
import designdata as DD
import dies as dieslib
import tilegrid as TG


def region_bits(ids, nbf, first, nfr, off, n):
    fi = ids // nbf
    o = ids % nbf
    sel = (fi >= first) & (fi < first + nfr) & (o >= off) & (o < off + n)
    return [f'{a - first:02d}_{b - off:03d}' for a, b in zip(fi[sel], o[sel])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--die', required=True)
    ap.add_argument('--designs', required=True)
    ap.add_argument('--max', type=int, default=20)
    ap.add_argument('--span', type=int, default=3)
    ap.add_argument('--min-bits', type=int, default=30)
    args = ap.parse_args()
    die = dieslib.load()[args.die]
    dbdir = os.path.join(dieslib.DB, die.arch)
    path = os.path.join(dbdir, die.name, 'tilegrid.json')
    tg = json.load(open(path))
    db = CK.Database(dbdir)
    df = DD.DieFrames(die)
    nbf = df.wpf * 32
    cols = TG.frame_columns(df)
    colinfo = {}
    for key, clist in cols.items():
        for i, (col, first, nfr) in enumerate(clist):
            colinfo[(key, col)] = (first, nfr, i)
    # Groups: (block, half, row, grid column) -> tiles
    groups = collections.defaultdict(list)
    for name, t in tg.items():
        for k, r in enumerate(t['bits']):
            if r['block'] != 0:
                continue
            groups[(r['block'], r['half'], r['row'], t['gx'])].append(
                (name, k, r))
    designs = DD.design_dirs(args.designs)[-args.max:]
    idsets = [DD.load_bits(df, d) for d in designs]

    def score(tiles, first, nfr):
        tot = doc = 0
        for ids in idsets:
            for name, k, r in tiles:
                bits = region_bits(ids, nbf, first, nfr, r['offset'],
                                   r['nbits'])
                if not bits:
                    continue
                _, d = db.decode(tg[name]['type'], k, bits)
                tot += len(bits)
                doc += len(set(bits) & d)
        return tot, doc

    changed = 0
    for key, tiles in sorted(groups.items()):
        blk, half, row, gx = key
        r0 = tiles[0][2]
        cur = colinfo.get(((blk, half, row), r0['col']))
        if cur is None:
            continue
        tot, doc = score(tiles, cur[0], cur[1])
        if tot < args.min_bits or doc >= 0.95 * tot:
            continue
        clist = cols[(blk, half, row)]
        best = (doc / tot, r0['col'])
        for j in range(max(0, cur[2] - args.span),
                       min(len(clist), cur[2] + args.span + 1)):
            col, first, nfr = clist[j]
            t2, d2 = score(tiles, first, nfr)
            if t2 >= args.min_bits and d2 / t2 > best[0] + 0.05:
                best = (d2 / t2, col)
        if best[1] != r0['col']:
            first, nfr, _ = colinfo[((blk, half, row), best[1])]
            for name, k, r in tiles:
                r['col'] = best[1]
                r['base'] = f'0x{df.frames[first]:08x}'
                r['frames'] = nfr
            changed += 1
            print(f'{key} col {r0["col"]} -> {best[1]} '
                  f'({doc}/{tot} -> {best[0]:.2f})', flush=True)
        else:
            print(f'{key} col {r0["col"]} poor ({doc}/{tot}), no better '
                  f'candidate', flush=True)
    with open(path, 'w') as f:
        json.dump(tg, f, indent=0, sort_keys=True)
    print('changed', changed)


if __name__ == '__main__':
    main()
