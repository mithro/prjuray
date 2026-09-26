#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Check a bitstream against the database: every set configuration bit must
be documented.

A set bit is documented when it lies inside a tile's frame region and is
either a default bit of that tile type or one of the bits of a database
feature whose bits all match (set bits set, "!" bits clear).  Bits in
regions shared by several tiles are documented if any owner documents them.

Usage:
  check.py --die <die> (--bit <file.bit> | --designs <dir>)
"""
import argparse
import collections
import json
import os
import sys

import numpy as np

import bitstream
import mkdb
import designdata as DD
import dies as dieslib


class Database:
    def __init__(self, dbdir):
        self.dbdir = dbdir
        self.types = {}

    def get(self, ttype, k):
        key = (ttype, k)
        if key in self.types:
            return self.types[key]
        suffix = ttype.lower() + (f'.{k}' if k else '')
        feats = []
        for path in (os.path.join(self.dbdir, f'segbits_{suffix}.db'),
                     os.path.join(self.dbdir, f'segbits_opt_{suffix}.db')):
            if not os.path.exists(path):
                continue
            for line in open(path):
                p = line.split()
                pos = frozenset(b for b in p[1:] if not b.startswith('!'))
                neg = frozenset(b[1:] for b in p[1:] if b.startswith('!'))
                feats.append((p[0], pos, neg))
        defaults = set()
        path = os.path.join(self.dbdir, f'defaults_{suffix}.db')
        if os.path.exists(path):
            defaults = set(l.split()[0] for l in open(path) if l.strip())
        by_bit = collections.defaultdict(list)
        for i, (_, pos, _) in enumerate(feats):
            for b in pos:
                by_bit[b].append(i)
        self.types[key] = (feats, defaults, by_bit)
        return self.types[key]

    def decode(self, ttype, k, bits):
        """Returns (matched feature names, set of documented bits)."""
        feats, defaults, by_bit = self.get(ttype, k)
        bits = set(bits)
        doc = bits & defaults
        matched = []
        seen = set()
        for b in bits:
            for i in by_bit.get(b, ()):
                if i in seen:
                    continue
                seen.add(i)
                name, pos, neg = feats[i]
                if pos <= bits and not (neg & bits):
                    matched.append(name)
                    doc |= pos
        return matched, doc


def check_ids(col, db, ids, fasm=None):
    """Returns (unowned bit count, {tile type: Counter(bit -> count)}).
    If fasm is a list, decoded "<tile>.<feature>" lines are appended."""
    rb = col.region_bits(ids)
    unknown = collections.defaultdict(collections.Counter)
    # region idx -> (tile type, region index within tile)
    rinfo = {}
    for tile, rlist in col.tile_regions.items():
        for k, i in enumerate(rlist):
            rinfo[i] = (col.regions[i][1], k)
    # Documented global ids per region; shared bits need one owner.
    documented = set()
    undoc = []
    for i, bits in rb.items():
        tt, k = rinfo[i]
        matched, doc = db.decode(tt, k, bits)
        if fasm is not None:
            tile = col.regions[i][0]
            for m in matched:
                fasm.append(f'{tile}.{m.split(".", 1)[1]}')
        _, _, fi, _, off, _ = col.regions[i]
        for b in bits:
            f, o = b.split('_')
            gid = (fi + int(f)) * col.nbf + off + int(o)
            if b in doc:
                documented.add(gid)
            else:
                undoc.append((gid, tt, b))
    for gid, tt, b in undoc:
        if gid not in documented:
            unknown[tt][b] += 1
    return col.unowned(ids), unknown


# Registers whose value is fully defined by their meaning.
SEMANTIC_REGS = {
    'CMD': 'command code',
    'FAR': 'frame address',
    'IDCODE': 'device IDCODE',
    'CRC': 'CRC of the preceding data',
    'MFWR': 'multi frame write',
    'LOUT': 'legacy output',
    'RBCRC_SW': 'readback CRC',
}


def check_registers(path, dbdir):
    """Configuration register writes: bits not explained by the register
    database (option values, fixed bits) or by the register's meaning."""
    import regdb
    opts = []
    p = os.path.join(dbdir, 'registers.db')
    if os.path.exists(p):
        for line in open(p):
            f = line.split()
            opts.append((f[0], set(f[1:])))
    fixed = set()
    p = os.path.join(dbdir, 'registers.txt')
    if os.path.exists(p):
        for line in open(p):
            f = line.split()
            if len(f) >= 2 and f[1] == 'fixed':
                fixed.update(f'{f[0]}:{b}' for b in f[2:])
    bits = set()
    for k, v in regdb.reg_writes(path):
        name = k.split('#')[0]
        if name in SEMANTIC_REGS or name.startswith('R'):
            continue
        bits.update(f'{k}:{b:02d}' for b in range(32) if (v >> b) & 1)
    doc = bits & fixed
    for name, obits in opts:
        doc |= bits & obits
    return sorted(bits - doc)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--die', required=True)
    ap.add_argument('--bit', action='append', default=[])
    ap.add_argument('--designs', default=None)
    ap.add_argument('--max', type=int, default=1000)
    ap.add_argument('--top', type=int, default=10)
    ap.add_argument('--registers', action='store_true',
                    help='also check configuration register writes')
    ap.add_argument('--fasm', default=None,
                    help='write decoded features of the (first) input here')
    args = ap.parse_args()
    die = dieslib.load()[args.die]
    dbdir = os.path.join(dieslib.BUILD, 'db', die.arch)
    tg = json.load(open(os.path.join(dbdir, die.name, 'tilegrid.json')))
    col = mkdb.Collector(die, tg)
    db = Database(dbdir)
    total_unowned = 0
    total = collections.defaultdict(collections.Counter)
    inputs = []
    for b in args.bit:
        _, frames, order = bitstream.frames_explicit(b, die.arch)
        # Per-frame-CRC bitstreams write every frame separately.
        if len(frames) < len(col.df.frames) // 2:
            frames = bitstream.frames_sequential(b, die.arch,
                                                 col.df.frames)
        far, word, bit = bitstream.set_bits(frames, die.arch)
        inputs.append((b, np.sort(col.df.bit_ids(far, word, bit))))
    if args.designs:
        for d in DD.design_dirs(args.designs)[:args.max]:
            inputs.append((d, DD.load_bits(col.df, d)))
    if args.registers:
        for b in args.bit:
            und = check_registers(b, dbdir)
            print(f'{b}: undocumented register bits {len(und)} {und[:20]}')
            if und:
                total['REGISTERS'].update(und)
    for idx, (name, ids) in enumerate(inputs):
        fasm = [] if (args.fasm and idx == 0) else None
        unowned, unknown = check_ids(col, db, ids, fasm)
        if fasm is not None:
            with open(args.fasm, 'w') as f:
                f.write('\n'.join(sorted(set(fasm))) + '\n')
        n = sum(sum(c.values()) for c in unknown.values())
        print(f'{name}: set {len(ids)} unowned {unowned} undocumented {n}')
        total_unowned += unowned
        for tt, c in unknown.items():
            total[tt].update(c)
    print('== undocumented bits by tile type')
    for tt, c in sorted(total.items(), key=lambda x: -sum(x[1].values())):
        print(f'{tt}: {sum(c.values())} ({len(c)} distinct) e.g. '
              f'{", ".join(f"{b}x{n}" for b, n in c.most_common(args.top))}')
    print('unowned total', total_unowned)
    return 1 if (total or total_unowned) else 0


if __name__ == '__main__':
    sys.exit(main())
