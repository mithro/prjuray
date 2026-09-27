#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Which tile bits are exactly explained by a single feature, among the
samples of a tile type with a given site type in use (e.g. to check
derived features such as the clock generator DRP fields).

For each bit that varies, prints the features whose sample pattern equals
the bit's; for unexplained bits with an offset in [--lo, --hi), the
nearest feature and the samples where they disagree.

  bit_fit.py --die xa7s15 --tags r10,r11 --type CMT_TOP_L_LOWER_B \
      --site-type MMCME2_ADV --lo 800 --hi 1000
"""
import argparse
import collections
import json
import os

import designdata as DD
import dies as dieslib
import mkdb as BD


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--die', required=True)
    ap.add_argument('--tags', required=True)
    ap.add_argument('--type', required=True)
    ap.add_argument('--site-type', required=True)
    ap.add_argument('--max', type=int, default=200)
    ap.add_argument('--lo', type=int, default=0)
    ap.add_argument('--hi', type=int, default=0)
    a = ap.parse_args()
    d = dieslib.load()[a.die]
    with open(os.path.join(dieslib.DB, d.arch, d.name, 'tilegrid.json')) as f:
        tg = json.load(f)
    col = BD.Collector(d, tg)
    roots = [os.path.join(dieslib.BUILD, 'designs', a.die, t)
             for t in a.tags.split(',')]
    S, names = [], []
    for dd in DD.design_dirs(roots, v2only=True)[:a.max]:
        for t, k, fs, bits in col.samples(dd):
            if t == a.type and k == 0 and any(
                    f.endswith('.TYPE.' + a.site_type) for f in fs):
                S.append((frozenset(fs), frozenset(bits)))
                names.append('/'.join(dd.split('/')[-2:]))
    n = len(S)
    print('samples', n)
    bc = collections.Counter(b for _, bs in S for b in bs)
    fc = collections.Counter(f for fs, _ in S for f in fs)
    fsets = {f: frozenset(i for i, (fs, _) in enumerate(S) if f in fs)
             for f, c in fc.items() if 0 < c < n}
    byset = collections.defaultdict(list)
    for f, s in fsets.items():
        byset[s].append(f)
    res = collections.Counter()
    for b in sorted(b for b, c in bc.items() if 0 < c < n):
        s = frozenset(i for i, (_, bs) in enumerate(S) if b in bs)
        fs = byset.get(s, [])
        if fs:
            res['explained'] += 1
            print(b, len(s), sorted(fs)[:3])
            continue
        res['unexplained'] += 1
        if fsets and a.lo <= int(b.split('_')[1]) < a.hi:
            best = min(fsets, key=lambda f: len(fsets[f] ^ s))
            print('NEAR', b, len(s), best, 'differs in',
                  [names[i] for i in sorted(fsets[best] ^ s)])
    print(dict(res))


if __name__ == '__main__':
    main()
