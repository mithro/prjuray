#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Configuration register database: correlates BITSTREAM.* options with the
values written to configuration registers (and with any configuration frame
bits they change).

  regdb.py --arch Series7 --dirs build/regfuzz/xa7s15 [...]

Register writes are identified by register name and occurrence index (the
packet sequence of a bitstream is fixed), e.g. COR0#0, CMD#3.  Output:
  build/db/<arch>/registers.db     "<option>=<value> <REG#k>:<bit> ..."
  build/db/<arch>/registers.txt    register write sequence with the bits no
                                   option explains (fixed values)
"""
import argparse
import collections
import glob
import os

import numpy as np

import bitstream
import mkdb
import dies as dieslib

REG_NAMES = {
    0: 'CRC', 1: 'FAR', 2: 'FDRI', 3: 'FDRO', 4: 'CMD', 5: 'CTL0', 6: 'MASK',
    7: 'STAT', 8: 'LOUT', 9: 'COR0', 10: 'MFWR', 11: 'CBC', 12: 'IDCODE',
    13: 'AXSS', 14: 'COR1', 16: 'WBSTAR', 17: 'TIMER', 19: 'RBCRC_SW',
    22: 'BOOTSTS', 24: 'CTL1', 31: 'BSPI',
}


def reg_writes(path):
    """[(name#k, value)] for every register write except frame data and
    CRC (which depends on the frame data)."""
    words = bitstream.read_words(path)
    seen = collections.Counter()
    out = []
    for op, reg, payload in bitstream.iter_packets(words):
        if op != 2 or reg in (2, 0) or not len(payload):
            continue
        name = REG_NAMES.get(reg, f'R{reg}')
        for w in payload:
            key = f'{name}#{seen[name]}'
            seen[name] += 1
            out.append((key, int(w)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--arch', required=True)
    ap.add_argument('--dirs', required=True, nargs='+')
    ap.add_argument('--die', default=None,
                    help='also check frame bits for option dependence')
    args = ap.parse_args()
    samples = []
    seqs = []
    for d in args.dirs:
        for opts in sorted(glob.glob(os.path.join(d, 'r*.opts'))):
            bit = opts[:-5] + '.bit'
            if not os.path.exists(bit):
                continue
            feats = set()
            for line in open(opts):
                p = line.split()
                if len(p) == 2:
                    feats.add(f'{p[0]}={p[1]}')
            writes = reg_writes(bit)
            seqs.append(writes)
            bits = [f'{k}:{b:02d}' for k, v in writes for b in range(32)
                    if (v >> b) & 1]
            samples.append((feats, bits))
    print('bitstreams', len(samples))
    res = mkdb.correlate(samples)
    outdir = os.path.join(dieslib.DB, args.arch)
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, 'registers.db'), 'w') as f:
        for feat in sorted(res['feat_bits']):
            f.write(f'{feat} {" ".join(sorted(res["feat_bits"][feat]))}\n')
    # Bits set in every bitstream are fixed register contents.
    counts = collections.Counter(b for _, bits in samples for b in bits)
    fixed = sorted(b for b, n in counts.items() if n == len(samples))
    names = []
    for k, _ in seqs[0]:
        if k not in names:
            names.append(k)
    with open(os.path.join(outdir, 'registers.txt'), 'w') as f:
        f.write('# register write sequence; fixed = set in every bitstream\n')
        for k in names:
            fb = [b.split(':')[1] for b in fixed if b.split(':')[0] == k]
            f.write(f'{k} fixed {" ".join(fb)}\n')
        f.write('# unexplained (varying, no option explains them)\n')
        for b, (left, n, d) in sorted(res['unexplained'].items()):
            f.write(f'{b} unexplained {left} of {n}\n')
    print('options with bits', len(res['feat_bits']), 'unexplained',
          len(res['unexplained']))
    if args.die:
        # Frame bits that differ between the option variants of one design.
        import designdata as DD
        die = dieslib.load()[args.die]
        df = DD.DieFrames(die)
        fsamples = []
        for d in args.dirs:
            for opts in sorted(glob.glob(os.path.join(d, 'r*.opts'))):
                bit = opts[:-5] + '.bit'
                if not os.path.exists(bit):
                    continue
                frames = bitstream.frames_sequential(bit, die.arch, df.frames)
                far, word, b = bitstream.set_bits(frames, die.arch)
                feats = set(f'{p[0]}={p[1]}' for p in
                            (l.split() for l in open(opts)) if len(p) == 2)
                fsamples.append((feats, set(df.bit_ids(far, word, b).tolist())))
        common = set.intersection(*(s for _, s in fsamples))
        union = set.union(*(s for _, s in fsamples))
        varying = sorted(union - common)
        print('frame bits varying with options:', len(varying))
        with open(os.path.join(outdir, f'option_frame_bits_{args.die}.txt'),
                  'w') as f:
            for gid in varying:
                far, w, b = df.decode_id(gid)
                on = [sorted(fs) for fs, s in fsamples if gid in s][:1]
                f.write(f'{far:08x} {w} {b} set_in {sum(gid in s for _, s in fsamples)}/{len(fsamples)}\n')


if __name__ == '__main__':
    main()
