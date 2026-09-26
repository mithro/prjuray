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

import numpy as np

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
        fis = ids // self.nbf
        offs = ids % self.nbf
        n = 0
        for f, o in zip(fis.tolist(), offs.tolist()):
            if not any(off <= o < off + k
                       for off, k, _ in self.by_frame.get(f, ())):
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
    fidx, bidx = {}, {}
    for fs, bs in samples:
        for f in fs:
            fidx.setdefault(f, len(fidx))
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

    def implying(pb, clear, nmax=None):
        """Features f with f => bit set (clear=False) or f => bit clear."""
        ok = nf > 0
        if nmax is not None:
            ok &= nf <= nmax
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
            bits = sorted(res['feat_bits'][feat], key=lambda b: b.lstrip('!'))
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


def _collect_one(item):
    """Worker: samples of one design (tile type, region, features, bits)."""
    import random
    arch, dn, d = item
    if dn not in _COLLECTORS:
        die = dieslib.load()[dn]
        outdir = os.path.join(dieslib.BUILD, 'db', arch)
        tg = json.load(open(os.path.join(outdir, dn, 'tilegrid.json')))
        _COLLECTORS[dn] = Collector(die, tg)
    rng = random.Random(hash(d) & 0xffffffff)
    return list(_COLLECTORS[dn].samples(d, rng=rng))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--arch', required=True)
    ap.add_argument('--dies', required=True, help='comma separated')
    ap.add_argument('--tag', default='fabric')
    ap.add_argument('--types', default=None, help='restrict to tile types')
    ap.add_argument('--jobs', type=int, default=32)
    ap.add_argument('--max-samples', type=int, default=50000)
    args = ap.parse_args()
    import random
    rng = random.Random(0)
    outdir = os.path.join(dieslib.BUILD, 'db', args.arch)
    os.makedirs(outdir, exist_ok=True)
    per_type = collections.defaultdict(list)
    alldies = dieslib.load()
    only = set(args.types.split(',')) if args.types else None
    work = []
    for dn in args.dies.split(','):
        for d in DD.design_dirs([
                os.path.join(dieslib.BUILD, 'designs', dn, t)
                for t in args.tag.split(',')
        ], v2only=True):
            work.append((args.arch, dn, d))
    from concurrent.futures import ProcessPoolExecutor as _PPE
    with _PPE(args.jobs) as ex:
        for res in ex.map(_collect_one, work, chunksize=2):
            for tt, k, fs, bits in res:
                if only and tt not in only:
                    continue
                per_type[(tt, k)].append((fs, bits))
    # Bound the work per tile type: keep a random subset of the samples.
    for key, samples in per_type.items():
        used = [x for x in samples if x[0]]
        empty = [x for x in samples if not x[0]]
        if len(used) > args.max_samples:
            used = rng.sample(used, args.max_samples)
        if len(empty) > args.max_samples // 5:
            empty = rng.sample(empty, args.max_samples // 5)
        per_type[key] = used + empty
    summary = {}
    from concurrent.futures import ProcessPoolExecutor
    keys = sorted(per_type, key=lambda k: -len(per_type[k]))
    with ProcessPoolExecutor(min(len(keys), args.jobs) or 1) as ex:
        results = dict(zip(keys, ex.map(correlate,
                                        [per_type[k] for k in keys])))
    for (tt, k) in sorted(results):
        res = results[(tt, k)]
        write_db(outdir, tt, k, res)
        nexp = len(res['unexplained'])
        summary[f'{tt}.{k}'] = dict(samples=res['samples'],
                                    empty=res['empty'],
                                    features=len(res['feat_bits']),
                                    defaults=len(res['defaults']),
                                    unexplained=nexp)
        print(f'{tt}.{k}: samples {res["samples"]} empty {res["empty"]} '
              f'feat {res["nfeat"]} with-bits {len(res["feat_bits"])} '
              f'defaults {len(res["defaults"])} unexplained {nexp}',
              flush=True)
    json.dump(summary, open(os.path.join(outdir, 'summary.json'), 'w'),
              indent=1)


if __name__ == '__main__':
    main()
