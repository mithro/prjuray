"""Compare two feature dumps as line sets (ignoring '#' comment lines)."""
import sys, gzip, collections


def load(p):
    f = gzip.open(p, 'rt') if p.endswith('.gz') else open(p)
    lines = [l.rstrip('\n') for l in f if not l.startswith('#')]
    return collections.Counter(lines), [l for l in open(p) if l.startswith('# t_')] if not p.endswith('.gz') else []


a, ta = load(sys.argv[1])
b, tb = load(sys.argv[2])
print('A', sum(a.values()), ''.join(ta).replace('\n', ' '))
print('B', sum(b.values()), ''.join(tb).replace('\n', ' '))
oa = a - b
ob = b - a
print('onlyA', sum(oa.values()), 'onlyB', sum(ob.values()))
for l in list(oa)[:10]:
    print('  A:', l)
for l in list(ob)[:10]:
    print('  B:', l)
sys.exit(1 if oa or ob else 0)
