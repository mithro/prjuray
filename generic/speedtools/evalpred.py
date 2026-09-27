"""evalpred.py [--missed TILETYPE] <dbdir> <die> <design roots...>: prediction
accuracy of a database on designs (ideally designs not used to build it).

Per design, every tile region predicts its bits from the tile's features
(segbits: feature bits, "A&B" features need both, "!" bits clear defaults;
defaults set unless cleared).  The predictions are compared per die bit
address, over the bits owned by any tile region (bits of a frame column
shared by several tiles, e.g. 7-series INT + CLB, count once):

  correct  set, and predicted by some owner
  missed   set, and predicted by no owner
  false    predicted by some owner, not set

Totals, and per owning tile type (a bit counts for every type owning it):
set bits, missed bits, false bits (predicted by that type but not set).
--missed TT lists the most missed bits (relative to TT's regions).
"""
import collections, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'python'))
import numpy as np
import dies as dieslib
import designdata as DD
import mkdb

args = sys.argv[1:]
show = None
if args[0] == '--missed':
    show, args = args[1], args[2:]
dbdir, dn, roots = args[0], args[1], args[2:]
die = dieslib.load()[dn]
tg = json.load(open(os.path.join(dieslib.DB, die.arch, dn, 'tilegrid.json')))
col = mkdb.Collector(die, tg)
dbs = {}


def db(tt, k):
    key = (tt, k)
    if key not in dbs:
        suffix = tt.lower() + (f'.{k}' if k else '')
        feats = []
        p = os.path.join(dbdir, f'segbits_{suffix}.db')
        if os.path.exists(p):
            for l in open(p):
                f = l.split()
                name = f[0].split('.', 1)[1]
                feats.append((name.split('&'),
                              [b for b in f[1:] if not b.startswith('!')],
                              [b[1:] for b in f[1:] if b.startswith('!')]))
        d = set()
        p = os.path.join(dbdir, f'defaults_{suffix}.db')
        if os.path.exists(p):
            d = set(l.split()[0] for l in open(p) if l.strip())
        dbs[key] = (feats, d)
    return dbs[key]


def gid(i, b):
    """Die bit address of region i's relative bit b ("FF_BBB")."""
    _, _, fi, _, off, _ = col.regions[i]
    f, o = b.split('_')
    return (fi + int(f)) * col.nbf + off + int(o)


# Owners of every die bit address (up to 3 regions; 7-series INT + CLB
# frame columns have 2).
N = len(col.df.frames) * col.nbf
own = np.full((3, N), -1, dtype=np.int32)
for i, (_, _, fi, nfr, off, n) in enumerate(col.regions):
    for f in range(fi, fi + nfr):
        g = np.arange(f * col.nbf + off, f * col.nbf + off + n)
        for j in range(3):
            free = own[j, g] < 0
            own[j, g[free]] = i
            g = g[~free]
            if not len(g):
                break


def owners(g):
    return [int(x) for x in own[:, g] if x >= 0]


rtype = [r[1] for r in col.regions]
rkey = {}
for tile, rlist in col.tile_regions.items():
    for k, i in enumerate(rlist):
        rkey[i] = (col.regions[i][1], k)

tot = collections.Counter()
per = collections.defaultdict(collections.Counter)
missed_bits = collections.Counter()
set_bits = collections.Counter()
for d in DD.design_dirs(roots, v2only=True):
    ids = DD.load_bits(col.df, d)
    feats = DD.load_features(d, col.sk)
    ids = ids[ids < N]
    act = set(int(x) for x in ids[own[0, ids] >= 0])
    pred_by = collections.defaultdict(set)  # gid -> predicting regions
    for tile, rlist in col.tile_regions.items():
        fs = feats.get(tile, set())
        for i in rlist:
            tt, k = rkey[i]
            flist, defaults = db(tt, k)
            pred = set(defaults)
            clear = set()
            for names, pos, neg in flist:
                if all(n in fs for n in names):
                    pred.update(pos)
                    clear.update(neg)
            for b in pred - clear:
                pred_by[gid(i, b)].add(i)
    predicted = set(pred_by)
    tot['set'] += len(act)
    tot['correct'] += len(act & predicted)
    tot['missed'] += len(act - predicted)
    tot['false'] += len(predicted - act)
    for g in act:
        types = {rtype[i] for i in owners(g)}
        for t in types:
            per[t]['set'] += 1
        if g not in predicted:
            for t in types:
                per[t]['missed'] += 1
            if show:
                for i in owners(g):
                    if rtype[i] == show:
                        _, _, fi, _, off, _ = col.regions[i]
                        f, o = divmod(g, col.nbf)
                        missed_bits[f'{f - fi:02d}_{o - off:03d}'] += 1
    for g in predicted - act:
        for t in {rtype[i] for i in pred_by[g]}:
            per[t]['false'] += 1
print(f'TOTAL set {tot["set"]} correct {tot["correct"]} missed '
      f'{tot["missed"]} false {tot["false"]}')
for tt, c in sorted(per.items(), key=lambda x: -x[1]['missed'])[:30]:
    print(f'  {tt:24s} set {c["set"]:8d} missed {c["missed"]:7d} '
          f'false {c["false"]:7d}')
if show:
    print(f'most missed bits of {show}:')
    for b, n in missed_bits.most_common(40):
        print(f'  {b} {n}')
