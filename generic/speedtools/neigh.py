"""neigh.py <die> <design dir> <bit> <explain-regex>: for INT tiles, bit set vs
the site usage of the left / right neighbour tiles (same grid row)."""
import json, os, re, sys, collections
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'python'))
import dies as dieslib, mkdb, designdata as DD
die = dieslib.load()[sys.argv[1]]
d, bit, rx = sys.argv[2], sys.argv[3], re.compile(sys.argv[4])
col = mkdb.Collector(die, json.load(open(os.path.join(dieslib.DB, die.arch, die.name, 'tilegrid.json'))))
pos = {}
at = {}
for line in open(die.tiles_tsv):
    p = line.split()
    if p[0] != 'tile':
        continue
    pos[p[1]] = (p[2], int(p[3]), int(p[4]))
    at[(int(p[3]), int(p[4]))] = p[1]
ids = DD.load_bits(col.df, d)
rb = col.region_bits(ids)
feats = DD.load_features(d, col.sk)
c = collections.Counter()
for tile, rl in col.tile_regions.items():
    i = rl[0]
    if col.regions[i][1] != 'INT':
        continue
    fs = feats.get(tile, set())
    if any(rx.search(f) for f in fs):
        continue
    t, x, y = pos[tile]
    sides = []
    for dx, nm in ((-1, 'L'), (1, 'R')):
        nb = at.get((x + dx, y))
        nf = feats.get(nb, set()) if nb else set()
        used = any('.TYPE.' in f for f in nf)
        sides.append(f'{nm}:{pos[nb][0] if nb else "-"}:{"used" if used else "free"}')
    has = bit in rb.get(i, [])
    ints = 'intused' if fs else 'intfree'
    c[(has, ints) + tuple(sides)] += 1
for k, v in sorted(c.items(), key=lambda x: -x[1])[:30]:
    print(v, k)
