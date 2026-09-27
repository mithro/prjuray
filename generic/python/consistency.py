#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Per design consistency of the feature dump with the bitstream.

A design whose feature dump does not describe its bitstream (e.g. a repair
path leaving a stale state) poisons the bit database: mkdb only keeps
feature -> bit implications that hold in every sample.  With an existing
bit database, every feature of the database that a tile of the design has
must have its bits set (and its "!" bits clear) in that tile; a healthy
design violates few of these, a bad one many (concentrated on the features
whose meaning its dump got wrong).

  consistency.py --die <die> --designs <roots> [--max N] [--top K]

Prints per design the checked / violated feature instances and the features
the design gets systematically wrong (violated in at least --min-viol and
--share of at least --min-occ instances), then "SUSPECT <dir>" for each design with such
features.  Exit status 1 when there are suspects.
"""
import argparse
import collections
import json
import os
import sys

import check as CK
import designdata as DD
import dies as dieslib
import mkdb


def design_violations(col, db, d):
    """(checked, violated, Counter(feature -> violations), Counter(feature
    -> occurrences))"""
    checked = violated = 0
    per = collections.Counter()
    occ = collections.Counter()
    for tt, k, fs, bits in col.samples(d):
        if not fs:
            continue
        feats, _, _ = db.get(tt, k)
        if not feats:
            continue
        bits = set(bits)
        for name, pos, neg in feats:
            parts = name.split('.', 1)[1].split('&')
            if not all(p in fs for p in parts):
                continue
            checked += 1
            key = f'{tt}.{name.split(".", 1)[1]}'
            occ[key] += 1
            if not pos <= bits or neg & bits:
                violated += 1
                per[key] += 1
    return checked, violated, per, occ


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--die', required=True)
    ap.add_argument('--designs', required=True,
                    help='comma separated design roots')
    ap.add_argument('--max', type=int, default=1000)
    ap.add_argument('--top', type=int, default=3)
    ap.add_argument('--min-occ', type=int, default=20,
                    help='instances of a feature needed to judge it')
    ap.add_argument('--min-viol', type=int, default=5,
                    help='violated instances needed to call a feature wrong')
    ap.add_argument('--share', type=float, default=0.05,
                    help='share of violated instances making a feature '
                    'systematically wrong in a design')
    args = ap.parse_args()
    die = dieslib.load()[args.die]
    dbdir = os.path.join(dieslib.DB, die.arch)
    tg = json.load(open(os.path.join(dbdir, die.name, 'tilegrid.json')))
    col = mkdb.Collector(die, tg)
    db = CK.Database(dbdir)
    bad = []
    n_designs = 0
    for d in DD.design_dirs(args.designs)[:args.max]:
        n_designs += 1
        n, v, per, occ = design_violations(col, db, d)
        # Features this design gets wrong systematically: violated in at
        # least --share of at least --min-occ instances (occasional
        # violations are database noise).
        wrong = sorted(((per[f] / occ[f], f) for f in per
                        if occ[f] >= args.min_occ and per[f] >= args.min_viol
                        and per[f] >= args.share * occ[f]), reverse=True)
        top = ', '.join(f'{f} {per[f]}/{occ[f]}' for _, f in wrong[:args.top])
        print(f'{d} checked {n} violated {v} wrong_features {len(wrong)} '
              f'{top}', flush=True)
        if wrong:
            bad.append(d)
    print(f'suspects {len(bad)} of {n_designs}')
    for d in bad:
        print(f'SUSPECT {d}')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
