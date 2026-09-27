"""info.py <die> <tag> [<tag>...]: information content of designs.

Per tag: ok designs, CPU hours, distinct tile-type features (union), mean
features per design, feature observations, and rates per CPU hour.
"""
import sys, os, json, glob, collections
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'python'))
pass  # URAY_BUILD from the environment
import dies as dieslib
import features as featlib

die = dieslib.load()[sys.argv[1]]
sk = featlib.SiteKeys(die.tiles_tsv)
B = os.path.join(dieslib.BUILD, 'designs', die.name)
for tag in sys.argv[2:]:
    union = set()
    obs = 0
    nok = 0
    cpu = 0.0
    wall = 0.0
    nall = 0
    rare = collections.Counter()
    for d in sorted(glob.glob(os.path.join(B, tag, 's*'))):
        try:
            st = json.load(open(d + '/run.stats'))
        except (OSError, ValueError):
            continue
        nall += 1
        cpu += st.get('cpu') or 0
        wall += st.get('wall') or 0
        if not os.path.exists(d + '/bits.npz'):
            continue
        nok += 1
        f = featlib.tile_features(d + '/design.features.gz', sk)
        s = set()
        for tile, fs in f.items():
            tt = sk.tile_type.get(tile, '?')
            for x in fs:
                s.add((tt, x))
            obs += len(fs)
        union |= s
        for x in s:
            rare[x] += 1
    if not nall:
        continue
    h = cpu / 3600
    print(f'{tag:14s} designs {nall:3d} ok {nok:3d} cpu {h:6.2f} h wall(slot) {wall/3600:6.2f} h | '
          f'ok/cpu-h {nok/max(h,1e-9):6.2f} | distinct {len(union):7d} '
          f'({len(union)/max(h,1e-9):8.0f}/cpu-h) | obs/design {obs/max(nok,1):9.0f} obs/cpu-h {obs/max(h,1e-9):10.0f} '
          f'| features seen in <=2 designs {sum(1 for v in rare.values() if v <= 2)}')
