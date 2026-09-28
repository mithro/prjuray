import os, re, sys, glob, json, collections
# Region designs: global clock buffer count (by site type) vs router hang.
root = '/home/tim/github/f4pga/prjuray/build/designs'
rows = []
for d in glob.glob(root + '/*/*/s*'):
    if not os.path.exists(os.path.join(d, 'design.region')):
        continue
    tcl = open(os.path.join(d, 'design.tcl'), errors='replace').read()
    locs = re.findall(r'\b(BUFG\w*|BUFCE\w*)_X\d+Y\d+', tcl)
    c = collections.Counter(locs)
    vl = os.path.join(d, 'vivado.log')
    hang = False
    if os.path.exists(vl):
        ov = re.findall(r'Number of Nodes with overlaps = (\d+)', open(vl, errors='replace').read())
        hang = len(ov) > 30 and len(set(ov[-20:])) == 1
    rows.append((sum(c.values()), hang, d.replace(root + '/', ''), dict(c)))
rows.sort()
for r in rows:
    if r[0] >= 15 or r[1]:
        print(r)
print('hang by total-bufg bucket:')
b = collections.defaultdict(lambda: [0, 0])
for n, h, _, _ in rows:
    b[min(n // 10 * 10, 60)][h] += 1
for k in sorted(b):
    print(k, 'ok/hang', b[k])
