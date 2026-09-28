"""conflict.py <die> <tags> <type> <bit> [max designs]: samples whose full tile
feature set occurs several times: how often the bit differs between them
(the bit is then not a function of the tile's dumped features)."""
import json, os, sys, collections
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'python'))
import dies as dieslib, mkdb, designdata as DD
die = dieslib.load()[sys.argv[1]]
tags, tt, bit = sys.argv[2].split(','), sys.argv[3], sys.argv[4]
mx = int(sys.argv[5]) if len(sys.argv) > 5 else 20
col = mkdb.Collector(die, json.load(open(os.path.join(dieslib.DB, die.arch, die.name, 'tilegrid.json'))))
g = collections.defaultdict(lambda: [0, 0])
roots = [os.path.join(dieslib.BUILD, 'designs', die.name, t) for t in tags]
for d in DD.design_dirs(roots, v2only=True)[:mx]:
    for t, k, fs, bits in col.samples(d):
        if t != tt or k != 0:
            continue
        key = frozenset(fs)
        g[key][bit in bits] += 1
multi = [(v, k) for k, v in g.items() if sum(v) >= 2]
mixed = [(v, k) for v, k in multi if v[0] and v[1]]
print(f'distinct feature sets {len(g)}, repeated {len(multi)}, with both bit values {len(mixed)}')
for v, k in sorted(mixed, key=lambda x: -sum(x[0]))[:8]:
    print(f'  bit0 {v[0]} bit1 {v[1]} nfeat {len(k)} e.g. {sorted(k)[:6]}')
