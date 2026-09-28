#!/usr/bin/env python3
"""Router hangs vs BUFG_PS use: for every design under <designs root>
(default build/designs) count the BUFG_PS / other global buffer LOCs in
design.tcl and whether route_design got stuck (the last 20 overlap counts
of a long run all equal), split by --region designs (design.region).

    bufgps.py [build/designs]
"""
import collections
import glob
import os
import re
import sys

root = sys.argv[1] if len(sys.argv) > 1 else 'build/designs'
tab = collections.Counter()
ex = collections.defaultdict(list)
buckets = collections.defaultdict(lambda: [0, 0])
for d in sorted(glob.glob(root + '/*/*/s*')):
    tcl = os.path.join(d, 'design.tcl')
    if not os.path.exists(tcl):
        continue
    locs = collections.Counter(re.findall(
        r'\b(BUFG\w*|BUFCE\w*)_X\d+Y\d+', open(tcl, errors='replace').read()))
    ps = locs.get('BUFG_PS', 0)
    vl = os.path.join(d, 'vivado.log')
    hang = False
    if os.path.exists(vl) and os.path.getsize(vl) < 300e6:
        ov = re.findall(r'Nodes with overlaps = (\d+)',
                        open(vl, errors='replace').read())
        hang = len(ov) > 30 and len(set(ov[-20:])) == 1
    reg = os.path.exists(os.path.join(d, 'design.region'))
    tab[reg, ps > 0, hang] += 1
    if reg:
        buckets[min(ps // 10 * 10, 40)][hang] += 1
    if hang:
        ex[reg, ps > 0].append(f'{os.path.relpath(d, root)}({ps})')
for k in sorted(tab):
    print('region=%d bufg_ps=%d hang=%d: %d' % (k[0], k[1], k[2], tab[k]))
print('region designs, hang rate by BUFG_PS count bucket (ok, hang):')
for k in sorted(buckets):
    print(f'  {k:3d}+ {buckets[k]}')
for k in sorted(ex):
    print('hung region=%d bufg_ps=%d:' % k, ' '.join(ex[k][:20]))
