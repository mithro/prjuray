#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""PIP coverage of the database: which PIPs of each tile type have been
observed (and correlated) and which have not.

  coverage.py --arch Series7 --pips build/meta/pips/<die>.txt [--targets out]
"""
import argparse
import collections
import glob
import os

import dies as dieslib


def db_features(dbdir):
    """{tile type: {feature: has bits}} from segbits files."""
    out = collections.defaultdict(dict)
    for path in glob.glob(os.path.join(dbdir, 'segbits_*.db')):
        for line in open(path):
            p = line.split()
            tt, feat = p[0].split('.', 1)
            out[tt][feat] = len(p) > 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--arch', required=True)
    ap.add_argument('--pips', required=True, action='append')
    ap.add_argument('--targets', default=None)
    ap.add_argument('--min-seen', type=int, default=3)
    args = ap.parse_args()
    dbdir = os.path.join(dieslib.DB, args.arch)
    db = db_features(dbdir)
    counts = {}
    for path in glob.glob(os.path.join(dbdir, 'counts_*.txt')):
        for line in open(path):
            f, n = line.rsplit(' ', 1)
            counts[f] = int(n)
    pips = collections.defaultdict(set)
    for path in args.pips:
        for line in open(path):
            p = line.split()
            if p[5] == '1':  # pseudo pip (route-through)
                continue
            pips[p[1]].add((p[2], p[3], p[4]))
    targets = []
    tot = cov = 0
    for tt in sorted(pips):
        n = len(pips[tt])
        c = 0
        for w0, w1, d in pips[tt]:
            name = f'{w0}->{w1}' if d == '1' else f'{w0}<->{w1}'
            if counts.get(f'{tt}.{name}', 0) >= args.min_seen:
                c += 1
            else:
                targets.append((tt, w0, w1, d))
        tot += n
        cov += c
        print(f'{tt:32s} pips {n:6d} seen>={args.min_seen} {c:6d} '
              f'({100.0 * c / max(1, n):5.1f}%)')
    print(f'total pips {tot} covered {cov} ({100.0 * cov / max(1, tot):.1f}%)')
    if args.targets:
        with open(args.targets, 'w') as f:
            for t in targets:
                f.write(' '.join(t) + '\n')


if __name__ == '__main__':
    main()
