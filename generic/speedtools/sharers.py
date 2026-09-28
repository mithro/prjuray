"""sharers.py <die> <tiletype> <bit> [n]: other tile regions owning the same die
address as <bit> of <tiletype> region 0, for the first n tiles of the type."""
import collections, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'python'))
import dies as dieslib, mkdb
die = dieslib.load()[sys.argv[1]]
tt, bit = sys.argv[2], sys.argv[3]
n = int(sys.argv[4]) if len(sys.argv) > 4 else 2000
col = mkdb.Collector(die, json.load(open(os.path.join(dieslib.DB, die.arch, die.name, 'tilegrid.json'))))
f, o = map(int, bit.split('_'))
c = collections.Counter()
m = 0
for tile, rl in col.tile_regions.items():
    i = rl[0]
    name, t, fi, nfr, off, nb = col.regions[i]
    if t != tt:
        continue
    m += 1
    if m > n:
        break
    fr, ob = fi + f, off + o
    others = []
    for off2, n2, j in col.by_frame.get(fr, ()):
        if j != i and off2 <= ob < off2 + n2:
            r = col.regions[j]
            k = col.tile_regions[r[0]].index(j)
            others.append(f'{r[1]}.{k}:{fr - r[2]:02d}_{ob - off2:03d}')
    c[' '.join(sorted(others)) or '-'] += 1
for k, v in c.most_common(10):
    print(v, k)
