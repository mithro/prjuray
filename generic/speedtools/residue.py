#!/usr/bin/env python3
"""residue.py [--tilegrid TG] [--max N] [--rx RE] [--show N] <dbdir> <die>
<tile type> <bit[,bit...]> <design roots...>: context of undocumented bits.

For every tile region of the type (region index 0) in the designs, decodes
its bits with the database (check.Database.decode, the region's own
features only: bits another owner documents count as undocumented here)
and splits the samples in which the bit is set into documented and
undocumented ones.  Prints, per bit, the features (matching --rx) most
over-represented in the undocumented samples (count there, in documented
set samples, in clear samples) and --show example tiles with their
matching features.
"""
import argparse, collections, json, os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'python'))
import check as CK
import designdata as DD
import dies as dieslib
import mkdb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tilegrid')
    ap.add_argument('--max', type=int, default=8)
    ap.add_argument('--rx', default='.')
    ap.add_argument('--show', type=int, default=5)
    ap.add_argument('--top', type=int, default=25)
    ap.add_argument('dbdir')
    ap.add_argument('die')
    ap.add_argument('type')
    ap.add_argument('bits')
    ap.add_argument('roots', nargs='+')
    a = ap.parse_args()
    die = dieslib.load()[a.die]
    tgp = a.tilegrid or os.path.join(a.dbdir, die.name, 'tilegrid.json')
    col = mkdb.Collector(die, json.load(open(tgp)))
    db = CK.Database(a.dbdir)
    rx = re.compile(a.rx)
    want = a.bits.split(',')
    cnt = {b: [collections.Counter() for _ in range(3)] for b in want}
    n = {b: [0, 0, 0] for b in want}  # undoc, doc, clear
    ex = collections.defaultdict(list)
    for d in DD.design_dirs(a.roots)[:a.max]:
        ids = DD.load_bits(col.df, d)
        rb = col.region_bits(ids)
        feats = DD.load_features(d, col.sk)
        for tile, rl in col.tile_regions.items():
            i = rl[0]
            if col.regions[i][1] != a.type:
                continue
            bits = rb.get(i, [])
            fs = feats.get(tile, set())
            _, doc = db.decode(a.type, 0, bits)
            bs = set(bits)
            ctx = sorted(f for f in fs if rx.search(f))
            for b in want:
                c = 2 if b not in bs else (1 if b in doc else 0)
                n[b][c] += 1
                cnt[b][c].update(ctx)
                if c == 0 and len(ex[b]) < a.show:
                    ex[b].append((os.path.basename(d), tile, ctx))
    for b in want:
        u, dc, cl = n[b]
        print(f'== {a.type} {b}: undocumented {u}, documented {dc}, '
              f'clear {cl}')
        rows = []
        for f, k in cnt[b][0].items():
            rows.append((k / max(1, u) - cnt[b][1][f] / max(1, dc)
                         - cnt[b][2][f] / max(1, cl), f))
        for _, f in sorted(rows, reverse=True)[:a.top]:
            print(f'  {cnt[b][0][f]:6d} {cnt[b][1][f]:6d} {cnt[b][2][f]:7d}'
                  f'  {f}')
        for dn, t, ctx in ex[b]:
            print(f'  e.g. {dn} {t}: {" ".join(ctx[:40])}')


if __name__ == '__main__':
    main()
