#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Check a bitstream against the database: every set configuration bit must
be documented.

A set bit is documented when it lies inside a tile's frame region and is
either a default bit of that tile type or one of the bits of a database
feature whose bits all match (set bits set, "!" bits clear).  Bits in
regions shared by several tiles are documented if any owner documents them.

Usage:
  check.py --die <die> (--bit <file.bit> | --designs <dir>)
"""
import argparse
import collections
import json
import os
import sys

import numpy as np

import bitstream
import mkdb
import regionmap as RM
import designdata as DD
import dies as dieslib


class Database:
    def __init__(self, dbdir):
        self.dbdir = dbdir
        self.types = {}
        self.type_codes = {}

    def get(self, ttype, k):
        key = (ttype, k)
        if key in self.types:
            return self.types[key]
        suffix = ttype.lower() + (f'.{k}' if k else '')
        feats = []
        for path in (os.path.join(self.dbdir, f'segbits_{suffix}.db'),
                     os.path.join(self.dbdir, f'segbits_opt_{suffix}.db')):
            if not os.path.exists(path):
                continue
            for line in open(path):
                p = line.split()
                pos = frozenset(b for b in p[1:] if not b.startswith('!'))
                neg = frozenset(b[1:] for b in p[1:] if b.startswith('!'))
                feats.append((p[0], pos, neg))
        defaults = set()
        path = os.path.join(self.dbdir, f'defaults_{suffix}.db')
        if os.path.exists(path):
            defaults = set(l.split()[0] for l in open(path) if l.strip())
        by_bit = collections.defaultdict(list)
        for i, (_, pos, _) in enumerate(feats):
            for b in pos:
                by_bit[b].append(i)
        self.types[key] = (feats, defaults, by_bit)
        return self.types[key]

    def codes(self, ttype, k):
        """TypeCodes of a (tile type, region index), cached; read straight
        from the files (no per bit Python objects: some tile types have
        hundreds of millions of feature bits)."""
        key = (ttype, k)
        if key not in self.type_codes:
            suffix = ttype.lower() + (f'.{k}' if k else '')
            self.type_codes[key] = TypeCodes.from_files(
                [os.path.join(self.dbdir, f'segbits_{suffix}.db'),
                 os.path.join(self.dbdir, f'segbits_opt_{suffix}.db')],
                os.path.join(self.dbdir, f'defaults_{suffix}.db'))
        return self.type_codes[key]

    def decode(self, ttype, k, bits):
        """Returns (matched feature names, set of documented bits)."""
        feats, defaults, by_bit = self.get(ttype, k)
        bits = set(bits)
        doc = bits & defaults
        matched = []
        seen = set()
        for b in bits:
            for i in by_bit.get(b, ()):
                if i in seen:
                    continue
                seen.add(i)
                name, pos, neg = feats[i]
                if pos <= bits and not (neg & bits):
                    matched.append(name)
                    doc |= pos
        return matched, doc


_NEG_MARK = 999999  # '!' in a segbits line (no frame / offset is this big)


def _parse_segbits_line(rest):
    """'FF_BBB !FF_BBB ...' -> (set bit codes, clear bit codes)."""
    v = np.fromstring(rest.replace(b'_', b' ').replace(b'!', b'999999 '),
                      dtype=np.int64, sep=' ')
    m = np.flatnonzero(v == _NEG_MARK)
    if len(m):
        neg = np.zeros(len(v), dtype=bool)
        neg[m + 1] = True
        keep = np.ones(len(v), dtype=bool)
        keep[m] = False
        v, neg = v[keep], neg[keep]
        neg = neg[0::2]
    else:
        neg = np.zeros(len(v) // 2, dtype=bool)
    code = v[0::2] * RM.CODE_M + v[1::2]
    return code[~neg], code[neg]


class TypeCodes:
    """Database of one (tile type, region index) with relative bits as
    integer codes (regionmap.name_code), for vectorised decoding."""

    @classmethod
    def from_files(cls, segbits, defaults_path):
        """From segbits files (in order; missing ones skipped) and a
        defaults file.  Same result as TypeCodes(Database.get(...)[:2])
        for files written by mkdb (bits of a line distinct)."""
        self = cls.__new__(cls)
        self.names = []
        pos, neg = [], []
        for path in segbits:
            if not os.path.exists(path):
                continue
            with open(path, 'rb') as f:
                for line in f:
                    parts = line.rstrip(b'\n').split(b' ', 1)
                    if not parts[0]:
                        continue
                    self.names.append(parts[0].decode())
                    if len(parts) > 1 and parts[1].strip():
                        pc, nc = _parse_segbits_line(parts[1])
                        if len(pc) != len(np.unique(pc)) or \
                                len(nc) != len(np.unique(nc)):
                            pc, nc = np.unique(pc), np.unique(nc)
                    else:
                        pc = nc = np.zeros(0, dtype=np.int64)
                    pos.append(pc)
                    neg.append(nc)
        d = []
        if os.path.exists(defaults_path):
            d = sorted({l.split()[0] for l in open(defaults_path)
                        if l.strip()})
        self._arrays(pos, neg, np.unique(np.array(
            [RM.name_code(b) for b in d], dtype=np.int64)))
        return self

    def _arrays(self, pos, neg, defaults):
        self.npos = np.array([len(p) for p in pos], dtype=np.int64)
        self.nneg = np.array([len(p) for p in neg], dtype=np.int64)
        self.pos_ptr = np.concatenate(([0], np.cumsum(self.npos)))
        self.neg_ptr = np.concatenate(([0], np.cumsum(self.nneg)))
        cat = (lambda x: np.concatenate(x).astype(np.int64) if x else
               np.zeros(0, dtype=np.int64))
        self.pos = cat(pos)
        self.neg = cat(neg)
        self.defaults = defaults
        # Every feature with set bits is found through one anchor bit (its
        # least shared set bit, ties by code): the candidates are the
        # (region, feature) pairs with the anchor set, whose other bits are
        # then verified.
        fid = np.flatnonzero(self.npos)
        if len(self.pos):
            fan = np.bincount(self.pos)
            key = fan[self.pos] * (np.int64(1) << np.int64(32)) + self.pos
            best = np.minimum.reduceat(key, self.pos_ptr[fid])
            anc = best & ((np.int64(1) << np.int64(32)) - 1)
        else:
            anc = np.zeros(0, dtype=np.int64)
        order = np.argsort(anc, kind='stable')
        self.anchor = anc[order]
        self.anchor_f = fid[order]
        # per anchor-sorted feature: bits to verify (for chunking)
        self.anchor_cost = np.concatenate(([0], np.cumsum(
            self.npos[self.anchor_f] + self.nneg[self.anchor_f])))

    def __init__(self, feats, defaults):
        """From Database.get's (name, pos set, neg set) features."""
        self.names = [f[0] for f in feats]
        self._arrays(
            [np.array(sorted(RM.name_code(b) for b in f[1]), dtype=np.int64)
             for f in feats],
            [np.array(sorted(RM.name_code(b) for b in f[2]), dtype=np.int64)
             for f in feats],
            np.unique(np.array([RM.name_code(b) for b in defaults],
                               dtype=np.int64)))


def _expand(ptr, counts, sel):
    """For the features sel: (index into sel, index into the flat bit
    array) of all their bits."""
    cnt = counts[sel]
    tot = int(cnt.sum())
    rep = np.repeat(np.arange(len(sel), dtype=np.int64), cnt)
    start = np.repeat(np.cumsum(cnt) - cnt, cnt)
    flat = np.repeat(ptr[sel], cnt) + (np.arange(tot, dtype=np.int64) -
                                       start)
    return rep, flat


def _member(sorted_keys, q):
    """Boolean mask: q in sorted_keys (sorted numpy array)."""
    if not len(sorted_keys) or not len(q):
        return np.zeros(len(q), dtype=bool)
    i = np.searchsorted(sorted_keys, q)
    i[i == len(sorted_keys)] = 0
    return sorted_keys[i] == q


_KEY_M = np.int64(1) << np.int64(32)


# feature bits verified per step (memory bound; URAY_CHECK_CHUNK for tests)
DECODE_CHUNK = int(os.environ.get("URAY_CHECK_CHUNK", 1 << 25))


def decode_type(tc, reg, code):
    """_decode_type in chunks of whole regions (pairs are grouped by
    region), each verifying at most ~DECODE_CHUNK feature bits (types
    with huge feature bit sets otherwise expand to tens of GB)."""
    lo = np.searchsorted(tc.anchor, code, 'left')
    hi = np.searchsorted(tc.anchor, code, 'right')
    cost = np.cumsum(tc.anchor_cost[hi] - tc.anchor_cost[lo])
    if not len(cost) or cost[-1] <= DECODE_CHUNK:
        return _decode_type(tc, reg, code)
    # region boundaries, and cut points at most DECODE_CHUNK apart
    starts = np.concatenate(([0], np.flatnonzero(reg[1:] != reg[:-1]) + 1))
    before = np.concatenate(([0], cost))[starts]
    cuts = [0]
    before_cut = 0
    for i, c in zip(starts.tolist(), before.tolist()):
        if i and c - before_cut > DECODE_CHUNK:
            cuts.append(i)
            before_cut = c
    cuts.append(len(reg))
    doc = np.zeros(len(reg), dtype=bool)
    mr, mf = [], []
    for a, b in zip(cuts, cuts[1:]):
        d, (r, f) = _decode_type(tc, reg[a:b], code[a:b])
        doc[a:b] = d
        mr.append(r)
        mf.append(f)
    return doc, (np.concatenate(mr), np.concatenate(mf))


def _decode_type(tc, reg, code):
    """Vectorised Database.decode of all regions of one (tile type, region
    index).  reg, code: the (region, relative bit code) pairs of the set
    bits.  Returns (documented mask of the pairs, (regions, feature
    indices) of the matched features)."""
    keys = reg * _KEY_M + code
    skeys = np.sort(keys)
    doc = _member(tc.defaults, code)
    lo = np.searchsorted(tc.anchor, code, 'left')
    hi = np.searchsorted(tc.anchor, code, 'right')
    cnt = hi - lo
    tot = int(cnt.sum())
    if not tot:
        return doc, (np.zeros(0, np.int64), np.zeros(0, np.int64))
    start = np.repeat(np.cumsum(cnt) - cnt, cnt)
    cf = tc.anchor_f[np.repeat(lo, cnt) +
                     (np.arange(tot, dtype=np.int64) - start)]
    cr = np.repeat(reg, cnt)
    ok = np.ones(tot, dtype=bool)
    # All set bits of the feature set,
    rep, flat = _expand(tc.pos_ptr, tc.npos, cf)
    miss = ~_member(skeys, cr[rep] * _KEY_M + tc.pos[flat])
    ok[rep[miss]] = False
    # and none of its "!" bits.
    sel = np.flatnonzero(ok)
    rep, flat = _expand(tc.neg_ptr, tc.nneg, cf[sel])
    hit = _member(skeys, cr[sel][rep] * _KEY_M + tc.neg[flat])
    ok[sel[rep[hit]]] = False
    mr, mf = cr[ok], cf[ok]
    # The bits of matched features are documented.
    rep, flat = _expand(tc.pos_ptr, tc.npos, mf)
    doc |= _member(np.unique(mr[rep] * _KEY_M + tc.pos[flat]), keys)
    return doc, (mr, mf)


def col_region_keys(col):
    """(array: region -> key index, [(tile type, region index within
    tile)]) of a Collector, cached on it."""
    if getattr(col, '_region_keys', None) is None:
        keys = {}
        kidx = np.zeros(len(col.regions), dtype=np.int64)
        for tile, rlist in col.tile_regions.items():
            for k, i in enumerate(rlist):
                kidx[i] = keys.setdefault((col.regions[i][1], k), len(keys))
        col._region_keys = (kidx, list(keys))
    return col._region_keys


def check_ids(col, db, ids, fasm=None):
    """Returns (unowned bit count, {tile type: Counter(bit -> count)}).
    If fasm is a list, decoded "<tile>.<feature>" lines are appended.
    Vectorised; same results (and Counter order) as check_ids_ref."""
    rm = col.rmap
    pos, reg, code = rm.pairs(ids)
    kidx_of, keys = col_region_keys(col)
    kidx = kidx_of[reg]
    doc = np.zeros(len(reg), dtype=bool)
    order = np.argsort(kidx, kind='stable')
    bounds = np.flatnonzero(np.diff(kidx[order])) + 1
    for grp in np.split(order, bounds):
        if not len(grp):
            continue
        tt, k = keys[kidx[grp[0]]]
        tc = db.codes(tt, k)
        d, (mr, mf) = decode_type(tc, reg[grp], code[grp])
        doc[grp] = d
        if fasm is not None:
            for r, f in zip(mr.tolist(), mf.tolist()):
                fasm.append(f'{col.regions[r][0]}.'
                            f'{tc.names[f].split(".", 1)[1]}')
    # A bit is documented when some owner documents it.
    bad = ~_member(np.unique(pos[doc]), pos)
    unknown = collections.defaultdict(collections.Counter)
    for r, b in zip(reg[bad].tolist(), rm.names(code[bad])):
        unknown[col.regions[r][1]][b] += 1
    check_ids.undocumented_bits = len(np.unique(pos[bad]))
    return col.unowned(ids, rm.owned(ids, pos)), unknown


def check_ids_ref(col, db, ids, fasm=None):
    """Reference (per bit Python) implementation of check_ids."""
    rb = col.region_bits(ids)
    unknown = collections.defaultdict(collections.Counter)
    # region idx -> (tile type, region index within tile)
    rinfo = {}
    for tile, rlist in col.tile_regions.items():
        for k, i in enumerate(rlist):
            rinfo[i] = (col.regions[i][1], k)
    # Documented global ids per region; shared bits need one owner.
    documented = set()
    undoc = []
    for i, bits in rb.items():
        tt, k = rinfo[i]
        matched, doc = db.decode(tt, k, bits)
        if fasm is not None:
            tile = col.regions[i][0]
            for m in matched:
                fasm.append(f'{tile}.{m.split(".", 1)[1]}')
        _, _, fi, _, off, _ = col.regions[i]
        for b in bits:
            f, o = b.split('_')
            gid = (fi + int(f)) * col.nbf + off + int(o)
            if b in doc:
                documented.add(gid)
            else:
                undoc.append((gid, tt, b))
    bad = set()
    for gid, tt, b in undoc:
        if gid not in documented:
            unknown[tt][b] += 1
            bad.add(gid)
    check_ids_ref.undocumented_bits = len(bad)
    return col.unowned(ids), unknown


# Registers whose value is fully defined by their meaning.
SEMANTIC_REGS = {
    'CMD': 'command code',
    'FAR': 'frame address',
    'IDCODE': 'device IDCODE',
    'CRC': 'CRC of the preceding data',
    'MFWR': 'multi frame write',
    'LOUT': 'legacy output',
    'RBCRC_SW': 'readback CRC',
}


def check_registers(path, dbdir):
    """Configuration register writes: bits not explained by the register
    database (option values, fixed bits) or by the register's meaning."""
    import regdb
    opts = []
    p = os.path.join(dbdir, 'registers.db')
    if os.path.exists(p):
        for line in open(p):
            f = line.split()
            opts.append((f[0], set(f[1:])))
    fixed = set()
    p = os.path.join(dbdir, 'registers.txt')
    if os.path.exists(p):
        for line in open(p):
            f = line.split()
            if len(f) >= 2 and f[1] == 'fixed':
                fixed.update(f'{f[0]}:{b}' for b in f[2:])
    bits = set()
    for k, v in regdb.reg_writes(path):
        name = k.split('#')[0]
        if name in SEMANTIC_REGS or name.startswith('R'):
            continue
        bits.update(f'{k}:{b:02d}' for b in range(32) if (v >> b) & 1)
    doc = bits & fixed
    for name, obits in opts:
        doc |= bits & obits
    return sorted(bits - doc)


_CHECK = None


def _check_one(item):
    """Checks one input (collector and database from the parent).  A
    design input is (design directory, None): its bits are loaded here, so
    that the parent holds no bit arrays."""
    idx, (name, ids), want_fasm = item
    col, db, impl = _CHECK
    if ids is None:
        ids = DD.load_bits(col.df, name)
    fasm = [] if want_fasm else None
    unowned, unknown = impl(col, db, ids, fasm)
    return len(ids), unowned, dict(unknown), fasm, \
        impl.undocumented_bits, col.hidden


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--jobs', type=int, default=8,
                    help='inputs checked in parallel')
    ap.add_argument('--die', required=True)
    ap.add_argument('--bit', action='append', default=[])
    ap.add_argument('--designs', default=None)
    ap.add_argument('--max', type=int, default=1000)
    ap.add_argument('--top', type=int, default=10)
    ap.add_argument('--registers', action='store_true',
                    help='also check configuration register writes')
    ap.add_argument('--fasm', default=None,
                    help='write decoded features of the (first) input here')
    ap.add_argument('--ref', action='store_true',
                    help='per bit Python reference implementation (slow; '
                    'for equivalence tests)')
    args = ap.parse_args()
    die = dieslib.load()[args.die]
    dbdir = os.path.join(dieslib.DB, die.arch)
    tg = json.load(open(os.path.join(dbdir, die.name, 'tilegrid.json')))
    col = mkdb.Collector(die, tg)
    db = Database(dbdir)
    total_unowned = 0
    total = collections.defaultdict(collections.Counter)
    inputs = []
    for b in args.bit:
        _, frames, order = bitstream.frames_explicit(b, die.arch)
        # Per-frame-CRC bitstreams write every frame separately.
        if len(frames) < len(col.df.frames) // 2:
            frames = bitstream.frames_sequential(b, die.arch,
                                                 col.df.frames)
        far, word, bit = bitstream.set_bits(frames, die.arch)
        inputs.append((b, np.sort(col.df.bit_ids(far, word, bit))))
    if args.designs:
        for d in DD.design_dirs(args.designs)[:args.max]:
            inputs.append((d, None))
    if args.registers:
        for b in args.bit:
            und = check_registers(b, dbdir)
            print(f'{b}: undocumented register bits {len(und)} {und[:20]}')
            if und:
                total['REGISTERS'].update(und)
    global _CHECK
    _CHECK = (col, db, check_ids_ref if args.ref else check_ids)
    todo = [(idx, inp, bool(args.fasm and idx == 0))
            for idx, inp in enumerate(inputs)]
    if args.jobs > 1 and len(todo) > 1:
        # Forked workers share the collector and the database, loaded
        # here once (copy on write).
        if not args.ref:
            col.rmap
            kidx, keys = col_region_keys(col)
            for tt, k in keys:
                db.codes(tt, k)
        from concurrent.futures import ProcessPoolExecutor
        ex = ProcessPoolExecutor(min(args.jobs, len(todo)))
        results = ex.map(_check_one, todo)
    else:
        ex = None
        results = map(_check_one, todo)
    for (name, _), (nset, unowned, unknown, fasm, nbits, hidden) in zip(
            inputs, results):
        if fasm is not None:
            with open(args.fasm, 'w') as f:
                f.write('\n'.join(sorted(set(fasm))) + '\n')
        n = sum(sum(c.values()) for c in unknown.values())
        # n counts a bit once per owning tile; distinct bits too
        print(f'{name}: set {nset} unowned {unowned} undocumented {n} '
              f'bits {nbits} baseline_in_tileless_rows {hidden}')
        total_unowned += unowned
        for tt, c in unknown.items():
            total[tt].update(c)
    if ex:
        ex.shutdown()
    print('== undocumented bits by tile type')
    for tt, c in sorted(total.items(), key=lambda x: -sum(x[1].values())):
        print(f'{tt}: {sum(c.values())} ({len(c)} distinct) e.g. '
              f'{", ".join(f"{b}x{n}" for b, n in c.most_common(args.top))}')
    print('unowned total', total_unowned)
    return 1 if (total or total_unowned) else 0


if __name__ == '__main__':
    sys.exit(main())
