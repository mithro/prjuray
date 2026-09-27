"""evalpred.py <dbdir> <die> <design roots...>: prediction accuracy of a
database on designs: per tile instance, the bits predicted from the tile's
features (segbits: feature bits; "A&B" features need both; defaults set
unless cleared by a present feature's "!" bit) compared with the actual bits.
Prints totals and per tile type: actual bits, correctly predicted, missed
(actual not predicted), false (predicted not actual)."""
import collections, glob, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'python'))
import dies as dieslib
import designdata as DD
import mkdb

dbdir, dn, roots = sys.argv[1], sys.argv[2], sys.argv[3:]
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


tot = collections.Counter()
per = collections.defaultdict(collections.Counter)
for d in DD.design_dirs(roots, v2only=True):
    for tt, k, fs, bits in col.samples(d):
        feats, defaults = db(tt, k)
        pred = set(defaults)
        clear = set()
        for names, pos, neg in feats:
            if all(n in fs for n in names):
                pred.update(pos)
                clear.update(neg)
        pred -= clear
        act = set(bits)
        c = per[tt]
        c['actual'] += len(act)
        c['correct'] += len(act & pred)
        c['missed'] += len(act - pred)
        c['false'] += len(pred - act)
for tt, c in per.items():
    tot.update(c)
print(f'TOTAL actual {tot["actual"]} correct {tot["correct"]} missed '
      f'{tot["missed"]} false {tot["false"]}')
for tt, c in sorted(per.items(), key=lambda x: -x[1]['missed'])[:25]:
    print(f'  {tt:24s} actual {c["actual"]:8d} missed {c["missed"]:7d} '
          f'false {c["false"]:7d}')
