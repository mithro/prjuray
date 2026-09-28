#!/usr/bin/env python3
"""Target list for directed PIP coverage (gen_design.py --pips): the PIPs of
a die whose feature has few samples in the database (counts_<tile>.txt),
including ones never seen.

    rarepips.py <db dir> <meta pips file> [--max N] [--types INT_L,INT_R]
                [--skip-clock] > targets.txt

Output lines: <tile type> <wire0> <wire1> <dir> <count>, rarest first
(the format gen_design --pips reads; the count is informational).
"""
import argparse
import collections
import os
import re

ap = argparse.ArgumentParser()
ap.add_argument('db')
ap.add_argument('meta')
ap.add_argument('--max', type=int, default=10,
                help='targets: PIP features with at most this many samples')
ap.add_argument('--types', default=None,
                help='comma separated tile types (default: all)')
ap.add_argument('--skip-clock', action='store_true',
                help='drop GCLK/HCLK/CLK wires (find_routing_path trouble)')
ap.add_argument('--stats', action='store_true',
                help='only print per tile type totals')
args = ap.parse_args()
types = set(args.types.split(',')) if args.types else None

counts = {}
for f in os.listdir(args.db):
    m = re.match(r'counts_(.*)\.txt$', f)
    if not m:
        continue
    for line in open(os.path.join(args.db, f)):
        p = line.split()
        if len(p) == 2:
            counts[p[0]] = int(p[1])

rows = []
seen = set()
stat = collections.defaultdict(collections.Counter)
for line in open(args.meta):
    p = line.split()
    if len(p) < 5 or p[0] != 'pip':
        continue
    tt, w0, w1, dirn = p[1], p[2], p[3], p[4]
    if types and tt not in types:
        continue
    if (tt, w0, w1) in seen:
        continue
    seen.add((tt, w0, w1))
    if args.skip_clock and re.search(r'GCLK|HCLK|CLK', w0 + w1):
        continue
    # Feature names: <TT>.<w0>-><w1> (bidirectional PIPs as seen used).
    n = counts.get(f'{tt}.{w0}->{w1}', 0)
    if dirn != '1':
        n = max(n, counts.get(f'{tt}.{w1}->{w0}', 0))
    stat[tt]['pips'] += 1
    stat[tt]['never' if n == 0 else 'rare' if n <= args.max else 'ok'] += 1
    if n <= args.max:
        rows.append((n, tt, w0, w1, dirn))
if args.stats:
    for tt in sorted(stat, key=lambda t: -stat[t]['never'] - stat[t]['rare']):
        s = stat[tt]
        if s['never'] or s['rare']:
            print(f"{tt:28s} pips {s['pips']:5d} never {s['never']:5d} "
                  f"rare {s['rare']:5d}")
else:
    for n, tt, w0, w1, dirn in sorted(rows):
        print(tt, w0, w1, dirn, n)
