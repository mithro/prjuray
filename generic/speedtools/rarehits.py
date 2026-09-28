#!/usr/bin/env python3
"""Coverage of rare PIP features per CPU hour, per round.

    rarehits.py <db dir> <round dir>... [--max 10]

For each round directory (<designs>/<die>/<tag>) sums over its ok designs:
CPU hours (run.stats), PIP occurrences whose database sample count is at
most --max ("rare", incl. never seen), the distinct rare features hit, and
PIP features absent from the database.  Rates are per CPU hour.
"""
import argparse
import glob
import gzip
import json
import os
import re

ap = argparse.ArgumentParser()
ap.add_argument('db')
ap.add_argument('rounds', nargs='+')
ap.add_argument('--max', type=int, default=10)
args = ap.parse_args()

counts = {}
for f in os.listdir(args.db):
    if re.match(r'counts_.*\.txt$', f):
        for line in open(os.path.join(args.db, f)):
            p = line.split()
            if len(p) == 2:
                counts[p[0]] = int(p[1])

print(f'{"round":34s} {"ok":>4s} {"fail":>4s} {"cpuh":>6s} {"rare occ":>8s} '
      f'{"distinct":>8s} {"new":>5s} {"occ/cpuh":>9s} {"dist/cpuh":>9s}')
for r in args.rounds:
    cpu = 0.0
    ok = fail = occ = 0
    distinct = set()
    new = set()
    for st in glob.glob(os.path.join(r, 's*', 'run.stats')):
        d = os.path.dirname(st)
        try:
            s = json.load(open(st))
        except ValueError:
            continue
        cpu += s.get('cpu', 0)
        f = os.path.join(d, 'design.features.gz')
        if s.get('status') != 'ok' or not os.path.exists(f):
            fail += 1
            continue
        ok += 1
        with gzip.open(f, 'rt') as fh:
            for line in fh:
                if not line.startswith('pip '):
                    continue
                p = line.split()
                name = f'{p[2]}.{p[3]}->{p[4]}'
                n = counts.get(name, 0)
                if n <= args.max:
                    occ += 1
                    distinct.add(name)
                    if n == 0:
                        new.add(name)
    h = cpu / 3600 or 1e-9
    print(f'{r[-34:]:34s} {ok:4d} {fail:4d} {cpu/3600:6.2f} {occ:8d} '
          f'{len(distinct):8d} {len(new):5d} {occ/h:9.1f} {len(distinct)/h:9.1f}')
