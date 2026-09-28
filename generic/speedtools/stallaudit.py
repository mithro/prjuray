#!/usr/bin/env python3
"""Audit run_designs' router stall kills in a round.

    stallaudit.py <tag> [build/designs] [--rerun DIR]

Prints status counts per die and, for every design killed as 'stall', the
overlap counts of its last route_design call (from the kept vivado.log;
with --reuse the worker log is gone, so the count sequence may be missing).
With --rerun DIR it writes DIR/rerun.cmds: one generic/speedtools/rerun.sh
line per stalled design with NL_STALL_ITERS=0, to check that the design
would not have finished given the full time limit (a false kill would end
'ok' there).  --check DIR then reports those reruns' outcome.
"""
import argparse
import collections
import glob
import json
import os
import re

ap = argparse.ArgumentParser()
ap.add_argument('tag')
ap.add_argument('root', nargs='?', default='build/designs')
ap.add_argument('--rerun', help='write DIR/rerun.cmds')
ap.add_argument('--check', help='report reruns in DIR')
args = ap.parse_args()
here = os.path.dirname(os.path.abspath(__file__))

counts = collections.defaultdict(collections.Counter)
stalled = []
for st in sorted(glob.glob(f'{args.root}/*/{args.tag}/s*/run.stats')):
    d = os.path.dirname(st)
    die = d.split('/')[-3]
    try:
        s = json.load(open(st))
    except ValueError:
        continue
    counts[die][s.get('status')] += 1
    if s.get('status') == 'stall':
        stalled.append((d, s))
tot = collections.Counter()
for die in sorted(counts):
    tot.update(counts[die])
    print(f'{die:10s}', dict(counts[die]))
print('total', dict(tot))
for d, s in stalled:
    note = ''
    nl = os.path.join(d, 'nl.log')
    if os.path.exists(nl):
        m = re.findall(r'^route stalled: .*$', open(nl).read(), re.M)
        note = m[-1] if m else ''
    seq = ''
    vl = os.path.join(d, 'vivado.log')
    if os.path.exists(vl):
        call = open(vl, errors='replace').read().split(
            'Command: route_design')[-1]
        seq = ' '.join(re.findall(r'Nodes with overlaps = (\d+)', call))
    print(f'stall {d} wall {s["wall"]:.0f}s: {note} [{seq}]')

if args.rerun:
    os.makedirs(args.rerun, exist_ok=True)
    with open(os.path.join(args.rerun, 'rerun.cmds'), 'w') as f:
        for d, _ in stalled:
            name = '_'.join(d.split('/')[-3:])
            f.write(f'TMO=3000 {here}/rerun.sh {os.path.abspath(d)} '
                    f'{os.path.abspath(args.rerun)}/{name} NL_STALL_ITERS=0\n')
    print(f'{len(stalled)} reruns in {args.rerun}/rerun.cmds')

if args.check:
    for done in sorted(glob.glob(f'{args.check}/*/done')):
        d = os.path.dirname(done)
        log = open(os.path.join(d, 'nl.log')).read() \
            if os.path.exists(os.path.join(d, 'nl.log')) else ''
        ok = re.search(r'^done \d+', log, re.M)
        wall = ''
        t = os.path.join(d, 'vivado.log.time')
        if os.path.exists(t):
            m = re.search(r'Elapsed .*: (\S+)', open(t).read())
            wall = m.group(1) if m else ''
        print(f'{os.path.basename(d)}: {open(done).read().strip()} '
              f'{"FINISHED (false kill?)" if ok else "not finished"} {wall}')
