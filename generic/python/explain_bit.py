#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Debug helper: statistics relating one tile bit to the tile's features.

  explain_bit.py --die <die> --tags r1 --type CLEL_R --bit 08_029 [--k 0]
"""
import argparse
import collections
import json
import os

import mkdb as BD
import designdata as DD
import dies as dieslib


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--die', required=True)
    ap.add_argument('--tags', default='r1')
    ap.add_argument('--type', required=True)
    ap.add_argument('--k', type=int, default=0)
    ap.add_argument('--bit', required=True)
    ap.add_argument('--max', type=int, default=30)
    ap.add_argument('--top', type=int, default=15)
    ap.add_argument('--v2', action='store_true')
    args = ap.parse_args()
    die = dieslib.load()[args.die]
    tg = json.load(
        open(os.path.join(dieslib.DB, die.arch, die.name,
                          'tilegrid.json')))
    col = BD.Collector(die, tg)
    nb = nnb = 0
    fb = collections.Counter()
    fnb = collections.Counter()
    empty_b = empty_nb = 0
    roots = [
        os.path.join(dieslib.BUILD, 'designs', die.name, t)
        for t in args.tags.split(',')
    ]
    for d in DD.design_dirs(roots, v2only=args.v2)[:args.max]:
        for tt, k, fs, bits in col.samples(d):
            if tt != args.type or k != args.k:
                continue
            if args.bit in bits:
                nb += 1
                fb.update(fs)
                empty_b += not fs
            else:
                nnb += 1
                fnb.update(fs)
                empty_nb += not fs
    print(f'bit set in {nb} samples ({empty_b} empty), clear in {nnb} '
          f'({empty_nb} empty)')
    rows = []
    for f in set(fb) | set(fnb):
        a, b = fb[f], fnb[f]
        rows.append((a / max(1, nb), a / max(1, a + b), a, b, f))
    print('-- by P(feature | bit set)')
    for p_f_b, p_b_f, a, b, f in sorted(rows, reverse=True)[:args.top]:
        print(f'  P(f|b)={p_f_b:.2f} P(b|f)={p_b_f:.2f} {a:5d} {b:5d} {f}')
    print('-- by P(bit set | feature), seen >= 3')
    for p_f_b, p_b_f, a, b, f in sorted(
        (r for r in rows if r[2] + r[3] >= 3), key=lambda r: -r[1])[:args.top]:
        print(f'  P(f|b)={p_f_b:.2f} P(b|f)={p_b_f:.2f} {a:5d} {b:5d} {f}')


if __name__ == '__main__':
    main()
