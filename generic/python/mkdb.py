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
import resource
import shutil
import time
import zlib

import numpy as np

import bitstream
import designdata as DD
import dies as dieslib
import features as featlib
import regionmap


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
        self._rmap = None

    @property
    def rmap(self):
        """Vectorised region lookup (regionmap.RegionMap), built lazily."""
        if self._rmap is None:
            self._rmap = regionmap.RegionMap(self.regions, self.nbf)
        return self._rmap

    def region_bits(self, ids):
        """Global bit ids -> {region idx: [relative bit names]}"""
        pos, reg, code = self.rmap.pairs(ids)
        names = self.rmap.names(code)
        out = collections.defaultdict(list)
        if not len(reg):
            return out
        cut = np.flatnonzero(reg[1:] != reg[:-1]) + 1
        starts = [0] + cut.tolist()
        ends = cut.tolist() + [len(reg)]
        for r, a, b in zip(reg[starts].tolist(), starts, ends):
            out[r] = names[a:b]
        return out

    def unowned(self, ids, owned=None):
        """Set bits in no tile's region.  Frame rows without any tile region
        (e.g. fabric the device does not expose, configured but without
        tiles) are not a tile grid gap: their bits that are also set in the
        empty design (baseline) are counted in self.hidden instead (constant
        baseline bits), the others stay unowned.  owned: optional mask of
        the ids in some region (RegionMap.owned)."""
        if not hasattr(self, "_hidden_frame"):
            key = [bitstream.far_fields(self.die.arch, f)[:3]
                   for f in self.df.frames]
            used = {key[f] for f in self.by_frame}
            self._hidden_frame = np.array([k not in used for k in key],
                                          dtype=bool)
            self._base = np.unique(self.df.base)
        ids = np.asarray(ids, dtype=np.int64)
        if owned is None:
            owned = self.rmap.owned(ids)
        free = ids[~owned]
        hid = self._hidden_frame[free // self.nbf] & np.isin(free, self._base)
        self.hidden = int(hid.sum())
        return int(len(free) - self.hidden)

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

    def sample_codes(self, design_dir, empty_keep=0.2, rng=None):
        """samples() with the bits as regionmap codes (numpy array, same
        order) instead of names."""
        ids = DD.load_bits(self.df, design_dir)
        _, reg, code = self.rmap.pairs(ids)
        cut = (np.flatnonzero(reg[1:] != reg[:-1]) + 1).tolist()
        starts = [0] + cut
        sl = dict(zip(reg[starts].tolist() if len(reg) else [],
                      zip(starts, cut + [len(reg)])))
        feats = DD.load_features(design_dir, self.sk)
        for tile, rlist in self.tile_regions.items():
            fs = feats.get(tile, set())
            for k, i in enumerate(rlist):
                if not fs and rng is not None and rng.random() > empty_keep:
                    continue
                a, b = sl.get(i, (0, 0))
                yield self.regions[i][1], k, fs, code[a:b]


    def sample_joined(self, design_dir, empty_keep=0.2, rng=None):
        """sample_codes() with each tile's features as one string (sorted,
        '\\n' separated; '' when unused), see load_features_joined."""
        ids = DD.load_bits(self.df, design_dir)
        _, reg, code = self.rmap.pairs(ids)
        cut = (np.flatnonzero(reg[1:] != reg[:-1]) + 1).tolist()
        starts = [0] + cut
        sl = dict(zip(reg[starts].tolist() if len(reg) else [],
                      zip(starts, cut + [len(reg)])))
        feats = DD.load_features_joined(design_dir, self.sk)
        for tile, rlist in self.tile_regions.items():
            fs = feats.get(tile, '')
            for k, i in enumerate(rlist):
                if not fs and rng is not None and rng.random() > empty_keep:
                    continue
                a, b = sl.get(i, (0, 0))
                yield self.regions[i][1], k, fs, code[a:b]


def _packed(rows, S, n):
    """Bit-packed (n x ceil(S/64) uint64) matrix from rows: a list, per
    sample s, of the row indices set in column s (bit s % 64 of word
    s // 64, as np.packbits(..., bitorder='little') of a dense matrix)."""
    W = (S + 63) // 64
    P = np.zeros((n, W), dtype=np.uint64)
    lens = np.fromiter((len(r) for r in rows), dtype=np.int64, count=S)
    if lens.sum():
        r = np.concatenate([np.asarray(x, dtype=np.int64) for x in rows])
        s = np.repeat(np.arange(S, dtype=np.int64), lens)
        np.bitwise_or.at(P, (r, s // 64),
                         np.left_shift(np.uint64(1),
                                       (s % 64).astype(np.uint64)))
    return P


class _Interner(dict):
    """name -> id, new names numbered in order of first lookup."""

    def __missing__(self, key):
        v = self[key] = len(self)
        return v


class PackedRows:
    """Bit-packed row x sample matrix built one sample at a time, rows
    created on first use (names interned).  Records, per row, the first
    sample (and position in that sample's list) it appeared in."""

    def __init__(self, S):
        self.S = S
        self.W = (S + 63) // 64
        self.P = np.zeros((1024, self.W), dtype=np.uint64)
        self.ids = _Interner()
        self.first = np.full(1024, np.iinfo(np.int64).max, dtype=np.int64)
        self.firstpos = np.zeros(1024, dtype=np.int64)

    def add(self, s, names):
        if not names:
            return
        self.add_ids(s, np.fromiter(map(self.ids.__getitem__, names),
                                    dtype=np.int64, count=len(names)))

    def add_ids(self, s, ids):
        """add() of names already interned (ids, in the sample's order)."""
        if not len(ids):
            return
        n = len(self.ids)
        if n > len(self.P):
            cap = max(n, 2 * len(self.P))
            self.P = np.concatenate(
                [self.P, np.zeros((cap - len(self.P), self.W), np.uint64)])
            self.first = np.concatenate([self.first, np.full(
                cap - len(self.first), np.iinfo(np.int64).max, np.int64)])
            self.firstpos = np.concatenate(
                [self.firstpos, np.zeros(cap - len(self.firstpos), np.int64)])
        # Names within one sample are distinct: plain fancy indexing.
        self.P[ids, s // 64] |= np.uint64(1) << np.uint64(s % 64)
        m = s < self.first[ids]
        self.first[ids[m]] = s
        self.firstpos[ids[m]] = np.nonzero(m)[0]

    def rows(self, order):
        """(matrix, names) with the rows in the given name order."""
        return self.P[[self.ids[n] for n in order]], list(order)

    def first_order(self):
        """Names in order of first appearance over the samples."""
        n = len(self.ids)
        names = list(self.ids)
        o = np.lexsort((self.firstpos[:n], self.first[:n]))
        return [names[i] for i in o]


def feature_order(name):
    """Order of the features in the correlation: the greedy covers break
    ties by index, so among features with identical sample patterns the
    first is chosen.  Site / BEL features (e.g. TYPE.RAMB36E1) come before
    PIPs (wire->wire), which usually just follow them, then by name."""
    return ('->' in name, name)


def correlate(samples):
    """samples: list of (features set, bits list).  Returns dict."""
    # Features in a fixed order (feature_order): the greedy covers break
    # ties by index, and the iteration order of sets of strings changes
    # from run to run.
    fnames = sorted(set().union(*(fs for fs, _ in samples)),
                    key=feature_order)
    fidx = {f: i for i, f in enumerate(fnames)}
    bidx = {}
    for fs, bs in samples:
        for b in bs:
            bidx.setdefault(b, len(bidx))
    return correlate_ids([[fidx[f] for f in fs] for fs, _ in samples],
                         [[bidx[b] for b in bs] for _, bs in samples],
                         fnames, list(bidx))


def correlate_ids(frows, brows, fnames, bnames):
    """correlate() on samples given as feature / bit index lists (frows[s],
    brows[s]) into fnames / bnames.  Works on bit-packed matrices only
    (memory ~ (features + bits) x samples / 8 bytes)."""
    S = len(frows)
    emptyv = np.fromiter((not len(r) for r in frows), dtype=bool, count=S)
    return correlate_packed(_packed(frows, S, len(fnames)),
                            _packed(brows, S, len(bnames)), emptyv, fnames,
                            bnames)


class Correlator:
    """correlate() on bit-packed feature / bit matrices (rows fnames /
    bnames, bit s of the rows = sample s; emptyv[s]: sample s has no
    features), in parts: bit ranges can be processed separately (bits(),
    e.g. in several processes sharing memory-mapped matrices), then the
    pair covers and the result assembly (finish())."""

    PAIR_BUDGET = 3000
    # A feature seen n times implies a bit that is set in a fraction p of
    # all samples by chance with probability p**n: only accept implications
    # less likely than this to be coincidences (rare features otherwise
    # pick up frequently set bits as noise).
    CHANCE = 1e-3

    def __init__(self, PF, PB, emptyv, fnames, bnames):
        self.PF, self.PB = PF, PB
        self.fnames, self.bnames = fnames, bnames
        S = self.S = len(emptyv)
        self.nF, self.nB = len(fnames), len(bnames)
        self.nf = np.bitwise_count(PF).sum(axis=1).astype(np.int64)
        self.nb = np.bitwise_count(PB).sum(axis=1).astype(np.int64)
        self.nempty = int(emptyv.sum())
        self.EM = _packed([[0] if e else [] for e in emptyv], S, 1)[0]
        self.full = _packed([[0]] * S, S, 1)[0]
        # Quick prefilter on a few sample words before the full implication
        # test (matters for tile types with tens of thousands of features).
        W = PF.shape[1]
        self.qidx = np.random.RandomState(0).choice(W, min(W, 8),
                                                    replace=False)
        self.PFq = PF[:, self.qidx]
        self.lognf = self.nf.astype(np.float64)
        # Features whose sample pattern equals a bit pattern: rows by hash
        # (in feature order), compared exactly on lookup.
        self.exact = collections.defaultdict(list)
        for f in np.nonzero(self.nf >= 2)[0].tolist():
            self.exact[hash(PF[f].tobytes())].append(f)

    @staticmethod
    def popcount(a):
        return int(np.bitwise_count(a).sum())

    def exact_features(self, key):
        return [f for f in self.exact.get(hash(key.tobytes()), ())
                if np.array_equal(self.PF[f], key)]

    def cover(self, target, cand):
        """Greedy cover of target (packed) with candidate feature rows."""
        PF, popcount = self.PF, self.popcount
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

    def count_in(self, target):
        """Samples of target each feature is present in (only the words of
        target with samples, in row blocks: the full PF & target temporary
        is up to hundreds of MiB for block RAM tiles)."""
        PF, nF = self.PF, self.nF
        nzw = np.nonzero(target)[0]
        cnt = np.zeros(nF, dtype=np.uint64)
        if len(nzw) == 0:
            return cnt
        t = target[nzw]
        step = max(1, (1 << 21) // len(nzw))
        for r in range(0, nF, step):
            cnt[r:r + step] = np.bitwise_count(
                PF[r:r + step][:, nzw] & t).sum(axis=1)
        return cnt

    def pair_cover(self, target, is_default, pb, K=40, residual=None):
        """Greedy cover of target with single features and pairwise
        conjunctions of the K features most often present in target.
        With residual (the part of target single features do not explain),
        residual is covered, with the K features most over-represented in
        it (count minus the count expected from their overall frequency:
        ubiquitous features do not crowd out the informative ones)."""
        PF, nF, fnames, popcount = self.PF, self.nF, self.fnames, \
            self.popcount
        if residual is not None:
            cnt = self.count_in(residual).astype(np.int64)
            score = cnt - self.nf * (popcount(residual) / self.S)
            score[cnt < 2] = -np.inf
            top = [int(i) for i in np.argsort(-score, kind='stable')[:K]
                   if cnt[i] >= 2]
            return self._pairs_from(top, residual, is_default, pb)
        # (Signed: with the unsigned counts -cnt sorted the absent features
        # first and top was empty for almost every bit.)
        cnt = self.count_in(target).astype(np.int64)
        top = [int(i) for i in np.argsort(-cnt, kind='stable')[:K]
               if cnt[i] > 0]
        return self._pairs_from(top, target, is_default, pb)

    def _pairs_from(self, top, target, is_default, pb):
        """Greedy cover of target with the candidate features top and
        their pairwise conjunctions (only ones never present without the
        bit set / clear, seen at least twice and unlikely by chance)."""
        PF, fnames, popcount = self.PF, self.fnames, self.popcount
        if not top:
            return [], popcount(target)
        T = PF[top]  # K x W
        iu, ju = np.triu_indices(len(top))  # includes i == j (singles)
        G = T[iu] & T[ju]  # pairs x W
        bad = (G & pb) if is_default else (G & ~pb)
        ng = np.bitwise_count(G).sum(axis=1).astype(np.int64)
        ok = ~np.any(bad, axis=1) & (ng >= 2)
        if PAIR_CHANCE:
            # As for single features: a conjunction seen n times that
            # implies a bit set (clear) in a fraction p of the samples by
            # chance with probability p**n is accepted only below CHANCE.
            p = self.popcount(pb) / self.S
            if is_default:
                p = 1.0 - p
            if 0.0 < p < 1.0:
                ok &= ng * np.log(p) < np.log(self.CHANCE)
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

    def implying(self, pb, clear, nmax=None):
        """Features f with f => bit set (clear=False) or f => bit clear."""
        nf, PF, PFq = self.nf, self.PF, self.PFq
        ok = nf > 0
        if nmax is not None:
            ok &= nf <= nmax
        p = self.popcount(pb) / self.S
        if clear:
            p = 1.0 - p
        if 0.0 < p < 1.0:
            ok &= self.lognf * np.log(p) < np.log(self.CHANCE)
        q = pb[self.qidx]
        viol = (PFq & q) if clear else (PFq & ~q)
        ok &= ~np.any(viol, axis=1)
        idx = np.nonzero(ok)[0]
        if len(idx) == 0:
            return idx
        full_viol = (PF[idx] & pb) if clear else (PF[idx] & ~pb)
        return idx[~np.any(full_viol, axis=1)]

    def bits(self, b0=0, b1=None, step=1):
        """Single feature explanation of bits b0, b0+step, ... < b1:
        [(b, is_default, feature names, left, pair)], pair: a pair cover
        may be tried (bits in order get the pair budget)."""
        PB, nb, full = self.PB, self.nb, self.full
        brange = range(b0, self.nB if b1 is None else b1, step)
        empty_set = np.bitwise_count(PB[b0:b1:step] & self.EM).sum(axis=1)
        out = []
        for j, b in enumerate(brange):
            pb = PB[b]
            # Default: set in (almost) every unused instance; the few
            # exceptions are tiles used in ways the feature dump does not
            # see.
            is_default = self.nempty > 0 and \
                empty_set[j] >= 0.97 * self.nempty
            key = (full & ~pb) if is_default else pb
            ex = self.exact_features(key)
            if ex:
                out.append((b, is_default, [self.fnames[f] for f in ex], 0,
                            False))
                continue
            if is_default:
                # features implying the bit is clear
                cand = self.implying(pb, clear=True)
                chosen, left = self.cover(full & ~pb, cand)
            else:
                cand = self.implying(pb, clear=False, nmax=nb[b])
                chosen, left = self.cover(pb, cand)
            out.append((b, is_default, [self.fnames[f] for f in chosen],
                        left, bool(left and nb[b] >= 10)))
        return out

    def pairs(self, bs, parts=None):
        """Pair covers of bits bs: {b: (names, left)}.  With the bits()
        parts (and PAIR_RESIDUAL), of what the single features chosen for
        the bit leave unexplained."""
        chosen = {}
        if parts is not None and PAIR_RESIDUAL:
            if not hasattr(self, 'fidx'):
                self.fidx = {f: i for i, f in enumerate(self.fnames)}
            want = set(bs)
            for part in parts:
                for r in part:
                    if r[0] in want:
                        chosen[r[0]] = [self.fidx[n] for n in r[2]]
        res = {}
        for b in bs:
            pb = self.PB[b]
            is_default = self.nempty > 0 and self.popcount(
                pb & self.EM) >= 0.97 * self.nempty
            target = (self.full & ~pb) if is_default else pb
            if b in chosen:
                residual = target.copy()
                for f in chosen[b]:
                    residual &= ~self.PF[f]
                res[b] = self.pair_cover(target, is_default, pb,
                                         residual=residual)
            else:
                res[b] = self.pair_cover(target, is_default, pb)
        return res

    def pair_bits(self, parts):
        """The bits that get a pair cover (the first PAIR_BUDGET candidate
        bits in bit order), given the bits() results."""
        cands = sorted(r[0] for part in parts for r in part if r[4])
        return cands[:self.PAIR_BUDGET]

    def finish(self, parts, pairs):
        """The correlate() result from the bits() parts and the pair covers
        ({b: (names, left)}) of the pair_bits()."""
        feat_bits = collections.defaultdict(list)
        defaults = {}
        unexplained = {}
        for part in parts:
            for b, is_default, names, left, _ in part:
                bn = self.bnames[b]
                pre = '!' if is_default else ''
                if is_default:
                    defaults[bn] = int(self.nb[b])
                for n in names:
                    feat_bits[n].append(pre + bn)
                if b in pairs:
                    pnames, left = pairs[b]
                    for n in pnames:
                        feat_bits[n].append(pre + bn)
                if left:
                    unexplained[bn] = (left, int(self.nb[b]), is_default)
        return dict(samples=self.S,
                    empty=self.nempty,
                    feat_bits=feat_bits,
                    defaults=defaults,
                    unexplained=unexplained,
                    nfeat=self.nF,
                    counts={self.fnames[i]: int(self.nf[i])
                            for i in range(self.nF)})


def correlate_packed(PF, PB, emptyv, fnames, bnames):
    """correlate() on bit-packed matrices (see Correlator), in one go."""
    c = Correlator(PF, PB, emptyv, fnames, bnames)
    parts = [c.bits()]
    return c.finish(parts, c.pairs(c.pair_bits(parts), parts))


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


def _collector(arch, dn):
    """The Collector of a die in a worker.  Only the last die's is kept:
    phase 1 workers see the dies one after the other (the work list is in
    die order), and every Collector kept costs 100-200 MB (Series7)."""
    if dn not in _COLLECTORS:
        _COLLECTORS.clear()
        import gc
        gc.collect()
        die = dieslib.load()[dn]
        tg = json.load(open(os.path.join(dieslib.DB, arch, dn,
                                         'tilegrid.json')))
        _COLLECTORS[dn] = Collector(die, tg)
    return _COLLECTORS[dn]


def design_seed(d):
    """Seed of the per design sample thinning: stable across runs and build
    directory locations (hash() of a string changes every run)."""
    rel = '/'.join(os.path.normpath(d).split(os.sep)[-3:])  # die/tag/sN
    return zlib.crc32(rel.encode())


def _collect_one(item):
    """Samples of one design [(tile type, region, features, bits)]."""
    import random
    arch, dn, d = item
    rng = random.Random(design_seed(d))
    return list(_collector(arch, dn).samples(d, rng=rng))


# Per design sample cache.  One file per design: zlib compressed pickled
# chunks, one per (tile type, region), then the pickled index {'stamp',
# 'keys': [(key, (offset, length, used, empty, digest))]}, then an 8 byte
# trailer with the index offset.  A chunk (_encode_chunk) holds the sorted
# feature names and the bit names of its samples once, and per sample the
# uint16 / int32 indices of its features (ascending: sorted names) and bits (in
# the sample's bit order), for the used then the empty tiles.  digest is a
# hash of the compressed chunk (phase 2 reuses the results of tasks whose
# inputs did not change, see _task_digest).  The stamp (inputs' mtimes and
# sizes, features.py checksum) invalidates the file.
# (pickle: the cache is private to the build tree and written only here.)
CACHE_VERSION = 3  # 2: zlib compressed chunks, 3: vocabulary encoded


def _ids_dtype(vocab):
    return np.uint16 if len(vocab) <= 1 << 16 else np.int32


def _encode_chunk(used, empty, names):
    """used: [(feature set, bit codes)], empty: [bit codes] -> bytes.
    names: bit codes -> names (RegionMap.names)."""
    flat = [f for fs, _ in used for f in fs]
    fv = sorted(set(flat))
    fidx = dict(zip(fv, range(len(fv))))
    fid = np.fromiter(map(fidx.__getitem__, flat), dtype=_ids_dtype(fv),
                      count=len(flat))
    flen = np.array([len(fs) for fs, _ in used], dtype=np.int32)
    # ascending within each sample (deterministic bytes: set order varies)
    fid = fid[np.lexsort((fid, np.repeat(np.arange(len(flen)), flen)))]
    codes = [c for _, c in used] + empty
    allc = np.concatenate(codes) if codes else np.zeros(0, np.int64)
    uc = np.unique(allc)
    bid = np.searchsorted(uc, allc).astype(_ids_dtype(uc))
    blen = np.array([len(c) for c in codes], dtype=np.int32)
    data = (fv, names(uc), fid, flen, bid, blen, len(used))
    return zlib.compress(pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL),
                         1)


def _encode_chunk_joined(used, empty, names):
    """_encode_chunk with each sample's features as a sorted '\\n' joined
    string: the same bytes (sorted strings give ascending vocabulary
    indices), without per sample sorting."""
    flat = '\n'.join(s for s, _ in used).split('\n') if used else []
    fv = sorted(set(flat))
    fidx = dict(zip(fv, range(len(fv))))
    fid = np.fromiter(map(fidx.__getitem__, flat), dtype=_ids_dtype(fv),
                      count=len(flat))
    flen = np.array([s.count('\n') + 1 for s, _ in used], dtype=np.int32)
    codes = [c for _, c in used] + empty
    allc = np.concatenate(codes) if codes else np.zeros(0, np.int64)
    uc = np.unique(allc)
    bid = np.searchsorted(uc, allc).astype(_ids_dtype(uc))
    blen = np.array([len(c) for c in codes], dtype=np.int32)
    data = (fv, names(uc), fid, flen, bid, blen, len(used))
    return zlib.compress(pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL),
                         1)


_ENC_ID = []


def _encoding_id():
    """Checksum of everything that turns a chunk's content into bytes: the
    encoder and the bit code layout / names (regionmap), CACHE_VERSION."""
    if not _ENC_ID:
        here = os.path.dirname(os.path.abspath(__file__))
        _ENC_ID.append(repr((
            CACHE_VERSION,
            DD.code_digest(os.path.join(here, 'mkdb.py'),
                           ['_encode_chunk_joined', '_ids_dtype']),
            DD.code_digest(os.path.join(here, 'regionmap.py'),
                           ['code_name', 'name_code', 'CODE_M',
                            'RegionMap']))).encode())
    return _ENC_ID[0]


def _content_digest(used, empty):
    """Digest of a chunk's content: the samples' joined features and bit
    codes, in order, and _encoding_id(): equal digests give equal bytes."""
    import hashlib
    h = hashlib.blake2b(_encoding_id(), digest_size=16)
    for s, c in used:
        h.update(s.encode())
        h.update(b'\0')
        h.update(np.ascontiguousarray(c, dtype=np.int64).tobytes())
        h.update(b'\1')
    h.update(b'\2')
    for c in empty:
        h.update(np.ascontiguousarray(c, dtype=np.int64).tobytes())
        h.update(b'\1')
    return h.hexdigest()


def _read_raw(path, off, n):
    with open(path, 'rb') as f:
        f.seek(off)
        return pickle.loads(zlib.decompress(f.read(n)))


def _chunk_samples(raw):
    """Decoded chunk: per sample (kind 'u' / 'e', feature ids, bit ids)
    into raw's vocabularies (fv = raw[0], bit names = raw[1])."""
    fv, bn, fflat, flen, bflat, blen, nu = raw
    fo = np.concatenate(([0], np.cumsum(flen, dtype=np.int64)))
    bo = np.concatenate(([0], np.cumsum(blen, dtype=np.int64)))
    for j in range(len(blen)):
        if j < nu:
            yield 'u', fflat[fo[j]:fo[j + 1]], bflat[bo[j]:bo[j + 1]]
        else:
            yield 'e', fflat[:0], bflat[bo[j]:bo[j + 1]]


_TILES_TSV = {}


def _cache_stamp(arch, dn, d):
    # feature code, its data files and switches, the die's site files
    # (designdata.feature_inputs_stamp): anything changing the features
    if dn not in _TILES_TSV:
        _TILES_TSV[dn] = dieslib.load()[dn].tiles_tsv
    st = [CACHE_VERSION, DD.feature_inputs_stamp(_TILES_TSV[dn])]
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
    """A chunk as ([(sorted feature tuple, bit list)] of the used tiles,
    the same of the empty tiles)."""
    raw = _read_raw(path, off, n)
    fv, bn = raw[0], raw[1]
    out = ([], [])
    for kind, f, b in _chunk_samples(raw):
        out[kind == 'e'].append((tuple(fv[i] for i in f.tolist()),
                                 [bn[i] for i in b.tolist()]))
    return out


def _cache_one(item):
    """Worker: makes sure the sample cache of one design is current.
    Returns [(key, used count, empty count, chunk digest)] in sample
    order."""
    import hashlib
    import random
    arch, dn, d, path = item
    stamp = _cache_stamp(arch, dn, d)
    old = {}  # stale file: chunks whose content did not change are reused
    if os.path.exists(path):
        try:
            idx = _read_index(path)
            if idx['stamp'] == stamp:
                return [(k, v[2], v[3], v[4]) for k, v in idx['keys']]
            old = {k: v for k, v in idx['keys'] if len(v) > 5}
        except (OSError, ValueError, EOFError, pickle.UnpicklingError,
                KeyError, IndexError):
            pass
    _MISS[0] = True
    col = _collector(arch, dn)
    chunks = {}
    for tt, k, fs, codes in col.sample_joined(
            d, rng=random.Random(design_seed(d))):
        c = chunks.setdefault((tt, k), ([], []))
        if fs:
            c[0].append((fs, codes))
        else:
            c[1].append(codes)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.tmp'
    keys = []
    # (URAY_, not MKDB_: task digests include the MKDB_ settings)
    reuse = os.environ.get('URAY_CHUNK_REUSE', '1') != '0'
    with open(tmp, 'wb') as f:
        for key, c in chunks.items():
            cd = _content_digest(*c)
            o = old.get(key)
            data = None
            if reuse and o is not None and o[5] == cd:
                with open(path, 'rb') as of:
                    of.seek(o[0])
                    data = of.read(o[1])
                if len(data) != o[1]:
                    data = None
            if data is None:
                data = _encode_chunk_joined(*c, col.rmap.names)
            keys.append((key, (f.tell(), len(data), len(c[0]), len(c[1]),
                               hashlib.blake2b(data,
                                               digest_size=16).hexdigest(),
                               cd)))
            f.write(data)
        off = f.tell()
        f.write(pickle.dumps({'stamp': stamp, 'keys': keys},
                             protocol=pickle.HIGHEST_PROTOCOL))
        f.write(off.to_bytes(8, 'little'))
    os.replace(tmp, path)
    return [(k, v[2], v[3], v[4]) for k, v in keys]


_MISS = [False]


def _cache_item(item):
    """_cache_one and whether the cache had to be (re)built."""
    _MISS[0] = False
    keys = _cache_one(item)
    return keys, _MISS[0]


def _sample_phase(work, jobs, budget_gib, cache):
    """Runs _cache_one on every work item; yields (index, keys) as they
    complete, at most as many at once as fit the memory budget
    (memsched.budget_map).  The work list is in die order and a worker
    keeps only its current die's Collector.  The measured worker peaks per
    die are kept in <cache>/worker_peaks.json: the next run starts from
    them (Series7 ~1.3 GiB) instead of the 2 GiB floor."""
    import memsched
    path = os.path.join(cache, 'worker_peaks.json')
    try:
        with open(path) as f:
            known = json.load(f)
    except (OSError, ValueError):
        known = {}
    peaks = {}
    for i, (keys, _) in memsched.budget_map(
            _cache_item, work, jobs, budget_gib, label='sample phase',
            log=lambda m: print(m, flush=True), key=lambda it: it[1],
            known=known, peaks=peaks, record=lambda r: r[1]):
        yield i, keys
    if peaks:
        for k, v in peaks.items():  # never below an earlier measurement
            known[k] = max(known.get(k, 0), v)
        os.makedirs(cache, exist_ok=True)
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'w') as f:
            json.dump(known, f, indent=1, sort_keys=True)
        os.replace(tmp, path)


def _interned(rows, vmap, vocab, vids):
    """PackedRows ids of the chunk vocabulary entries vids (vmap caches
    them, -1 = not yet interned)."""
    ids = vmap[vids]
    if (ids < 0).any():
        for v in vids[ids < 0].tolist():
            vmap[v] = rows.ids[vocab[v]]
        ids = vmap[vids]
    return ids


def _type_task(task):
    """Worker: gathers the selected samples of one (tile type, region) from
    the design caches, correlates them and writes the database files."""
    outdir, (tt, k), designs, sel_used, sel_empty, split_dir = task
    t0 = time.time()
    nu_sel = len(sel_used)
    pos = {('u', g): i for i, g in enumerate(sel_used)}
    pos.update({('e', g): nu_sel + i for i, g in enumerate(sel_empty)})
    # The samples go straight into bit-packed matrices (holding them as
    # lists of strings took tens of GiB for block RAM tiles with thousands
    # of INIT features per sample).
    S = nu_sel + len(sel_empty)
    FR, BR = PackedRows(S), PackedRows(S)
    emptyv = np.zeros(S, dtype=bool)
    gu = ge = 0
    for path, nu, ne, _ in designs:
        if not any(('u', g) in pos for g in range(gu, gu + nu)) and \
                not any(('e', g) in pos for g in range(ge, ge + ne)):
            gu += nu
            ge += ne
            continue
        off, n = dict(_read_index(path)['keys'])[(tt, k)][:2]
        raw = _read_raw(path, off, n)
        # chunk vocabulary index -> PackedRows id, interned on first use
        # by a selected sample (the same names in the same order as
        # adding the names)
        fmap = np.full(len(raw[0]), -1, dtype=np.int64)
        bmap = np.full(len(raw[1]), -1, dtype=np.int64)
        for kind, f, b in _chunk_samples(raw):
            g = gu if kind == 'u' else ge
            i = pos.get((kind, g))
            if i is not None:
                FR.add_ids(i, _interned(FR, fmap, raw[0], f))
                BR.add_ids(i, _interned(BR, bmap, raw[1], b))
                emptyv[i] = not len(f)
            if kind == 'u':
                gu += 1
            else:
                ge += 1
        del raw
    # Same indexing as correlate(): features in feature_order, bits in order of first
    # appearance over the samples.
    del pos
    PF, fnames = FR.rows(sorted(FR.ids, key=feature_order))
    del FR
    PB, bnames = BR.rows(BR.first_order())
    del BR
    nparts = split_parts(len(fnames), len(bnames), len(emptyv))
    if nparts > 1:
        # Large task: the matrices go to disk and the bit ranges are
        # correlated by separate tasks (_bits_task) sharing them through
        # memory maps, then _finish_task does the pair covers and writes.
        sdir = os.path.join(split_dir, f'{tt}.{k}')
        os.makedirs(sdir, exist_ok=True)
        np.save(os.path.join(sdir, 'PF.npy'), PF)
        np.save(os.path.join(sdir, 'PB.npy'), PB)
        np.save(os.path.join(sdir, 'empty.npy'), emptyv)
        with open(os.path.join(sdir, 'names.pkl'), 'wb') as f:
            pickle.dump((fnames, bnames), f,
                        protocol=pickle.HIGHEST_PROTOCOL)
        # Interleaved bit subsets (bits i, i+n, i+2n, ...): the expensive
        # bits cluster (contiguous ranges differed 50x in run time).
        nB = len(bnames)
        nparts = min(nparts, nB)
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        return (tt, k), ('split', sdir, [(i, nB, nparts)
                                         for i in range(nparts)]), \
            None, peak, time.time() - t0
    res = correlate_packed(PF, PB, emptyv, fnames, bnames)
    return _written(outdir, tt, k, res, t0)


def split_parts(nF, nB, S):
    """Number of bit ranges to correlate a (tile type, region) in: about
    one per SPLIT_COST (3e10) feature x bit x sample-word units; the pair
    covers of a split task run in chunks of PAIR_CHUNK bits.  (Series7
    rebuild, 13 dies: ~2.5e-9 s per unit, block RAM bit ranges took
    ~500 s with 2e11 and the unsplit pair phase over 20 min.)"""
    cost = nF * nB * ((S + 63) // 64)
    return int(min(64, max(1, cost // SPLIT_COST)))


SPLIT_COST = int(float(os.environ.get('MKDB_SPLIT_COST', 3e10)))
PAIR_CHANCE = os.environ.get("MKDB_PAIR_CHANCE", "1") == "1"
PAIR_RESIDUAL = os.environ.get("MKDB_PAIR_RESIDUAL", "1") == "1"


def _load_split(sdir):
    """Correlator of a split task (matrices memory mapped, shared)."""
    PF = np.load(os.path.join(sdir, 'PF.npy'), mmap_mode='r')
    PB = np.load(os.path.join(sdir, 'PB.npy'), mmap_mode='r')
    emptyv = np.load(os.path.join(sdir, 'empty.npy'))
    with open(os.path.join(sdir, 'names.pkl'), 'rb') as f:
        fnames, bnames = pickle.load(f)
    return Correlator(PF, PB, emptyv, fnames, bnames)


def _bits_task(item):
    """Worker: one bit range of a split task."""
    sdir, b0, b1, step = item
    t0 = time.time()
    part = _load_split(sdir).bits(b0, b1, step)
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    return part, peak, time.time() - t0


def pair_bits_of(parts, budget=None):
    """The bits that get a pair cover (the first PAIR_BUDGET candidate bits
    in bit order), given the Correlator.bits() results."""
    cands = sorted(r[0] for part in parts for r in part if r[4])
    return cands[:Correlator.PAIR_BUDGET if budget is None else budget]


PAIR_CHUNK = int(os.environ.get('MKDB_PAIR_CHUNK', 50))


def _pairs_task(item):
    """Worker: pair covers of some bits of a split task."""
    sdir, bs, part = item
    t0 = time.time()
    res = _load_split(sdir).pairs(bs, [part])
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    return res, peak, time.time() - t0


def _finish_task(item):
    """Worker: database files of a split task from its bits() parts and
    pair covers."""
    outdir, (tt, k), sdir, parts, pairs = item
    t0 = time.time()
    c = _load_split(sdir)
    res = c.finish(parts, pairs)
    del c
    shutil.rmtree(sdir, ignore_errors=True)
    return _written(outdir, tt, k, res, t0)


def _written(outdir, tt, k, res, t0):
    """Writes the database files of a result; the task's report."""
    write_db(outdir, tt, k, res)
    nexp = len(res['unexplained'])
    summary = dict(samples=res['samples'], empty=res['empty'],
                   features=len(res['feat_bits']),
                   defaults=len(res['defaults']), unexplained=nexp)
    line = (f'{tt}.{k}: samples {res["samples"]} empty {res["empty"]} '
            f'feat {res["nfeat"]} with-bits {len(res["feat_bits"])} '
            f'defaults {len(res["defaults"])} unexplained {nexp}')
    # Peak memory of this worker process (one task per process).
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    return (tt, k), summary, line, peak, time.time() - t0


# Phase 2 task reuse: <outdir>/.mkdb_tasks/<type>[.k].json records the
# digest of a task's inputs with the hashes of the database files it wrote.
TASK_RECORD_VERSION = 1


_CODE_DIGEST = []


def _code_digest():
    """mkdb.py itself and the MKDB_* settings: any correlation change
    invalidates every task."""
    if _CODE_DIGEST:
        return _CODE_DIGEST[0]
    import hashlib
    h = hashlib.blake2b(digest_size=16)
    with open(os.path.abspath(__file__), 'rb') as f:
        h.update(f.read())
    for k in sorted(os.environ):
        if k.startswith('MKDB_'):
            h.update(f'{k}={os.environ[k]}'.encode())
    _CODE_DIGEST.append(h.hexdigest())
    return _CODE_DIGEST[0]


def _task_digest(task, max_samples):
    import hashlib
    _, key, designs, su, se, _ = task
    h = hashlib.blake2b(digest_size=16)
    h.update(repr((TASK_RECORD_VERSION, _code_digest(), key,
                   max_samples)).encode())
    for _, nu, ne, dg in designs:
        h.update(f'{nu} {ne} {dg};'.encode())
    h.update(np.asarray(su, dtype=np.int64).tobytes())
    h.update(b'|')
    h.update(np.asarray(se, dtype=np.int64).tobytes())
    return h.hexdigest()


def _task_files(outdir, key):
    tt, k = key
    suffix = tt.lower() + (f'.{k}' if k else '')
    return [os.path.join(outdir, f'{p}_{suffix}.{e}') for p, e in (
        ('segbits', 'db'), ('defaults', 'db'), ('counts', 'txt'),
        ('unexplained', 'txt'))]


def _file_hash(path):
    import hashlib
    with open(path, 'rb') as f:
        return hashlib.blake2b(f.read(), digest_size=16).hexdigest()


def _task_record_path(outdir, key):
    tt, k = key
    return os.path.join(outdir, '.mkdb_tasks', f'{tt}.{k}.json')


def _task_record(outdir, key, digest):
    """The record of the task's last run if its digest is digest and its
    database files are unchanged, else None."""
    try:
        with open(_task_record_path(outdir, key)) as f:
            rec = json.load(f)
        if rec['digest'] != digest:
            return None
        for p, h in zip(_task_files(outdir, key), rec['files']):
            if _file_hash(p) != h:
                return None
        return rec
    except (OSError, ValueError, KeyError):
        return None


def _save_task_record(outdir, key, digest, summary, line):
    path = _task_record_path(outdir, key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rec = dict(digest=digest, summary=summary, line=line,
               files=[_file_hash(p) for p in _task_files(outdir, key)])
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w') as f:
        json.dump(rec, f)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--arch', required=True)
    ap.add_argument('--dies', required=True, help='comma separated')
    ap.add_argument('--tag', default='fabric')
    ap.add_argument('--types', default=None, help='restrict to tile types')
    ap.add_argument('--jobs', type=int, default=32)
    ap.add_argument('--sample-jobs', type=int, default=None,
                    help='processes of the per design sample phase (light: '
                    'can exceed --jobs; default --jobs)')
    ap.add_argument('--sample-mem-budget', type=float, default=None,
                    help='GiB for all sample phase workers: the number of '
                    'designs processed at once (at most --sample-jobs) is '
                    'the budget over the largest worker peak measured so '
                    'far and a 2 GiB floor (x 1.25; measured Series7 up '
                    'to ~1.3 GiB per worker)')
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
    from concurrent.futures import ProcessPoolExecutor, as_completed
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
    results = [None] * len(work)
    for n, (i, keys) in enumerate(_sample_phase(
            work, args.sample_jobs or args.jobs, args.sample_mem_budget,
            cache), 1):
        results[i] = keys
        if n % 50 == 0 or n == len(work):
            el = time.time() - t0
            print(f'# samples {n}/{len(work)} designs, {el:.0f} s, eta '
                  f'{el / n * (len(work) - n):.0f} s', flush=True)
    for item, keys in zip(work, results):
        for key, nu, ne, dg in keys:
            if only and key[0] not in only:
                continue
            per_key.setdefault(key, []).append((item[3], nu, ne, dg))
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
        tasks.append((outdir, key, designs, su, se,
                      os.path.join(cache, 'split')))
    # Phase 2: one task per (tile type, region), largest first.  A task
    # whose inputs are those of the task that wrote its current database
    # files (same digest, files unchanged) is not run again.
    tasks.sort(key=lambda t: -(len(t[3]) + len(t[4])))
    results = {}
    digests = {}
    todo = []
    for t in tasks:
        dg = digests[t[1]] = _task_digest(t, args.max_samples)
        rec = _task_record(outdir, t[1], dg)
        if rec:
            results[t[1]] = (rec['summary'], rec['line'])
        else:
            todo.append(t)
    if len(todo) < len(tasks):
        print(f'# {len(tasks) - len(todo)} of {len(tasks)} tasks reused '
              '(inputs unchanged)', flush=True)
    tasks = todo
    t0 = time.time()
    # One task per process: a task's peak memory is returned to the system
    # when it ends (and measured).
    # Large (tile type, region) tasks come back split into bit range
    # tasks, whose results then go to a finishing task.
    with ProcessPoolExecutor(min(len(tasks), args.jobs) or 1,
                             max_tasks_per_child=1) as ex:
        futs = {ex.submit(_type_task, t): ('type', t[1]) for t in tasks}
        parts = {}
        ndone = 0
        while futs:
            done = next(as_completed(futs))
            kind, key = futs.pop(done)
            el = time.time() - t0
            if kind == 'type':
                key, summary, line, peak, dt = done.result()
                if isinstance(summary, tuple):
                    _, sdir, ranges = summary
                    parts[key] = [sdir, len(ranges), {}]
                    for i, (b0, b1, step) in enumerate(ranges):
                        futs[ex.submit(_bits_task,
                                       (sdir, b0, b1, step))] = \
                            ('bits', (key, i))
                    print(f'# task {key[0]}.{key[1]} split into '
                          f'{len(ranges)} bit ranges ({dt:.0f} s, peak '
                          f'{peak / 2**30:.2f} GiB); elapsed {el:.0f} s',
                          flush=True)
                    continue
            elif kind == 'bits':
                (key, i) = key
                part, peak, dt = done.result()
                p = parts[key]
                p[2][i] = part
                print(f'# task {key[0]}.{key[1]} bits {len(p[2])}/{p[1]} '
                      f'{dt:.0f} s peak {peak / 2**30:.2f} GiB; elapsed '
                      f'{el:.0f} s', flush=True)
                if len(p[2]) == p[1]:
                    # All bits done: the pair covers, in chunks.
                    allparts = [p[2][j] for j in range(p[1])]
                    pbits = pair_bits_of(allparts)
                    rows = {r[0]: r for part in allparts for r in part}
                    # (interleaved, like the bit subsets)
                    nch = -(-len(pbits) // PAIR_CHUNK)
                    chunks = [pbits[i::nch] for i in range(nch)]
                    p.append(allparts)
                    p.append({'n': len(chunks), 'res': {}})
                    for i, bs in enumerate(chunks):
                        futs[ex.submit(_pairs_task, (
                            p[0], bs, [rows[b] for b in bs]))] = \
                            ('pairs', (key, i))
                    if not chunks:
                        futs[ex.submit(_finish_task, (
                            outdir, key, p[0], allparts, {}))] = \
                            ('finish', key)
                continue
            elif kind == 'pairs':
                (key, i) = key
                res, peak, dt = done.result()
                p = parts[key]
                pr = p[4]
                pr['res'].update(res)
                pr['done'] = pr.get('done', 0) + 1
                print(f'# task {key[0]}.{key[1]} pairs {pr["done"]}/'
                      f'{pr["n"]} {dt:.0f} s peak {peak / 2**30:.2f} GiB; '
                      f'elapsed {el:.0f} s', flush=True)
                if pr['done'] == pr['n']:
                    futs[ex.submit(_finish_task, (
                        outdir, key, p[0], p[3], pr['res']))] = \
                        ('finish', key)
                continue
            else:
                key, summary, line, peak, dt = done.result()
            results[key] = (summary, line)
            _save_task_record(outdir, key, digests[key], summary, line)
            ndone += 1
            print(f'# task {ndone}/{len(tasks)} {key[0]}.{key[1]} samples '
                  f'{summary["samples"]} {dt:.0f} s peak '
                  f'{peak / 2**30:.2f} GiB; elapsed {el:.0f} s', flush=True)
    summary = {}
    for (tt, k) in sorted(results):
        s, line = results[(tt, k)]
        summary[f'{tt}.{k}'] = s
        print(line, flush=True)
    json.dump(summary, open(os.path.join(outdir, 'summary.json'), 'w'),
              indent=1)


if __name__ == '__main__':
    main()
