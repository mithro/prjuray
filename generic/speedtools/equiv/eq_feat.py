"""eq_feat.py <old features.py> <die> <tag> <n>: tile_features of the
worktree features.py vs an older copy must be identical on n designs."""
import sys, os, time, importlib.util
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'python'))

import dies as dieslib, designdata as DD, features as new
spec = importlib.util.spec_from_file_location('features_old', sys.argv[1])
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)
dn, tag, n = sys.argv[2], sys.argv[3], int(sys.argv[4])
die = dieslib.load()[dn]
sk_new = new.SiteKeys(die.tiles_tsv)
sk_old = old.SiteKeys(die.tiles_tsv)
to = tn = 0
nf = 0
for d in DD.design_dirs(os.path.join(dieslib.BUILD, 'designs', dn, tag), v2only=True)[:n]:
    p = os.path.join(d, 'design.features.gz')
    t = time.time(); a = old.tile_features(p, sk_old); t1 = time.time()
    b = new.tile_features(p, sk_new); t2 = time.time()
    to += t1 - t; tn += t2 - t1
    # EQ_IGNORE: regexp of features left out of the comparison (e.g. the
    # features only one side's code has, when comparing merged code)
    ign = os.environ.get('EQ_IGNORE')
    if ign:
        import re
        r = re.compile(ign)
        a = {k: {f for f in v if not r.search(f)} for k, v in a.items()}
        b = {k: {f for f in v if not r.search(f)} for k, v in b.items()}
    a = {k: v for k, v in a.items() if v}
    b = {k: v for k, v in b.items() if v}
    same = a == b
    nf += sum(len(v) for v in b.values())
    print(d, 'SAME' if same else 'DIFF', sum(len(v) for v in b.values()), f'old {t1-t:.2f}s new {t2-t1:.2f}s', flush=True)
    if not same:
        for k in sorted(set(a) | set(b)):
            if a.get(k) != b.get(k):
                print(' ', k, 'old-only', sorted(a.get(k, set()) - b.get(k, set()))[:5], 'new-only', sorted(b.get(k, set()) - a.get(k, set()))[:5])
        sys.exit(1)
# LUT equation function over every equation seen (old cache keys)
bad = 0
for e in list(old._EQN_CACHE):
    if old.lut_eqn_bits(e) != new.lut_eqn_bits(e):
        bad += 1
print(f'ALL SAME {nf} features; old {to:.1f}s new {tn:.1f}s; eqn mismatches {bad} of {len(old._EQN_CACHE)}')
