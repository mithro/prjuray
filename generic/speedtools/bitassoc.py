"""bitassoc.py <die> <tags> <tiletype> <bit> <exclude-regex> [max designs]:
features most associated with a bit, over the samples that have no feature
matching exclude-regex: for each feature f, n(f), n(f & bit), P(bit|f),
and coverage n(f & bit)/n(bit)."""
import collections, json, os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'python'))
import designdata, dies as dieslib, mkdb
die = dieslib.load()[sys.argv[1]]
tags, tt, bit, excl = sys.argv[2].split(','), sys.argv[3], sys.argv[4], re.compile(sys.argv[5])
mx = int(sys.argv[6]) if len(sys.argv) > 6 else 10
col = mkdb.Collector(die, json.load(open(os.path.join(dieslib.DB, die.arch, die.name, 'tilegrid.json'))))
roots = [os.path.join(dieslib.BUILD, 'designs', die.name, t) for t in tags]
nf = collections.Counter()
nfb = collections.Counter()
nb = 0
n = 0
for d in designdata.design_dirs(roots, v2only=True)[:mx]:
    for t, k, fs, bits in col.samples(d):
        if t != tt or k != 0:
            continue
        if any(excl.search(f) for f in fs):
            continue
        n += 1
        has = bit in bits
        nb += has
        for f in fs:
            nf[f] += 1
            if has:
                nfb[f] += 1
print(f'samples {n} with bit {nb}')
rows = [(nfb[f] / nf[f], nfb[f], nf[f], f) for f in nf if nfb[f] >= 5]
rows.sort(key=lambda r: (-r[0], -r[1]))
for p, a, b, f in rows[:40]:
    print(f'  P={p:.3f} {a:6d}/{b:6d} cov={a / max(nb, 1):.3f} {f}')
