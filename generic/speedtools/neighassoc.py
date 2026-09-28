"""neighassoc.py <die> <tags> <bit> <dx> [max designs]: INT <bit> vs the
features of the neighbour tile at grid x+dx: P(bit | feature) and coverage."""
import json, os, re, sys, collections
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'python'))
import dies as dieslib, mkdb, designdata as DD
die = dieslib.load()[sys.argv[1]]
tags, bit, dx = sys.argv[2].split(','), sys.argv[3], int(sys.argv[4])
mx = int(sys.argv[5]) if len(sys.argv) > 5 else 8
col = mkdb.Collector(die, json.load(open(os.path.join(dieslib.DB, die.arch, die.name, 'tilegrid.json'))))
pos, at = {}, {}
for line in open(die.tiles_tsv):
    p = line.split()
    if p[0] == 'tile':
        pos[p[1]] = (p[2], int(p[3]), int(p[4]))
        at[(int(p[3]), int(p[4]))] = p[1]
nf, nfb = collections.Counter(), collections.Counter()
nb = n = 0
roots = [os.path.join(dieslib.BUILD, 'designs', die.name, t) for t in tags]
for d in DD.design_dirs(roots, v2only=True)[:mx]:
    ids = DD.load_bits(col.df, d)
    rb = col.region_bits(ids)
    feats = DD.load_features(d, col.sk)
    for tile, rl in col.tile_regions.items():
        i = rl[0]
        if col.regions[i][1] != 'INT':
            continue
        t, x, y = pos[tile]
        nbt = at.get((x + dx, y))
        if not nbt:
            continue
        has = bit in rb.get(i, [])
        n += 1
        nb += has
        tt = pos[nbt][0]
        for f in feats.get(nbt, ()):
            k = f'{tt}.{f}'
            nf[k] += 1
            if has:
                nfb[k] += 1
print(f'samples {n} with bit {nb}')
rows = [(nfb[f] / nf[f], nfb[f], nf[f], f) for f in nf if nfb[f] >= 20]
rows.sort(key=lambda r: (-r[0], -r[1]))
for p, a, b, f in rows[:30]:
    print(f'  P={p:.3f} {a:6d}/{b:6d} cov={a / max(nb, 1):.3f} {f}')
