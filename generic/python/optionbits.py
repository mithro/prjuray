#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Configuration frame bits controlled by bitstream options (BITSTREAM.*),
from the option fuzzing bitstreams (generic/tcl/regfuzz.tcl).

Each option value becomes a feature of the tiles whose bits it changes:
  build/db/<arch>/segbits_opt_<tile type>.db
      "<TYPE>.BITSTREAM.<option>=<value> <bit> ..."
"""
import argparse
import collections
import glob
import json
import os

import bitstream
import mkdb as BD
import designdata as DD
import dies as dieslib


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--die', required=True)
    ap.add_argument('--dirs', required=True, nargs='+')
    args = ap.parse_args()
    die = dieslib.load()[args.die]
    dbdir = os.path.join(dieslib.BUILD, 'db', die.arch)
    tg = json.load(open(os.path.join(dbdir, die.name, 'tilegrid.json')))
    col = BD.Collector(die, tg)
    runs = []
    for d in args.dirs:
        for opts in sorted(glob.glob(os.path.join(d, 'r*.opts'))):
            bit = opts[:-5] + '.bit'
            if not os.path.exists(bit):
                continue
            frames = bitstream.frames_sequential(bit, die.arch, col.df.frames)
            far, word, b = bitstream.set_bits(frames, die.arch)
            ids = col.df.bit_ids(far, word, b)
            feats = set()
            for line in open(opts):
                p = line.split()
                if len(p) == 2:
                    feats.add(f'{p[0]}={p[1]}')
            runs.append((feats, set(ids.tolist())))
    common = set.intersection(*(s for _, s in runs))
    union = set.union(*(s for _, s in runs))
    varying = union - common
    print('runs', len(runs), 'varying frame bits', len(varying))
    import numpy as np
    per_type = collections.defaultdict(list)
    touched = set()
    vids = np.array(sorted(varying), dtype=np.int64)
    rb_all = col.region_bits(vids)
    for i in rb_all:
        touched.add(i)
    for feats, ids in runs:
        sel = np.array(sorted(varying & ids), dtype=np.int64)
        rb = col.region_bits(sel)
        for i in touched:
            tt = col.regions[i][1]
            k = col.tile_regions[col.regions[i][0]].index(i)
            per_type[(tt, k)].append((feats, rb.get(i, [])))
    for (tt, k), samples in per_type.items():
        res = BD.correlate(samples)
        suffix = tt.lower() + (f'.{k}' if k else '')
        with open(os.path.join(dbdir, f'segbits_opt_{suffix}.db'), 'w') as f:
            for feat in sorted(res['feat_bits']):
                bits = sorted(b for b in res['feat_bits'][feat]
                              if not b.startswith('!'))
                if bits:
                    f.write(f'{tt}.BITSTREAM.{feat} {" ".join(bits)}\n')
        print(tt, k, 'features', len(res['feat_bits']), 'unexplained',
              len(res['unexplained']))
    unowned = col.unowned(vids)
    print('varying bits outside tiles', unowned)


if __name__ == '__main__':
    main()
