"""unexpl_tiles.py <die> <design dir> <tiletype> <bit> <explain-regex> [n]:
tiles of the type in the design with <bit> set but no feature matching
explain-regex; prints the tile names (for a Vivado look-up)."""
import json, os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'python'))
import dies as dieslib, mkdb
die = dieslib.load()[sys.argv[1]]
d, tt, bit, rx = sys.argv[2], sys.argv[3], sys.argv[4], re.compile(sys.argv[5])
n = int(sys.argv[6]) if len(sys.argv) > 6 else 20
col = mkdb.Collector(die, json.load(open(os.path.join(dieslib.DB, die.arch, die.name, 'tilegrid.json'))))
import designdata as DD
ids = DD.load_bits(col.df, d)
rb = col.region_bits(ids)
feats = DD.load_features(d, col.sk)
tot = hit = 0
out = []
for tile, rl in col.tile_regions.items():
    i = rl[0]
    if col.regions[i][1] != tt:
        continue
    if bit not in rb.get(i, []):
        continue
    tot += 1
    fs = feats.get(tile, set())
    if any(rx.search(f) for f in fs):
        hit += 1
    else:
        out.append((tile, len(fs)))
print(f'{tt} tiles with {bit}: {tot}, explained by regex {hit}, not {len(out)}')
for t, k in out[:n]:
    print(t, k)
