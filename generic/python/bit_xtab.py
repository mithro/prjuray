#!/usr/bin/env python3
"""Cross tabulate features against bits of one tile type: for every sample
(tile instance in a design) the set of features matching a regular
expression (or its first group) against the values of the given bits.
Shows how a bit combines features (conjunctions, one-hot groups, ...).

    bit_xtab.py --die xa7s15 --tags r9 --type DSP_L \\
        --features 'X0Y1\\.DSP48E1\\.(AREG|ACASCREG)=' --bits 26_299,27_271
"""
import argparse
import collections
import json
import os
import re

import designdata
import dies as dieslib
import mkdb


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--die', required=True)
    ap.add_argument('--tags', required=True, help='comma separated')
    ap.add_argument('--type', required=True, help='tile type')
    ap.add_argument('--k', type=int, default=0, help='region index')
    ap.add_argument('--features', required=True,
                    help='regular expression; its first group, if any, is '
                    'the key shown')
    ap.add_argument('--bits', required=True, help='comma separated')
    ap.add_argument('--max', type=int, default=40, help='designs')
    args = ap.parse_args()
    die = dieslib.load()[args.die]
    rx = re.compile(args.features)
    bits = args.bits.split(',')
    with open(os.path.join(dieslib.DB, die.arch, die.name,
                           'tilegrid.json')) as f:
        col = mkdb.Collector(die, json.load(f))
    roots = [os.path.join(dieslib.BUILD, 'designs', die.name, t)
             for t in args.tags.split(',')]
    tab = collections.defaultdict(collections.Counter)
    n = 0
    for d in designdata.design_dirs(roots)[:args.max]:
        for t, k, fs, bs in col.samples(d):
            if t != args.type or k != args.k:
                continue
            n += 1
            key = set()
            for f in fs:
                m = rx.search(f)
                if m:
                    key.add(m.group(1) if m.groups() else f)
            val = ''.join('1' if b in bs else '0' for b in bits)
            tab[tuple(sorted(key))][val] += 1
    print('samples', n, 'bits', ' '.join(bits))
    for key, c in sorted(tab.items(), key=lambda kv: -sum(kv[1].values())):
        print(f'{sum(c.values()):5d}  {dict(sorted(c.items()))}  '
              f'{" ".join(key) or "-"}')


if __name__ == '__main__':
    main()
