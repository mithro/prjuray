"""sbdiff.py <db A> <db B> <segbits file>: features whose bits differ, and
bits that lose / gain features, with counts from counts files."""
import collections
import sys

A, B, f = sys.argv[1:4]


def load(d):
    sb = {}
    for line in open(f'{d}/{f}'):
        p = line.split()
        sb[p[0]] = set(p[1:])
    cnt = {}
    for line in open(f'{d}/{f.replace("segbits_", "counts_").replace(".db", ".txt")}'):
        p = line.split()
        cnt[p[0]] = int(p[1])
    return sb, cnt


sa, ca = load(A)
sb, cb = load(B)
lost = collections.Counter()
gain = collections.Counter()
rows = []
for k in set(sa) | set(sb):
    a, b = sa.get(k, set()), sb.get(k, set())
    if a != b:
        rows.append((ca.get(k, 0), cb.get(k, 0), k, sorted(a - b), sorted(b - a)))
        for x in a - b:
            lost[x] += 1
        for x in b - a:
            gain[x] += 1
print(f'features differing {len(rows)}; only in A {len(set(sa) - set(sb))}, only in B {len(set(sb) - set(sa))}')
# bits that lost all their features
bitsA = collections.defaultdict(set)
bitsB = collections.defaultdict(set)
for k, v in sa.items():
    for x in v:
        bitsA[x].add(k)
for k, v in sb.items():
    for x in v:
        bitsB[x].add(k)
orphan = [x for x in bitsA if x not in bitsB]
print('bits with features in A but none in B:', len(orphan), orphan[:30])
for x in orphan[:15]:
    print('  ', x, 'A features:', [(k, ca.get(k)) for k in sorted(bitsA[x])][:6])
rows.sort(key=lambda r: -(len(r[3]) + len(r[4])))
for r in rows[:25]:
    print(r[0], r[1], r[2], 'lost', r[3][:8], 'gained', r[4][:8])
