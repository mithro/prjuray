#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Correlate tile features with tile bits and write the bit database.

For every tile instance in every fuzzing design a sample (features, bits) is
formed, bits being relative to the tile's frame region(s) and named
"<frame>_<bit>" (frame = minor index in the frame column, bit = bit offset from
the tile's first bit in the frame).  Per tile type:

* A bit set in every sample of an unused tile is a *default* bit; the
  features that clear it (feature => bit clear) are found by greedy cover of
  the samples in which it is clear.
* Any other bit is explained as the OR of the smallest set of features found
  by greedy cover of the samples in which the bit is set, using only features
  that imply the bit (feature present => bit set, in every sample).

Output (per architecture directory):
  segbits_<tile type>.db   "<TT>.<feature> <bit> <bit> ..."   (bits it sets;
                           "!<bit>" = bit it clears from the default)
  defaults_<tile type>.db  "<bit>"  bits set in (almost) every unused tile
  unexplained_<tile type>.txt  bits that could not be explained
"""
import argparse
import collections
import json
import os
import pickle
import zlib

import numpy as np

import bitstream
import designdata as DD
import dies as dieslib
import features as featlib


def tile_regions(tg):
    """[(tile, type, region index, first frame index, nframes, offset, n)]"""
    return tg


class Collector:
    """Collects per tile type samples from designs of a die."""

    def __init__(self, die, tg):
        self.die = die
        self.df = DD.DieFrames(die)
        self.sk = featlib.SiteKeys(die.tiles_tsv)
        self.nbf = self.df.wpf * 32
        findex = self.df.index
        self.regions = []  # (tile, type, first fi, nfr, off, n)
        for name, t in tg.items():
            for r in t['bits']:
                fi = findex[int(r['base'], 16)]
                self.regions.append((name, t['type'], fi, r['frames'],
                                     r['offset'], r['nbits']))
        # Owner lookup: frame index -> list of (off, n, region idx)
        self.by_frame = collections.defaultdict(list)
        for i, (_, _, fi, nfr, off, n) in enumerate(self.regions):
            for f in range(fi, fi + nfr):
                self.by_frame[f].append((off, n, i))
        self.tile_regions = collections.defaultdict(list)
        for i, r in enumerate(self.regions):
            self.tile_regions[r[0]].append(i)

    def region_bits(self, ids):
        """Global bit ids -> {region idx: [relative bit names]}"""
        out = collections.defaultdict(list)
        fis = ids // self.nbf
        offs = ids % self.nbf
        for f, o in zip(fis.tolist(), offs.tolist()):
            for off, n, i in self.by_frame.get(f, ()):
                if off <= o < off + n:
                    rfi = self.regions[i][2]
                    out[i].append(f'{f - rfi:02d}_{o - off:03d}')
        return out

    def unowned(self, ids):
        """Set bits in no tile's region.  Frame rows without any tile region
        (e.g. fabric the device does not expose, configured but without
        tiles) are not a tile grid gap: their bits that are also set in the
        empty design (baseline) are counted in self.hidden instead (constant
        baseline bits), the others stay unowned."""
        if not hasattr(self, "_hidden_frame"):
            key = [bitstream.far_fields(self.die.arch, f)[:3]
                   for f in self.df.frames]
            used = {key[f] for f in self.by_frame}
            self._hidden_frame = [k not in used for k in key]
            self._base = set(self.df.base.tolist())
        fis = ids // self.nbf
        offs = ids % self.nbf
        n = 0
        self.hidden = 0
        for i, f, o in zip(ids.tolist(), fis.tolist(), offs.tolist()):
            if not any(off <= o < off + k
                       for off, k, _ in self.by_frame.get(f, ())):
                if self._hidden_frame[f] and i in self._base:
                    self.hidden += 1
                else:
                    n += 1
        return n

    def samples(self, design_dir, empty_keep=0.2, rng=None):
        """Yields (tile type, region idx within tile, features, bits)."""
        ids = DD.load_bits(self.df, design_dir)
        rb = self.region_bits(ids)
        feats = DD.load_features(design_dir, self.sk)
        for tile, rlist in self.tile_regions.items():
            fs = feats.get(tile, set())
            for k, i in enumerate(rlist):
                bits = rb.get(i, [])
                if not fs and rng is not None and rng.random() > empty_keep:
                    continue
                yield self.regions[i][1], k, fs, bits


def correlate(samples):
    """samples: list of (features set, bits list).  Returns dict."""
    S = len(samples)
    # Features in sorted order: the greedy covers break ties by index, and
    # the iteration order of sets of strings changes from run to run.
    fidx = {f: i for i, f in enumerate(sorted(set().union(
        *(fs for fs, _ in samples))))}
    bidx = {}
    for fs, bs in samples:
        for b in bs:
            bidx.setdefault(b, len(bidx))
    nF, nB = len(fidx), len(bidx)
    Fm = np.zeros((nF, S), dtype=bool)
    Bm = np.zeros((nB, S), dtype=bool)
    empty = np.zeros(S, dtype=bool)
    for s, (fs, bs) in enumerate(samples):
        for f in fs:
            Fm[fidx[f], s] = True
        for b in bs:
            Bm[bidx[b], s] = True
        empty[s] = not fs
    def pack64(m):
        pad = (-m.shape[1]) % 64
        if pad:
            m = np.concatenate([m, np.zeros((m.shape[0], pad), dtype=bool)],
                               axis=1)
        return np.packbits(m, axis=1, bitorder='little').view(np.uint64)

    PF = pack64(Fm)
    PB = pack64(Bm)
    nf = Fm.sum(axis=1)
    nb = Bm.sum(axis=1)
    nempty = int(empty.sum())
    fnames = [None] * nF
    for f, i in fidx.items():
        fnames[i] = f
    bnames = [None] * nB
    for b, i in bidx.items():
        bnames[i] = b
    full = pack64(np.ones((1, S), dtype=bool))[0]

    def popcount(a):
        return int(np.bitwise_count(a).sum())

    def cover(target, cand):
        """Greedy cover of target (packed) with candidate feature rows."""
        remaining = target.copy()
        chosen = []
        while popcount(remaining) and len(cand) and len(chosen) < 256:
            gains = np.bitwise_count(PF[cand] & remaining).sum(axis=1)
            j = int(np.argmax(gains))
            # A feature seen once cannot be told apart from coincidence.
            if gains[j] < 2:
                break
            chosen.append(int(cand[j]))
            remaining &= ~PF[cand[j]]
        return chosen, popcount(remaining)

    def pair_cover(target, is_default, pb, K=40):
        """Greedy cover of target with single features and pairwise
        conjunctions of the K features most often present in target."""
        cnt = np.bitwise_count(PF & target).sum(axis=1)
        top = [int(i) for i in np.argsort(-cnt)[:K] if cnt[i] > 0]
        if not top:
            return [], popcount(target)
        T = PF[top]  # K x W
        iu, ju = np.triu_indices(len(top))  # includes i == j (singles)
        G = T[iu] & T[ju]  # pairs x W
        bad = (G & pb) if is_default else (G & ~pb)
        ok = ~np.any(bad, axis=1) & (np.bitwise_count(G).sum(axis=1) >= 2)
        G = G[ok]
        pi, pj = iu[ok], ju[ok]
        remaining = target.copy()
        chosen = []
        while len(G) and popcount(remaining):
            gains = np.bitwise_count(G & remaining).sum(axis=1)
            k = int(np.argmax(gains))
            if gains[k] < 2:
                break
            i, j = top[pi[k]], top[pj[k]]
            chosen.append(fnames[i] if i == j else f'{fnames[i]}&{fnames[j]}')
            remaining &= ~G[k]
            if len(chosen) > 64:
                break
        return chosen, popcount(remaining)

    # Quick prefilter on a few sample words before the full implication
    # test (matters for tile types with tens of thousands of features).
    W = PF.shape[1]
    qidx = np.random.RandomState(0).choice(W, min(W, 8), replace=False)
    PFq = PF[:, qidx]

    # A feature seen n times implies a bit that is set in a fraction p of
    # all samples by chance with probability p**n: only accept implications
    # less likely than this to be coincidences (rare features otherwise
    # pick up frequently set bits as noise).
    CHANCE = 1e-3
    lognf = nf.astype(np.float64)

    def implying(pb, clear, nmax=None):
        """Features f with f => bit set (clear=False) or f => bit clear."""
        ok = nf > 0
        if nmax is not None:
            ok &= nf <= nmax
        p = popcount(pb) / S
        if clear:
            p = 1.0 - p
        if 0.0 < p < 1.0:
            ok &= lognf * np.log(p) < np.log(CHANCE)
        q = pb[qidx]
        viol = (PFq & q) if clear else (PFq & ~q)
        ok &= ~np.any(viol, axis=1)
        idx = np.nonzero(ok)[0]
        if len(idx) == 0:
            return idx
        full_viol = (PF[idx] & pb) if clear else (PF[idx] & ~pb)
        return idx[~np.any(full_viol, axis=1)]

    pair_budget = [3000]
    feat_bits = collections.defaultdict(list)
    defaults = {}
    unexplained = {}
    empty_rows = Bm[:, empty]
    # Fast path: features whose sample pattern equals the bit pattern.
    exact = collections.defaultdict(list)
    for f in range(nF):
        if nf[f] >= 2:
            exact[PF[f].tobytes()].append(f)
    for b in range(nB):
        pb = PB[b]
        # Default: set in (almost) every unused instance; the few exceptions
        # are tiles used in ways the feature dump does not see.
        is_default = nempty > 0 and empty_rows[b].sum() >= 0.97 * nempty
        key = (full & ~pb).tobytes() if is_default else pb.tobytes()
        if key in exact:
            if is_default:
                defaults[bnames[b]] = int(nb[b])
            for f in exact[key]:
                feat_bits[fnames[f]].append(('!' if is_default else '') +
                                            bnames[b])
            continue
        if is_default:
            target = full & ~pb
            # features implying the bit is clear
            cand = implying(pb, clear=True)
            chosen, left = cover(target, cand)
            defaults[bnames[b]] = int(nb[b])
            for f in chosen:
                feat_bits[fnames[f]].append('!' + bnames[b])
        else:
            cand = implying(pb, clear=False, nmax=nb[b])
            chosen, left = cover(pb, cand)
            for f in chosen:
                feat_bits[fnames[f]].append(bnames[b])
        if left and nb[b] >= 10 and pair_budget[0] > 0:
            # Try conjunctions of two features for bits no single feature
            # explains (e.g. a bit that is the XOR of two settings).
            pair_budget[0] -= 1
            target = (full & ~pb) if is_default else pb
            chosen, left = pair_cover(target, is_default, pb)
            for name in chosen:
                feat_bits[name].append(('!' if is_default else '') +
                                       bnames[b])
        if left:
            unexplained[bnames[b]] = (left, int(nb[b]), is_default)
    return dict(samples=S,
                empty=nempty,
                feat_bits=feat_bits,
                defaults=defaults,
                unexplained=unexplained,
                nfeat=nF,
                counts={fnames[i]: int(nf[i]) for i in range(nF)})


def write_db(outdir, ttype, k, res):
    suffix = ttype.lower() + (f'.{k}' if k else '')
    with open(os.path.join(outdir, f'segbits_{suffix}.db'), 'w') as f:
        for feat in sorted(res['feat_bits']):
            # A bit can be assigned twice (single cover, then pair cover).
            bits = sorted(set(res['feat_bits'][feat]),
                          key=lambda b: b.lstrip('!'))
            f.write(f'{ttype}.{feat} {" ".join(bits)}\n')
    with open(os.path.join(outdir, f'defaults_{suffix}.db'), 'w') as f:
        for b in sorted(res['defaults']):
            f.write(f'{b}\n')
    with open(os.path.join(outdir, f'counts_{suffix}.txt'), 'w') as f:
        for feat, n in sorted(res['counts'].items()):
            f.write(f'{ttype}.{feat} {n}\n')
    with open(os.path.join(outdir, f'unexplained_{suffix}.txt'), 'w') as f:
        for b, (left, n, d) in sorted(res['unexplained'].items()):
            f.write(f'{b} unexplained {left} of {n} default {d}\n')


_COLLECTORS = {}


def design_seed(d):
    """Seed of the per design sample thinning: stable across runs and build
    directory locations (hash() of a string changes every run)."""
    rel = '/'.join(os.path.normpath(d).split(os.sep)[-3:])  # die/tag/sN
    return zlib.crc32(rel.encode())


def _collect_one(item):
    """Samples of one design [(tile type, region, features, bits)]."""
    import random
    arch, dn, d = item
    if dn not in _COLLECTORS:
        die = dieslib.load()[dn]
        outdir = os.path.join(dieslib.DB, arch)
        tg = json.load(open(os.path.join(outdir, dn, 'tilegrid.json')))
        _COLLECTORS[dn] = Collector(die, tg)
    rng = random.Random(design_seed(d))
    return list(_COLLECTORS[dn].samples(d, rng=rng))


# Per design sample cache.  One file per design: pickled chunks, one per
# (tile type, region), then the pickled index {key: (offset, length, used,
# empty)} plus stamp, then an 8 byte trailer with the index offset.  A
# chunk is ([(sorted feature tuple, bit list)] of used tiles, same for
# empty tiles).  The stamp (inputs' mtimes and sizes, features.py checksum)
# invalidates it.
# (pickle: the cache is private to the build tree and written only here.)
CACHE_VERSION = 2  # 2: zlib compressed chunks


def _code_stamp():
    """Checksum of the feature extraction code: changing how features are
    derived from the dumps must invalidate cached samples."""
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, 'features.py'), 'rb') as f:
        return zlib.crc32(f.read())


def _cache_stamp(arch, dn, d):
    st = [CACHE_VERSION, _code_stamp()]
    for p in (os.path.join(d, 'bits.npz'),
              os.path.join(d, 'design.features.gz'),
              os.path.join(dieslib.DB, arch, dn, 'tilegrid.json')):
        s = os.stat(p)
        st += [s.st_mtime_ns, s.st_size]
    return st


def _read_index(path):
    with open(path, 'rb') as f:
        f.seek(-8, 2)
        off = int.from_bytes(f.read(8), 'little')
        f.seek(off)
        return pickle.loads(f.read()[:-8])


def _read_chunk(path, off, n):
    with open(path, 'rb') as f:
        f.seek(off)
        return pickle.loads(zlib.decompress(f.read(n)))


def _cache_one(item):
    """Worker: makes sure the sample cache of one design is current.
    Returns [(key, used count, empty count)] in sample order."""
    arch, dn, d, path = item
    stamp = _cache_stamp(arch, dn, d)
    if os.path.exists(path):
        try:
            idx = _read_index(path)
            if idx['stamp'] == stamp:
                return [(k, v[2], v[3]) for k, v in idx['keys']]
        except (OSError, ValueError, EOFError, pickle.UnpicklingError,
                KeyError):
            pass
    chunks = {}
    for tt, k, fs, bits in _collect_one((arch, dn, d)):
        c = chunks.setdefault((tt, k), ([], []))
        c[0 if fs else 1].append((tuple(sorted(fs)), bits))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    keys = []
    with open(tmp, 'wb') as f:
        for key, c in chunks.items():
            data = zlib.compress(pickle.dumps(c, protocol=pickle.HIGHEST_PROTOCOL), 1)
            keys.append((key, (f.tell(), len(data), len(c[0]), len(c[1]))))
            f.write(data)
        off = f.tell()
        f.write(pickle.dumps({'stamp': stamp, 'keys': keys},
                             protocol=pickle.HIGHEST_PROTOCOL))
        f.write(off.to_bytes(8, 'little'))
    os.replace(tmp, path)
    return [(k, v[2], v[3]) for k, v in keys]


def _type_task(task):
    """Worker: gathers the selected samples of one (tile type, region) from
    the design caches, correlates them and writes the database files."""
    outdir, (tt, k), designs, sel_used, sel_empty = task
    pos_u = {g: i for i, g in enumerate(sel_used)}
    pos_e = {g: i for i, g in enumerate(sel_empty)}
    used = [None] * len(sel_used)
    empty = [None] * len(sel_empty)
    gu = ge = 0
    for path, nu, ne in designs:
        if not any(g in pos_u for g in range(gu, gu + nu)) and \
                not any(g in pos_e for g in range(ge, ge + ne)):
            gu += nu
            ge += ne
            continue
        off, n = dict(_read_index(path)['keys'])[(tt, k)][:2]
        cu, ce = _read_chunk(path, off, n)
        for x in cu:
            i = pos_u.get(gu)
            if i is not None:
                used[i] = x
            gu += 1
        for x in ce:
            i = pos_e.get(ge)
            if i is not None:
                empty[i] = x
            ge += 1
    res = correlate(used + empty)
    write_db(outdir, tt, k, res)
    nexp = len(res['unexplained'])
    summary = dict(samples=res['samples'], empty=res['empty'],
                   features=len(res['feat_bits']),
                   defaults=len(res['defaults']), unexplained=nexp)
    line = (f'{tt}.{k}: samples {res["samples"]} empty {res["empty"]} '
            f'feat {res["nfeat"]} with-bits {len(res["feat_bits"])} '
            f'defaults {len(res["defaults"])} unexplained {nexp}')
    return (tt, k), summary, line


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--arch', required=True)
    ap.add_argument('--dies', required=True, help='comma separated')
    ap.add_argument('--tag', default='fabric')
    ap.add_argument('--types', default=None, help='restrict to tile types')
    ap.add_argument('--jobs', type=int, default=32)
    ap.add_argument('--max-samples', type=int, default=50000)
    ap.add_argument('--exclude', action='append', default=[],
                    help='file of design directories to leave out, one per '
                    'line ("SUSPECT <dir>" lines of consistency.py work)')
    ap.add_argument('--cache', default=None,
                    help='per design sample cache (default: '
                    '<db>/<arch>/cache)')
    args = ap.parse_args()
    import random
    import time
    from concurrent.futures import ProcessPoolExecutor
    rng = random.Random(0)
    outdir = os.path.join(dieslib.DB, args.arch)
    cache = args.cache or os.path.join(outdir, 'cache')
    os.makedirs(outdir, exist_ok=True)
    only = set(args.types.split(',')) if args.types else None
    excluded = set()
    for path in args.exclude:
        for line in open(path):
            p = line.split()
            if p:
                excluded.add(os.path.normpath(p[-1]))
    work = []
    for dn in args.dies.split(','):
        for d in DD.design_dirs([
                os.path.join(dieslib.BUILD, 'designs', dn, t)
                for t in args.tag.split(',')
        ], v2only=True):
            if os.path.normpath(d) in excluded:
                print(f'# excluded {d}', flush=True)
                continue
            rel = '/'.join(os.path.normpath(d).split(os.sep)[-3:])
            work.append((args.arch, dn, d,
                         os.path.join(cache, rel + '.smp')))
    # Phase 1: one small task per design (cached across runs).
    t0 = time.time()
    per_key = {}  # key -> [(cache path, used, empty)], first seen order
    with ProcessPoolExecutor(args.jobs) as ex:
        for item, keys in zip(work, ex.map(_cache_one, work, chunksize=2)):
            for key, nu, ne in keys:
                if only and key[0] not in only:
                    continue
                per_key.setdefault(key, []).append((item[3], nu, ne))
    print(f'# samples of {len(work)} designs in {time.time() - t0:.0f} s',
          flush=True)
    # Bound the work per tile type: keep a random subset of the samples
    # (the same subset as sampling the concatenated sample lists).
    tasks = []
    for key, designs in per_key.items():
        nu = sum(x[1] for x in designs)
        ne = sum(x[2] for x in designs)
        su = rng.sample(range(nu), args.max_samples) \
            if nu > args.max_samples else list(range(nu))
        se = rng.sample(range(ne), args.max_samples // 5) \
            if ne > args.max_samples // 5 else list(range(ne))
        tasks.append((outdir, key, designs, su, se))
    # Phase 2: one task per (tile type, region), largest first.
    tasks.sort(key=lambda t: -(len(t[3]) + len(t[4])))
    results = {}
    with ProcessPoolExecutor(min(len(tasks), args.jobs) or 1) as ex:
        for key, summary, line in ex.map(_type_task, tasks):
            results[key] = (summary, line)
    summary = {}
    for (tt, k) in sorted(results):
        s, line = results[(tt, k)]
        summary[f'{tt}.{k}'] = s
        print(line, flush=True)
    json.dump(summary, open(os.path.join(outdir, 'summary.json'), 'w'),
              indent=1)


if __name__ == '__main__':
    main()
