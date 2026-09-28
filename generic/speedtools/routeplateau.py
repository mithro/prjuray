#!/usr/bin/env python3
"""Plateaus of route_design: runs of identical nonzero "Number of Nodes with
overlaps" counts.  Reports the plateaus the router got out of (a later lower
count) and the ones it was still on when the call ended or was killed:
the basis of run_designs.StallWatch's NL_STALL_ITERS threshold.

    routeplateau.py [build/designs] [--min 3]
"""
import argparse
import collections
import glob
import os
import re

ap = argparse.ArgumentParser()
ap.add_argument('root', nargs='?', default='build/designs')
ap.add_argument('--min', type=int, default=3)
args = ap.parse_args()
esc = collections.Counter()
stuck = collections.Counter()
stuck_ex = []
for vl in sorted(glob.glob(args.root + '/*/*/s*/vivado.log')):
    if not os.path.exists(vl) or os.path.getsize(vl) > 300e6:
        continue
    txt = open(vl, errors='replace').read()
    for call in txt.split('Command: route_design')[1:]:
        ov = [int(x) for x in re.findall(r'Nodes with overlaps = (\d+)', call)]
        run = 0
        for i in range(1, len(ov)):
            if ov[i] == ov[i - 1] and ov[i]:
                run += 1
                continue
            if run >= args.min and ov[i] < ov[i - 1]:
                esc[run] += 1
                print(f'escaped after {run} at {ov[i - 1]} -> {ov[i]}: {vl}')
            run = 0
        if run >= args.min:
            stuck[run] += 1
            stuck_ex.append(f'{run}:{os.path.relpath(vl, args.root)}')
print('escaped plateaus (length: count)', sorted(esc.items()))
print('plateau at end of call (length: count)', sorted(stuck.items()))
for s in stuck_ex:
    print('  stuck', s)
