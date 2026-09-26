#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Group Vivado devices that share the same die (identical tile grid)."""
import argparse
import collections
import glob
import hashlib
import json
import os


def die_signature(path):
    h = hashlib.sha1()
    ntiles = 0
    head = None
    with open(path) as f:
        for line in f:
            p = line.split()
            if p[0] == 'part':
                head = p
                continue
            if p[0] != 'tile':
                continue
            ntiles += 1
            # Tile type + grid position define the die; site names differ
            # between bonded/unbonded variants so only hash site types.
            sites = p[6]
            stypes = ','.join(s.split(':')[1]
                              for s in sites.split(',')) if sites != '-' else '-'
            h.update(f'{p[1]} {p[2]} {p[3]} {p[4]} {stypes}\n'.encode())
    return head, ntiles, h.hexdigest()[:12]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('tiles_dir')
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    groups = collections.defaultdict(list)
    for path in sorted(glob.glob(os.path.join(args.tiles_dir, '*.tsv'))):
        head, ntiles, sig = die_signature(path)
        if head is None:
            continue
        groups[(head[3], sig)].append(
            dict(device=head[2], part=head[1], ntiles=ntiles))
    out = []
    for (arch, sig), devs in sorted(groups.items()):
        out.append(dict(arch=arch, sig=sig, devices=devs))
        print(arch, sig, devs[0]['ntiles'], ' '.join(d['device'] for d in devs))
    with open(args.out, 'w') as f:
        json.dump(out, f, indent=1)


if __name__ == '__main__':
    main()
