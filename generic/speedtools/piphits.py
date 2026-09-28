#!/usr/bin/env python3
"""Which directed PIP targets (nl_want_pip lines of design.tcl) ended up
used in the design's feature dump.

    piphits.py <design dir>...
"""
import gzip
import os
import re
import sys

tot_w = tot_h = 0
for d in sys.argv[1:]:
    want = set()
    for line in open(os.path.join(d, 'design.tcl')):
        m = re.match(r'nl_want_pip \S+ (\S+)/(\S+?)\.(\S+?)(?:->>|<<->>)(\S+)$',
                     line.strip())
        if m:
            want.add((m.group(1), m.group(3), m.group(4)))
    fn = os.path.join(d, 'design.features.gz')
    if not os.path.exists(fn):
        fn = os.path.join(d, 'design.features')
    op = gzip.open if fn.endswith('.gz') else open
    used = set()
    with op(fn, 'rt') as f:
        for line in f:
            if line.startswith('pip '):
                p = line.split()
                used.add((p[1], p[3], p[4]))
                used.add((p[1], p[4], p[3]))
    hit = want & used
    tot_w += len(want)
    tot_h += len(hit)
    print(f'{d}: wanted {len(want)} used {len(hit)}')
print(f'total wanted {tot_w} used {tot_h}')
