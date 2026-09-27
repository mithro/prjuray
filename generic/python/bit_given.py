#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Debug helper: test hypotheses for one tile bit.

For each hypothesis (a conjunction of feature regexps joined by '&') print
in how many samples of the tile type the hypothesis holds with the bit set
and with the bit clear, then the features most common in the set samples
no hypothesis covers.  E.g.

  bit_given.py --die xa7a12t --tags r9 --type LIOI3 --bit 31_092 \\
      'ODDR_CLK_EDGE=OPPOSITE_EDGE&OLOGIC_X0Y1.CLKINV.SP.CLK_B.OUT' \\
      'OLOGIC_X0Y1.OSERDESE2&OLOGIC_X0Y1.CLKINV.SP.CLK.OUT'
"""
import argparse
import collections
import json
import os
import re

import designdata as DD
import dies as dieslib
import mkdb as BD


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--die', required=True)
    ap.add_argument('--tags', default='r1')
    ap.add_argument('--type', required=True)
    ap.add_argument('--k', type=int, default=0)
    ap.add_argument('--bit', required=True)
    ap.add_argument('--max', type=int, default=80)
    ap.add_argument('--top', type=int, default=12)
    ap.add_argument('hypotheses', nargs='*')
    args = ap.parse_args()
    die = dieslib.load()[args.die]
    tg = json.load(open(os.path.join(dieslib.DB, die.arch, die.name,
                                     'tilegrid.json')))
    col = BD.Collector(die, tg)
    roots = [os.path.join(dieslib.BUILD, 'designs', die.name, t)
             for t in args.tags.split(',')]
    samples = []
    for d in DD.design_dirs(roots, v2only=True)[:args.max]:
        for tt, k, fs, bits in col.samples(d):
            if tt == args.type and k == args.k:
                samples.append((set(fs), args.bit in bits))
    hyps = [[re.compile(x) for x in h.split('&')] for h in args.hypotheses]

    def holds(h, fs):
        return all(any(r.search(f) for f in fs) for r in h)

    print(f'samples {len(samples)} bit set {sum(b for _, b in samples)}')
    for h, txt in zip(hyps, args.hypotheses):
        s = sum(1 for fs, b in samples if b and holds(h, fs))
        c = sum(1 for fs, b in samples if not b and holds(h, fs))
        print(f'{txt:70s} set {s:5d} clear {c:5d}')
    left = [fs for fs, b in samples
            if b and not any(holds(h, fs) for h in hyps)]
    print(f'set samples no hypothesis covers: {len(left)}')
    cnt = collections.Counter(f for fs in left for f in fs)
    for f, n in cnt.most_common(args.top):
        print(f'  {n:5d} {f}')


if __name__ == '__main__':
    main()
