"""Equivalence of the new Collector.region_bits / unowned with the old one
($EQ_SCRATCH/old = the $BASE tree, see common.sh)."""
import sys, os, time, json
S = os.environ['EQ_SCRATCH']
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'python'))

import importlib.util
import dies as dieslib, designdata as DD, mkdb
spec = importlib.util.spec_from_file_location('mkdb_old', os.path.join(S, 'old/generic/python/mkdb.py'))
mkdb_old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mkdb_old)
dn, tag, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
die = dieslib.load()[dn]
tg = json.load(open(os.path.join(dieslib.DB, die.arch, dn, 'tilegrid.json')))
new = mkdb.Collector(die, tg)
old = mkdb_old.Collector(die, tg)
to = tn = 0
for d in DD.design_dirs(os.path.join(dieslib.BUILD, 'designs', dn, tag))[:n]:
    ids = DD.load_bits(new.df, d)
    t = time.time(); a = old.region_bits(ids); ua = old.unowned(ids); ha = old.hidden; t1 = time.time()
    b = new.region_bits(ids); ub = new.unowned(ids); hb = new.hidden; t2 = time.time()
    to += t1 - t; tn += t2 - t1
    same = list(a.items()) == list(b.items()) and (ua, ha) == (ub, hb)
    print(d, len(ids), 'SAME' if same else 'DIFF', ua, ha, f'old {t1-t:.2f}s new {t2-t1:.2f}s', flush=True)
    if not same:
        sys.exit(1)
print(f'ALL SAME old {to:.1f}s new {tn:.1f}s')
