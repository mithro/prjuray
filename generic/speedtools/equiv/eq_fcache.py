"""eq_fcache.py <die> <tag> <n>: designdata.load_features through its
caches must equal features.tile_features: cold (parse + derive, both
caches written), feature cache hit, and parse cache hit only (the
feature cache removed, as after an edit of derived features).  The dumps
are copied into $EQ_SCRATCH/fcache (the caches follow real paths)."""
import glob
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)),
                                'python'))
import dies as dieslib  # noqa: E402
import designdata as DD  # noqa: E402
import features as F  # noqa: E402

dn, tag, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
S = os.path.join(os.environ['EQ_SCRATCH'], 'fcache')
die = dieslib.load()[dn]
sk = F.SiteKeys(die.tiles_tsv)
src = DD.design_dirs(os.path.join(dieslib.BUILD, 'designs', dn, tag),
                     v2only=True)[:n]
T = dict(direct=0, cold=0, hit=0, parsehit=0)
for d in src:
    x = os.path.join(S, 'designs', dn, tag, os.path.basename(d))
    shutil.rmtree(x, ignore_errors=True)
    os.makedirs(x)
    shutil.copy2(os.path.join(d, 'design.features.gz'), x)
    for c in glob.glob(os.path.join(S, 'cache', '*', dn, tag,
                                    os.path.basename(d) + '.*')):
        os.remove(c)
    t = time.time()
    a = F.tile_features(os.path.join(x, 'design.features.gz'), sk)
    T['direct'] += time.time() - t
    t = time.time()
    b = DD.load_features(x, sk)
    T['cold'] += time.time() - t
    t = time.time()
    c = DD.load_features(x, sk)
    T['hit'] += time.time() - t
    os.remove(DD.feature_cache_path(x, sk))
    t = time.time()
    e = DD.load_features(x, sk)
    T['parsehit'] += time.time() - t
    norm = [{k: v for k, v in z.items() if v} for z in (a, b, c, e)]
    ok = all(z == norm[0] for z in norm)
    print(os.path.basename(d), 'SAME' if ok else 'DIFF', flush=True)
    if not ok:
        sys.exit(1)
print('ALL SAME', ', '.join(f'{k} {v:.1f} s' for k, v in T.items()))
