"""dbdelta.py <base.out> <fix.out> <base check prefix> <fix check prefix>:
per tile type unexplained bits and undocumented bits (check) before/after,
plus examples of new pair features."""
import os, re, sys, glob, collections
A, B, ca, cb = sys.argv[1:5]


def unexpl(d):
    r = {}
    for p in glob.glob(os.path.join(d, 'unexplained_*.txt')):
        t = os.path.basename(p)[len('unexplained_'):-4]
        r[t] = sum(1 for _ in open(p))
    return r


def pairs(d):
    r = collections.defaultdict(list)
    for p in glob.glob(os.path.join(d, 'segbits_*.db')):
        for l in open(p):
            f = l.split()
            if '&' in f[0]:
                r[os.path.basename(p)[8:-3]].append(l.strip())
    return r


def undoc(prefix):
    r = {}
    for p in glob.glob(prefix + '*'):
        for l in open(p):
            m = re.match(r'^(\S+): (\d+) \((\d+) distinct\)', l)
            if m:
                r[m.group(1)] = r.get(m.group(1), 0) + int(m.group(2))
        tot = [l for l in open(p) if re.search(r'undocumented \d+', l)]
    return r


ua, ub = unexpl(A), unexpl(B)
print('unexplained bits (base -> fix), changed types:')
tot = [0, 0]
for t in sorted(set(ua) | set(ub)):
    a, b = ua.get(t, 0), ub.get(t, 0)
    tot[0] += a
    tot[1] += b
    if a != b:
        print(f'  {t:28s} {a:6d} -> {b:6d} ({b - a:+d})')
print(f'  TOTAL {tot[0]} -> {tot[1]} ({tot[1] - tot[0]:+d})')
da, db = undoc(ca), undoc(cb)
print('undocumented set bits in check (base -> fix), changed types:')
tot = [0, 0]
for t in sorted(set(da) | set(db)):
    a, b = da.get(t, 0), db.get(t, 0)
    tot[0] += a
    tot[1] += b
    if a != b:
        print(f'  {t:28s} {a:6d} -> {b:6d} ({b - a:+d})')
print(f'  TOTAL {tot[0]} -> {tot[1]} ({tot[1] - tot[0]:+d})')
pa, pb = pairs(A), pairs(B)
print('pair features: base', sum(len(v) for v in pa.values()), 'fix',
      sum(len(v) for v in pb.values()))
for t in sorted(pb, key=lambda t: -len(pb[t]))[:8]:
    print(f'  {t}: {len(pb[t])} e.g.')
    for l in pb[t][:3]:
        print('     ', l[:200])
