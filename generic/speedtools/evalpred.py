"""evalpred.py [--missed TILETYPE] [--jobs N] [--all] <dbdir> <die> <design
roots...>: prediction accuracy of a database on designs (ideally designs not
used to build it).

Per design, every tile region predicts its bits from the tile's features
(segbits: feature bits, "A&B" features need both, "!" bits clear defaults;
defaults set unless cleared).  The predictions are compared per die bit
address, over the bits owned by any tile region (bits of a frame column
shared by several tiles, e.g. 7-series INT + CLB, count once):

  correct  set, and predicted by some owner
  missed   set, and predicted by no owner
  false    predicted by some owner, not set

Totals, and per owning tile type (a bit counts for every type owning it):
set bits, missed bits, false bits (predicted by that type but not set);
the 30 types with the most missed bits (--all: every type; equal counts in
type name order).  --missed TT lists the most missed bits (relative to TT's
regions).  --jobs N: designs evaluated in parallel (default 1).

Vectorised (numpy over (region, bit) pairs); the counts are those of the
earlier per bit implementation.
"""
import collections, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'python'))
import numpy as np
import dies as dieslib
import designdata as DD
import mkdb
import regionmap as RM

M = np.int64(1) << np.int64(32)


class TypeDB:
    """Features of one (tile type, region index): parts (names joined by
    '&'), set / clear bits as codes, and the default bits."""

    def __init__(self, dbdir, tt, k):
        suffix = tt.lower() + (f'.{k}' if k else '')
        self.by_part = collections.defaultdict(list)  # part -> [feature]
        self.nparts = []
        pos, neg = [], []
        p = os.path.join(dbdir, f'segbits_{suffix}.db')
        if os.path.exists(p):
            # bits straight into numpy (some types have ~10^8 feature bits)
            import check as CK
            with open(p, 'rb') as fh:
                for l in fh:
                    f = l.rstrip(b'\n').split(b' ', 1)
                    if not f[0]:
                        continue
                    parts = f[0].decode().split('.', 1)[1].split('&')
                    i = len(self.nparts)
                    for x in set(parts):
                        self.by_part[x].append(i)
                    self.nparts.append(len(set(parts)))
                    if len(f) > 1 and f[1].strip():
                        pc, nc = CK._parse_segbits_line(f[1])
                    else:
                        pc = nc = np.zeros(0, dtype=np.int64)
                    pos.append(pc)
                    neg.append(nc)
        self.single = {x: v for x, v in self.by_part.items()
                       if all(self.nparts[i] == 1 for i in v)}
        self.npos = np.array([len(x) for x in pos], dtype=np.int64)
        self.nneg = np.array([len(x) for x in neg], dtype=np.int64)
        self.pos_ptr = np.concatenate(([0], np.cumsum(self.npos)))
        self.neg_ptr = np.concatenate(([0], np.cumsum(self.nneg)))
        cat = (lambda x: np.concatenate(x).astype(np.int64) if x else
               np.zeros(0, dtype=np.int64))
        self.pos = cat(pos)
        self.neg = cat(neg)
        d = set()
        p = os.path.join(dbdir, f'defaults_{suffix}.db')
        if os.path.exists(p):
            d = set(l.split()[0] for l in open(p) if l.strip())
        self.defaults = np.array(sorted(RM.name_code(b) for b in d),
                                 dtype=np.int64)

    def matched(self, fs):
        """Indices of the features whose parts are all in fs."""
        out = []
        multi = collections.Counter()
        for f in fs:
            v = self.by_part.get(f)
            if v is None:
                continue
            if f in self.single:
                out += v
            else:
                for i in v:
                    if self.nparts[i] == 1:
                        out.append(i)
                    else:
                        multi[i] += 1
        out += [i for i, n in multi.items() if n == self.nparts[i]]
        return out


def _expand(ptr, counts, sel):
    cnt = counts[sel]
    tot = int(cnt.sum())
    rep = np.repeat(np.arange(len(sel), dtype=np.int64), cnt)
    start = np.repeat(np.cumsum(cnt) - cnt, cnt)
    flat = np.repeat(ptr[sel], cnt) + (np.arange(tot, dtype=np.int64) -
                                       start)
    return rep, flat


def _member(sorted_keys, q):
    if not len(sorted_keys) or not len(q):
        return np.zeros(len(q), dtype=bool)
    i = np.searchsorted(sorted_keys, q)
    i[i == len(sorted_keys)] = 0
    return sorted_keys[i] == q


class Evaluator:
    def __init__(self, dbdir, dn):
        self.die = dieslib.load()[dn]
        tg = json.load(open(os.path.join(dieslib.DB, self.die.arch, dn,
                                         'tilegrid.json')))
        self.col = col = mkdb.Collector(self.die, tg)
        self.rm = col.rmap
        self.N = len(col.df.frames) * col.nbf
        types = sorted({r[1] for r in col.regions})
        self.types = types
        tix = {t: i for i, t in enumerate(types)}
        self.rtype = np.array([tix[r[1]] for r in col.regions],
                              dtype=np.int64)
        self.r_fi = np.array([r[2] for r in col.regions], dtype=np.int64)
        self.r_off = np.array([r[4] for r in col.regions], dtype=np.int64)
        self.dbs = {}
        self.tile_keys = []  # (tile, [(region, TypeDB)])
        for tile, rlist in col.tile_regions.items():
            self.tile_keys.append((tile, [
                (i, self.db(dbdir, col.regions[i][1], k))
                for k, i in enumerate(rlist)]))
        # Default bits of every region (design independent).
        dr, dc = [], []
        for tile, lst in self.tile_keys:
            for i, t in lst:
                if len(t.defaults):
                    dr.append(np.full(len(t.defaults), i, dtype=np.int64))
                    dc.append(t.defaults)
        self.def_keys = np.unique(np.concatenate(dr) * M +
                                  np.concatenate(dc)) if dr else \
            np.zeros(0, np.int64)

    def db(self, dbdir, tt, k):
        if (tt, k) not in self.dbs:
            self.dbs[(tt, k)] = TypeDB(dbdir, tt, k)
        return self.dbs[(tt, k)]

    def gid(self, reg, code):
        f, o = np.divmod(code, RM.CODE_M)
        return (self.r_fi[reg] + f) * self.col.nbf + self.r_off[reg] + o

    def design(self, d, show=None, max_owners=None):
        """Counts of one design: (totals, per type [set, missed, false]
        arrays, Counter of missed bits of type show)."""
        col = self.col
        ids = DD.load_bits(col.df, d)
        feats = DD.load_features(d, col.sk)
        ids = ids[ids < self.N]
        # Predictions: matched features per region.
        pr, pc, nr, nc = [], [], [], []
        for tile, lst in self.tile_keys:
            fs = feats.get(tile)
            if not fs:
                continue
            for i, t in lst:
                m = t.matched(fs)
                if not m:
                    continue
                m = np.array(m, dtype=np.int64)
                _, flat = _expand(t.pos_ptr, t.npos, m)
                pr.append(np.full(len(flat), i, dtype=np.int64))
                pc.append(t.pos[flat])
                _, flat = _expand(t.neg_ptr, t.nneg, m)
                nr.append(np.full(len(flat), i, dtype=np.int64))
                nc.append(t.neg[flat])
        cat = (lambda x: np.concatenate(x) if x else np.zeros(0, np.int64))
        pkeys = np.union1d(self.def_keys, cat(pr) * M + cat(pc))
        ckeys = np.unique(cat(nr) * M + cat(nc))
        pkeys = pkeys[~_member(ckeys, pkeys)]
        preg = pkeys // M
        pg = self.gid(preg, pkeys % M)
        predicted = np.unique(pg)
        # Set bits owned by some region, with their owners.
        pos, oreg, ocode = self.rm.pairs(ids)
        if max_owners:
            # Only the first max_owners owners (region order) of a bit, as
            # the earlier implementation (for equivalence tests).
            o = np.lexsort((oreg, pos))
            pos, oreg, ocode = pos[o], oreg[o], ocode[o]
            first = np.searchsorted(pos, pos, 'left')
            keep = np.arange(len(pos)) - first < max_owners
            pos, oreg, ocode = pos[keep], oreg[keep], ocode[keep]
        act = ids[np.unique(pos)]
        hit = _member(predicted, act)
        tot = np.array([len(act), int(hit.sum()), int((~hit).sum()),
                        len(predicted) - int(hit.sum())], dtype=np.int64)
        nt = len(self.types)
        og = ids[pos]
        # (bit, owner type) pairs, each once.
        ot = np.unique(og * nt + self.rtype[oreg])
        per = np.zeros((nt, 3), dtype=np.int64)
        per[:, 0] = np.bincount(ot % nt, minlength=nt)
        miss = ~_member(predicted, ot // nt)
        per[:, 1] = np.bincount(ot[miss] % nt, minlength=nt)
        # False: predicted, not set, by the types predicting it.
        fl = ~_member(act, pg)
        ft = np.unique(pg[fl] * nt + self.rtype[preg[fl]])
        per[:, 2] = np.bincount(ft % nt, minlength=nt)
        missed_bits = collections.Counter()
        if show is not None:
            sel = (self.rtype[oreg] == self.types.index(show)) & \
                ~_member(predicted, og)
            missed_bits.update(self.rm.names(ocode[sel]))
        return tot, per, missed_bits


_EV = None


def _one(item):
    d, show, mo = item
    return _EV.design(d, show, mo)


def main():
    args = sys.argv[1:]
    show, jobs, allt, mo = None, 1, False, None
    while args and args[0].startswith('--'):
        if args[0] == '--missed':
            show, args = args[1], args[2:]
        elif args[0] == '--jobs':
            jobs, args = int(args[1]), args[2:]
        elif args[0] == '--max-owners':
            mo, args = int(args[1]), args[2:]
        elif args[0] == '--all':
            allt, args = True, args[1:]
        else:
            sys.exit(__doc__)
    dbdir, dn, roots = args[0], args[1], args[2:]
    global _EV
    _EV = ev = Evaluator(dbdir, dn)
    todo = [(d, show, mo) for d in DD.design_dirs(roots, v2only=True)]
    if jobs > 1 and len(todo) > 1:
        from concurrent.futures import ProcessPoolExecutor
        ex = ProcessPoolExecutor(min(jobs, len(todo)))
        results = ex.map(_one, todo)
    else:
        ex = None
        results = map(_one, todo)
    tot = np.zeros(4, dtype=np.int64)
    per = np.zeros((len(ev.types), 3), dtype=np.int64)
    missed_bits = collections.Counter()
    for t, p, mb in results:
        tot += t
        per += p
        missed_bits.update(mb)
    if ex:
        ex.shutdown()
    print(f'TOTAL set {tot[0]} correct {tot[1]} missed {tot[2]} '
          f'false {tot[3]}')
    rows = sorted((i for i in range(len(ev.types)) if per[i].any()),
                  key=lambda i: (-per[i, 1], ev.types[i]))
    for i in rows if allt else rows[:30]:
        c = per[i]
        print(f'  {ev.types[i]:24s} set {c[0]:8d} missed {c[1]:7d} '
              f'false {c[2]:7d}')
    if show:
        print(f'most missed bits of {show}:')
        for b, n in sorted(missed_bits.items(),
                           key=lambda x: (-x[1], x[0]))[:None if allt else 40]:
            print(f'  {b} {n}')


if __name__ == '__main__':
    main()
