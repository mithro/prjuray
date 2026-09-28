#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Structural frame column assignment by sequence alignment.

Within a clock region row the configuration frame columns ("majors") of a
block follow the grid columns from left to right, and a frame column always
covers a run of neighbouring grid columns.  The number of frames of every
frame column is known from the frame address enumeration of the bitstream,
and it is characteristic of the tile columns it covers (e.g. 7-series CLB 36,
BRAM/DSP 28, IO 42, CMT/CFG/CLK 30; UltraScale+ INT 76, CLE 16).

Every grid column of a clock region row gets a *kind* (its most common tile
type with sites, else with PIPs, among the tiles with a bit window; columns
without such tiles are void and own no frames).  The assignment of the ordered grid column kinds to the ordered
frame column list is a monotonic alignment (a hidden Markov model whose
state is the frame column index), scored with:

* emission  log P(frame count | kind) / P0(frame count), learnt;
* transition  log P(new frame column | kind of the previous column, kind),
  learnt from neighbouring columns;
* design activity (the usage / bit change match scores of tilegrid.py) as a
  bonus for the frame column the column's tiles were seen changing;
* penalties for frame columns and grid columns left unassigned.

The model is learnt by hard EM over all dies of an architecture: it starts
from the activity supported assignment, and alternates between estimating
the kind tables from the assignments the activity does not contradict and
re-aligning every die.  Kinds without real activity on any die ("silent")
take part without a frame count preference and are not learnt from; those
left out join their nearest neighbour's frame column.  Clock region rows
with more unused frame columns than a die's best rows are realigned for
vertical consistency with them (see generic/README.md).

  colalign.py --arch Series7 --exp build/tilegrid_exp [--dies a,b,...]

reads <exp>/<arch>/<die>/evidence.json (tilegrid.py --evidence) and writes
<exp>/<arch>/<die>/colmap.json, <exp>/<arch>/model.json and a
self-consistency report on stdout.
"""
import argparse
import bisect
import collections
import json
import math
import os

import numpy as np

import designdata as DD
import dies as dieslib
import tilegrid as TG

# Scoring weights (natural log units).
ECAP = 8.0  # |emission| cap
W_ACT = 3.0  # activity bonus weight
W_VERT = 3.0  # bonus for the frame column of the same grid column in the
# best aligned clock region row
W_VERT_ANY = 0.0  # same, where the grid column has another kind there
S_MAJ = 2.0  # frame column without any grid column
S_COL = 3.0  # grid column of an active kind without a frame column
S_SILENT = 1.0  # same for a silent kind
MIN_ACT = 2.0  # activity score (~ matching tiles) making a kind active
SUPPORTED = False  # learn only from activity supported assignments
SUP_ACT = 3.0  # activity score supporting an assignment
INIT_EA = False  # leave kinds whose activity neighbours explain out of the
# initial assignment
ALPHA = 1.0  # emission smoothing
BETA = 1.0  # transition smoothing
MAXSKIP = 8  # consecutive unassigned grid columns


# Site types configured outside the configuration frames (the UltraScale+
# PS through its own registers): their tiles own no frame column; the
# PS8 tile in the bottom clock region row of the PS would otherwise take the
# frame column of the PS interface column next to it (prjuray-db:
# INT_INTF_LEFT_TERM_PSS in minor column 0 in every row).
OUTSIDE_FRAMES = {'PS8'}


def outside_frames(sites):
    """True for a tile whose sites (tiles.tsv field) all are of
    OUTSIDE_FRAMES types."""
    if sites == '-':
        return False
    return all(s.split(':')[-1] in OUTSIDE_FRAMES for s in sites.split(','))


def type_info(die):
    """tile type -> (npips, nsites)"""
    out = {}
    for line in open(die.types_txt):
        p = line.split()
        if p and p[0] == 'tiletype':
            out[p[1]] = (int(p[4]), int(p[5]))
    return out


class DieRows:
    """Structural description of a die: per clock region row, the ordered
    non void grid columns with their kind, the frame columns of its frame
    row and the activity evidence."""

    def __init__(self, die, evpath):
        self.die = die
        self.grid = TG.Grid(die)
        self.dframes = DD.DieFrames(die)
        self.cols = TG.frame_columns(self.dframes)
        self.ev = TG.load_evidence(evpath, self.cols)
        tinfo = type_info(die)

        def cfg(t):
            np_, ns = tinfo.get(t, (0, 0))
            return np_ > 0 or ns > 0

        counts = collections.defaultdict(collections.Counter)
        # grid row -> [(gx, tile)] of the tiles that can own bits
        self.gyrows = collections.defaultdict(list)
        self.sited = {k for k, v in tinfo.items() if v[1] > 0}
        for name, t in self.grid.tiles.items():
            # Tiles without PIPs or sites, or without a bit window (e.g.
            # break rows between clock regions) own no configuration bits.
            if t['type'] == 'NULL' or not cfg(t['type']) or \
                    self.grid.tile_window(name) is None or \
                    outside_frames(t['sites']):
                continue
            counts[(self.grid.crrow[t['gy']], t['gx'])][t['type']] += 1
            self.gyrows[t['gy']].append((t['gx'], name))
        for lst in self.gyrows.values():
            lst.sort()
        # Void columns: grid columns with tiles (feed through, breaks, PS
        # placeholders, ...) but none that can own bits.
        present = collections.defaultdict(set)
        for t in self.grid.tiles.values():
            if t['type'] != 'NULL':
                present[self.grid.crrow[t['gy']]].add(t['gx'])
        self.voids = {}
        for cr, gxs in present.items():
            self.voids[cr] = sorted(gxs)
        self.rows = {}
        for (cr, gx), c in counts.items():
            # Tile types with sites first (a hard block column also holds
            # break / clock distribution tiles), then the most common.
            kind = sorted(c.items(), key=lambda kv: (
                -int(tinfo[kv[0]][1] > 0), -kv[1], kv[0]))[0][0]
            self.rows.setdefault(cr, []).append((gx, kind))
        for cr in self.rows:
            self.rows[cr].sort()
        for cr in list(self.voids):
            own = {gx for gx, _ in self.rows.get(cr, ())}
            self.voids[cr] = sorted(set(self.voids[cr]) - own)
        self.crmap = dict(self.ev['crmap'])
        self.structural_rows()

    @property
    def ratio(self):
        """Grid columns (with bits) per frame column."""
        crs = [cr for cr in self.rows if self.majors(cr)]
        nc = sum(len(self.rows[cr]) for cr in crs)
        nm = sum(len(self.majors(cr)) for cr in crs)
        return max(1.0, nc / nm) if nm else 1.0

    def structural_rows(self):
        """Clock region rows without an activity learnt frame row get the
        frame row left over when the learnt rows are removed, if the order
        of the others makes it unique (frame rows are numbered away from the
        centre of the die in each half)."""
        keys = sorted(k for k in self.cols if k[0] == 0)
        crs = sorted(self.rows)
        missing = [cr for cr in crs if cr not in self.crmap]
        free = [k for k in keys if k not in self.crmap.values()]
        if missing and len(missing) == 1 and len(free) == 1:
            self.crmap[missing[0]] = free[0]

    def row_keys(self):
        return sorted(k for k in self.cols if k[0] == 0)

    def order_consistent(self, order):
        """None if the die's learnt frame rows do not fit the frame row
        ordering (clock region rows ascending), else the number fitting."""
        crs = sorted(self.rows)
        keys = sorted(self.row_keys(), key=order)
        if len(keys) != len(crs):
            return None
        want = dict(zip(crs, keys))
        n = 0
        for cr, k in self.crmap.items():
            if want.get(cr) != k:
                return None
            n += 1
        return n

    def order_conflicts(self, order):
        """Clock region rows whose learnt frame row contradicts the ordering
        when strictly more learnt rows fit it; else []."""
        crs = sorted(self.rows)
        keys = sorted(self.row_keys(), key=order)
        if len(keys) != len(crs):
            return []
        want = dict(zip(crs, keys))
        bad = [cr for cr, k in self.crmap.items() if want.get(cr) != k]
        if bad and len(self.crmap) - len(bad) > len(bad):
            return sorted(bad)
        return []

    def apply_order(self, order):
        crs = sorted(self.rows)
        keys = sorted(self.row_keys(), key=order)
        for cr, k in zip(crs, keys):
            self.crmap.setdefault(cr, k)

    def majors(self, cr):
        key = self.crmap.get(cr)
        if key is None:
            return None
        return self.cols[key]

    def activity(self, cr, gx, M):
        v = self.ev['scores'].get(cr, {}).get(gx)
        if v is None or v.max() < MIN_ACT:
            return None
        return v / (v.max() + 1.0)


RARE = 10  # pairs seen fewer times also use generalised kinds


def gen(kind):
    """A tile type name without its leading qualifier."""
    return kind.split('_', 1)[1] if '_' in kind else kind


class Model:
    def __init__(self):
        self.emit = collections.defaultdict(collections.Counter)
        self.pair = collections.defaultdict(lambda: [0, 0])  # [same, split]
        self.left = collections.defaultdict(lambda: [0, 0])  # (a, *)
        self.right = collections.defaultdict(lambda: [0, 0])  # (*, b)
        self.p0 = {}
        self.psplit = 0.5

    def fit(self, assignments, nf_all, noemit=()):
        """assignments: list of rows, each a list of (kind, frame column
        index or None, frame count or None).  Kinds in noemit (silent
        kinds, placed by the alignment alone) are left out of the tables,
        so that an alignment does not reinforce itself."""
        self.emit.clear()
        self.pair.clear()
        self.left.clear()
        self.right.clear()
        for row in assignments:
            prev = None
            for kind, j, nf in row:
                if j is None:
                    continue
                if kind in noemit:
                    # no pair across it either
                    prev = None
                    continue
                if nf is None:
                    # contradicted by activity: not learnt from
                    prev = None
                    continue
                self.emit[kind][nf] += 1
                if prev is not None:
                    x = int(prev[1] != j)
                    self.pair[(prev[0], kind)][x] += 1
                    self.left[prev[0]][x] += 1
                    self.right[kind][x] += 1
                prev = (kind, j)
        tot = sum(nf_all.values())
        self.p0 = {nf: n / tot for nf, n in nf_all.items()}
        # Prior of a new frame column between two grid columns.
        s, t = (sum(v[k] for v in self.pair.values()) for k in (0, 1))
        self.psplit = (t + 1) / (s + t + 2)

    def emission(self, kind, nf):
        c = self.emit.get(kind)
        p0 = self.p0.get(nf, 1e-3)
        if not c:
            return 0.0
        # Witten-Bell smoothing: a kind seen with several frame counts (an
        # interconnect column sharing the frame column of its neighbour)
        # keeps more probability for unseen frame counts than a kind always
        # seen with one.
        n = sum(c.values())
        T = len(c)
        if nf in c:
            p = c[nf] / (n + T)
        elif n - max(c.values()) >= 0.05 * n:
            # Flexible kind (e.g. interconnect sharing its neighbour's
            # frame column): no preference against unseen frame counts.
            return 0.0
        else:
            rest = sum(v for k, v in self.p0.items() if k not in c)
            p = T / (n + T) * p0 / max(rest, 1e-6)
        return max(-ECAP, min(ECAP, math.log(p / p0)))

    def transition(self, a, b):
        """(log P(same frame column), log P(new frame column))"""
        same, split = self.pair.get((a, b), (0, 0))
        if same + split < RARE:
            # Rare pair: add the pairs of the kinds' generalisations (the
            # name without its leading qualifier, e.g. GTP_INT_INTERFACE_R
            # -> INT_INTERFACE_R), when known.
            for ga, gb in ((gen(a), b), (a, gen(b)), (gen(a), gen(b))):
                s, t = self.pair.get((ga, gb), (0, 0))
                same, split = same + s, split + t
        if same + split == 0:
            # Unseen pair: back off to what follows a and what precedes b
            # (e.g. a kind always alone in its frame column).
            s1, t1 = self.left.get(a, (0, 0))
            s2, t2 = self.right.get(b, (0, 0))
            same, split = s1 + s2, t1 + t2
        n = same + split
        p = (split + BETA * self.psplit) / (n + BETA)
        return math.log(1 - p), math.log(p)

    def to_json(self):
        return dict(
            emission={k: dict(sorted((str(nf), n) for nf, n in c.items()))
                      for k, c in sorted(self.emit.items())},
            transitions=[[a, b, s, t]
                         for (a, b), (s, t) in sorted(self.pair.items())],
            p0={str(k): round(v, 4) for k, v in sorted(self.p0.items())})


def align_row(model, kinds, nfs, act, skip, gxs, voids, extra=None,
              ratio=1.0, refcol=None):
    """kinds: grid column kinds (left to right) at grid x gxs, nfs: frame
    counts of the frame columns, act: per column activity vector (or None),
    skip: cost of leaving each grid column unassigned, voids: grid x of the
    void columns of the row, ratio: grid columns per frame column of the
    die.  Frame columns left without a grid column cost S_MAJ each, except
    as many as the void grid columns at that place would make (a frame
    column of columns without configurable tiles, e.g. feed through columns
    replacing fabric; with refcol, grid x -> frame column in the die's
    best aligned row, the distinct frame columns of the void columns there).
    Returns the frame column index (or None) of every grid column."""
    n, M = len(kinds), len(nfs)
    vv = np.array(voids, dtype=float)

    refcol, refkind = refcol or ({}, {})
    rowkind = dict(zip(gxs, kinds))
    replaced = sorted(g for g, k in rowkind.items()
                      if g in refkind and refkind[g] != k)
    unrep = sorted(g for g, k in rowkind.items()
                   if g in refkind and refkind[g] == k)

    def nvoid(a, b):
        """frame columns the void columns strictly between grid x a and b
        can stand for: their frame columns in the reference row (with those
        of the columns from a to b replaced by another kind), at least
        void columns / grid columns per frame column"""
        lo = int(np.searchsorted(vv, a, 'right'))
        hi = int(np.searchsorted(vv, b))
        cols = set()
        other = 0
        for g in voids[lo:hi]:
            if g in refcol:
                cols.add(refcol[g])
            else:
                other += 1
        for g in replaced:
            if a <= g <= b:
                cols.add(refcol[g])
        n = max(len(cols) + int(other / ratio), int((hi - lo) / ratio))
        if replaced and any(a <= g <= b for g in replaced):
            # Inside a block replacing reference columns (e.g. GT quads in
            # the fabric), the frame columns of the whole block, between
            # the unreplaced columns around it, may go unused.
            k = bisect.bisect_right(unrep, a) - 1
            m = bisect.bisect_left(unrep, b)
            if k >= 0 and m < len(unrep):
                n = max(n, abs(refcol[unrep[m]] - refcol[unrep[k]]) - 1)
        return n

    cs = np.concatenate([[0.0], np.cumsum(skip)])  # cs[i] = sum skip[:i]
    if n == 0 or M == 0:
        return [None] * n, 0.0
    NEG = -1e18
    E = np.zeros((n, M))
    for i, k in enumerate(kinds):
        E[i] = [model.emission(k, nf) for nf in nfs]
        if act[i] is not None:
            E[i] += W_ACT * act[i]
    if extra is not None:
        E += extra
    dp = np.full((n, M), NEG)
    back = {}
    jidx = np.arange(M)
    # D[j, j'] = frame columns skipped going from j' to j
    D = jidx[:, None] - jidx[None, :] - 1
    valid = D >= 0
    for i in range(n):
        # start: columns 0..i-1 unassigned, frame columns 0..j-1 unused
        a0 = nvoid(-1, gxs[i])
        best = -cs[i] - S_MAJ * np.maximum(0, jidx - a0)
        arg = np.full((M, 2), -1)
        for ip in range(max(0, i - 1 - MAXSKIP), i):
            skipped = cs[i] - cs[ip + 1]
            ls, lt = model.transition(kinds[ip], kinds[i])
            same = dp[ip] + ls - skipped
            a = nvoid(gxs[ip], gxs[i])
            val = np.where(valid, dp[ip][None, :] -
                           S_MAJ * np.maximum(0, D - a), NEG)
            jp = np.argmax(val, axis=1)
            split = val[jidx, jp] + lt - skipped
            take = same > best
            best = np.where(take, same, best)
            arg[take] = np.stack([np.full(M, ip), jidx], axis=1)[take]
            take = split > best
            best = np.where(take, split, best)
            arg[take] = np.stack([np.full(M, ip), jp], axis=1)[take]
        dp[i] = best + E[i]
        back[i] = arg
    # end
    aend = np.array([nvoid(g, 10**9) for g in gxs])
    tail = dp - (cs[n] - cs[1:])[:, None] - S_MAJ * np.maximum(
        0, (M - 1 - jidx)[None, :] - aend[:, None])
    i, j = np.unravel_index(int(np.argmax(tail)), tail.shape)
    total = float(tail[i, j])
    out = [None] * n
    while i >= 0:
        out[i] = int(j)
        ip, jp = back[i][j]
        if ip < 0:
            break
        i, j = int(ip), int(jp)
    return out, total


def explain_away(dr, cr, s, M):
    """Activity vectors of the grid columns s of a clock region row.  A
    column whose best frame column is also the best one of a directly
    adjacent column with more activity gets no activity bonus there: tiles
    used together with their neighbour (e.g. interface tiles next to the
    interconnect) show the neighbour's bit changes, so their activity does
    not tell which frame column they own."""
    raw = [dr.ev['scores'].get(cr, {}).get(gx) for gx, _ in s]
    out = [dr.activity(cr, gx, M) for gx, _ in s]
    for i, (gx, _) in enumerate(s):
        v = raw[i]
        if v is None or out[i] is None:
            continue
        j = int(np.argmax(v))
        for k in (i - 1, i + 1):
            if 0 <= k < len(s) and abs(s[k][0] - gx) == 1 and \
                    raw[k] is not None and int(np.argmax(raw[k])) == j \
                    and raw[k][j] > v[j]:
                out[i] = out[i].copy()
                out[i][j] = 0.0
                break
    return out


def align_die(model, dr, d, active):
    """Aligns every clock region row of a die.  The column structure
    repeats vertically: rows left with more unused frame columns (e.g. where
    the PS replaces part of the fabric, so that many frame columns have no
    grid column) are aligned again with a bonus for the frame column the
    same grid column has in the best aligned row (fewest unused frame
    columns)."""
    def run(cr, ref=None, refcol=None):
        m = dr.majors(cr)
        s = dr.rows[cr]
        kinds = [k for _, k in s]
        act = explain_away(dr, cr, s, len(m))
        skip = [S_COL if k in active else S_SILENT for k in kinds]
        extra = None
        if ref:
            extra = np.zeros((len(s), len(m)))
            for i, (gx, k) in enumerate(s):
                for j, c in enumerate(m):
                    if (c[0], c[2]) == ref.get((gx, k)):
                        extra[i, j] = W_VERT
                    elif c[0] == refcol[0].get(gx):
                        extra[i, j] = W_VERT_ANY
        out, _ = align_row(model, kinds, [c[2] for c in m], act, skip,
                           [gx for gx, _ in s], dr.voids.get(cr, []), extra,
                           dr.ratio, refcol)
        return {gx: j for (gx, _), j in zip(s, out) if j is not None}

    res = {}
    crs = [cr for cr in dr.rows if dr.majors(cr)]
    for cr in crs:
        res[cr] = run(cr)

    def unused(cr):
        return len(dr.majors(cr)) - len(set(res[cr].values()))

    order = sorted(crs, key=lambda cr: (unused(cr), -len(res[cr])))
    if order:
        best = unused(order[0])
        ref = {}
        refcol = {}
        refkind = {}
        for cr in [c for c in order if unused(c) == best]:
            m = dr.majors(cr)
            kinds = dict(dr.rows[cr])
            for gx, j in res[cr].items():
                ref.setdefault((gx, kinds[gx]), (m[j][0], m[j][2]))
                if gx not in refcol:
                    refcol[gx] = m[j][0]
                    refkind[gx] = kinds[gx]
        for cr in order:
            if unused(cr) > best:
                res[cr] = run(cr, ref, (refcol, refkind))
    return {(d, cr): asg for cr, asg in res.items()}


def attach_silent(rows_all, assigned, model=None, maxdist=16):
    """Silent non void columns (kinds without activity on any die) join the
    frame column of the nearest assigned grid column: of both neighbours
    when they agree, else of the strictly nearer one (at a row end: only a
    directly adjacent one).  Nearer columns are attached first, so runs of
    silent columns follow their neighbour."""
    out = dict(assigned)
    gxs = [gx for gx, _ in rows_all]
    kinds = dict(rows_all)
    for d in range(1, maxdist + 1):
        while True:
            known = sorted(out.items())
            add = {}
            for gx in gxs:
                if gx in out:
                    continue
                left = [(g, m) for g, m in known if g < gx]
                right = [(g, m) for g, m in known if g > gx]
                L = left[-1] if left else None
                R = right[0] if right else None
                if L and R and L[1] == R[1]:
                    add[gx] = L[1]
                    continue
                dl = gx - L[0] if L else None
                dr = R[0] - gx if R else None
                if L and R:
                    if dl < dr and dl <= d:
                        add[gx] = L[1]
                    elif dr < dl and dr <= d:
                        add[gx] = R[1]
                    elif dl == dr and dl <= d and model is not None:
                        # Equally near: the side more likely to share.
                        k = kinds[gx]
                        pl = model.transition(kinds[L[0]], k)[0]
                        pr = model.transition(k, kinds[R[0]])[0]
                        if pl > pr + 0.1:
                            add[gx] = L[1]
                        elif pr > pl + 0.1:
                            add[gx] = R[1]
                elif L and dl == 1:
                    add[gx] = L[1]
                elif R and dr == 1:
                    add[gx] = R[1]
            if not add:
                break
            out.update(add)
    return out


CLAIM_NEED_EVIDENCE = True


def claim_unused(dr, cr, full, ref=None):
    """A grid column sharing its neighbour's frame column moves to an unused
    frame column right next to it on its own side, when its activity there
    is positive or it has that frame column (index and frame count) in
    all other clock region rows (ref: gx -> {(index, frames)}) (UltraScale+
    INT_INTF_LEFT_TERM_IO_FT: the 4 minor column between CMT_L and INT;
    the HMM lets interface columns share INT's)."""
    M = dr.majors(cr)
    out = dict(full)
    used = set(out.values())
    order = sorted(out.items())
    for i, (gx, j) in enumerate(order):
        mates = [g for g, k in order if k == j and g != gx]
        if not mates:
            continue
        v = dr.ev['scores'].get(cr, {}).get(gx)
        # own side: below the mates' columns -> the frame column before
        for step, side in ((-1, all(gx < g for g in mates)),
                           (1, all(gx > g for g in mates))):
            n = j + step
            if not side or n < 0 or n >= len(M) or n in used:
                continue
            if CLAIM_NEED_EVIDENCE and not (
                    (v is not None and n < len(v) and v[n] > 0) or
                    (ref and (n, M[n][2]) in ref.get(gx, ()))):
                continue
            # nothing between gx and the next grid column on that side may
            # use a frame column beyond n
            nb = [k for g, k in order if (g < gx if step < 0 else g > gx)]
            near = max(nb) if step < 0 and nb else (min(nb) if nb else None)
            if near is not None and (near >= n if step < 0 else near <= n):
                continue
            out[gx] = n
            used.add(n)
            break
    # The first (last) grid column of a row without activity, placed away
    # from its neighbour across unused frame columns, moves next to it
    # (xcu25 rows with the PS: INT_INTF_LEFT_TERM_PSS right of the PS's
    # unused frame columns, not at their start).
    order = sorted(out.items())
    for (gx, j), (ng, nj), step in ((order[0], order[1], 1),
                                    (order[-1], order[-2], -1)) \
            if len(order) > 1 else ():
        n = nj - step
        v = dr.ev['scores'].get(cr, {}).get(gx)
        if n == j or (v is not None and v.max() > 0):
            continue
        between = range(min(j, n) + 1, max(j, n)) if step > 0 else \
            range(min(j, n), max(j, n))
        if (n > j) != (step > 0) or n in used or \
                any(m in used for m in between if m != j):
            continue
        used.discard(j)
        out[gx] = n
        used.add(n)
    return out


def edge_frames(dr, cr, full, table):
    """The last (first) grid column of a row whose frame column has another
    frame count than its kind usually has (over all dies, silent kinds
    included) moves to the nearest unused frame column further out with
    that count (xazu3teg: a GTH_QUAD_RIGHT of the row without CMT took the
    4 minor column before the die edge's 8 minor one)."""
    if len(full) < 2:
        return full
    M = dr.majors(cr)
    out = dict(full)
    used = set(out.values())
    kinds = dict(dr.rows[cr])
    order = sorted(out.items())
    for (gx, j), step in ((order[-1], 1), (order[0], -1)):
        c = table.emit.get(kinds.get(gx))
        if not c:
            continue
        nf, n = c.most_common(1)[0]
        if M[j][2] == nf or n < 0.8 * sum(c.values()):
            continue
        k = j + step
        while 0 <= k < len(M):
            if k not in used and M[k][2] == nf:
                used.discard(j)
                out[gx] = k
                used.add(k)
                break
            k += step
    return out


def size_fix(dr, cr, full, table, silent):
    """A column of a silent kind whose frame column has another size than
    the kind's usual one (>= 60% of its columns over all dies) moves to the
    neighbouring frame column (index +-1) of that size, shared or not
    (xcku025 middle I/O column: HPIO_L 16 -> the 10 minor column holding
    its pull bits, XIPHY_L sharing INT's 58 -> the 16 minor one)."""
    M = dr.majors(cr)
    out = dict(full)
    kinds = dict(dr.rows[cr])
    for gx, j in sorted(full.items()):
        k = kinds.get(gx)
        c = table.emit.get(k)
        if k not in silent or not c:
            continue
        nf, n = c.most_common(1)[0]
        if M[j][2] == nf or n < 0.6 * sum(c.values()):
            continue
        near = [i for i in (j - 1, j + 1) if 0 <= i < len(M) and
                M[i][2] == nf]
        if len(near) == 1:
            out[gx] = near[0]
    return out


def minority_tiles(dr, cr, full):
    """Hard block tiles (with sites) of another type than their column's
    kind, e.g. a PCIE block over part of a CLB column, where the other grid
    columns sharing their column's frame column are absent: they take the
    frame column of their nearest neighbours in their own grid row (both
    sides agreeing, or the strictly nearer one) when it differs from their
    column's.  Returns {tile: frame column index}."""
    kinds = dict(dr.rows[cr])
    out = {}
    for name, t in dr.grid.tiles.items():
        if dr.grid.crrow.get(t['gy']) != cr or t['type'] not in dr.sited:
            continue
        gx = t['gx']
        if kinds.get(gx) in (None, t['type']) or gx not in full:
            continue
        row = [(g, n) for g, n in dr.gyrows.get(t['gy'], ()) if g in full]
        # Only when the other grid columns of its column's frame column are
        # missing in this grid row (the block replaces them), e.g. not for
        # clock row tiles of a column that is alone in its frame column.
        partners = [g for g, j in full.items()
                    if j == full[gx] and g != gx]
        here = {g for g, _ in row}
        if not partners or any(g in here for g in partners):
            continue
        left = [g for g, n in row if g < gx]
        right = [g for g, n in row if g > gx]
        L = left[-1] if left else None
        R = right[0] if right else None
        if L is not None and R is not None and full[L] == full[R]:
            j = full[L]
        elif L is not None and (R is None or gx - L < R - gx):
            j = full[L]
        elif R is not None and (L is None or R - gx < gx - L):
            j = full[R]
        else:
            continue
        if j != full[gx]:
            out[name] = j
    return out


def show(dr, cur, d, model, active):
    for cr in sorted(dr.rows):
        m = dr.majors(cr)
        print(f'== {d} clock region row {cr} frame row {dr.crmap.get(cr)}')
        if not m:
            continue
        asg = attach_silent(dr.rows[cr], cur[(d, cr)], model)
        used = set()
        for gx, k in dr.rows[cr]:
            j = asg.get(gx)
            v = dr.ev['scores'].get(cr, {}).get(gx)
            a = ''
            if v is not None and v.max() > 0:
                a = f'act {int(np.argmax(v))} ({v.max():.1f})'
            mark = '' if k in active else ' (silent)'
            if j is not None:
                used.add(j)
                print(f'  {gx:4d} {k:28s} -> {j:3d} col {m[j][0]:3d} '
                      f'nf {m[j][2]:3d} {a}{mark}')
            else:
                print(f'  {gx:4d} {k:28s} ->   - {a}{mark}')
        print('  unused frame columns:',
              ' '.join(f'{j}:{m[j][2]}' for j in range(len(m))
                       if j not in used))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--arch', required=True)
    ap.add_argument('--exp', required=True,
                    help='experiment directory (<exp>/<arch>/<die>/)')
    ap.add_argument('--dies', help='comma separated (default: all with an '
                    'evidence file)')
    ap.add_argument('--iters', type=int, default=8)
    ap.add_argument('--w-act', type=float, default=W_ACT,
                    help='weight of the design activity bonus')
    ap.add_argument('--verbose', action='store_true',
                    help='list frame count mismatches and activity '
                    'disagreements')
    ap.add_argument('--supported', action='store_true',
                    help='learn the tables only from assignments the '
                    'activity supports (not merely does not contradict)')
    ap.add_argument('--init-explained', action='store_true',
                    help='leave kinds whose activity is explained by their '
                    'neighbours out of the initial assignment')
    ap.add_argument('--show', help='comma separated dies whose alignment '
                    'is printed')
    ap.add_argument('--init-dies', help='dies whose activity assignment '
                    'initialises the model (default: all)')
    ap.add_argument('--learn-exclude', default='',
                    help='comma separated dies aligned with the model but '
                    'not learnt from (e.g. dies with only region designs, '
                    'whose activity matches many frame columns)')
    args = ap.parse_args()
    globals()['W_ACT'] = args.w_act
    globals()['INIT_EA'] = args.init_explained
    globals()['SUPPORTED'] = args.supported
    learn_ex = set(filter(None, args.learn_exclude.split(',')))
    alldies = dieslib.load()
    base = os.path.join(args.exp, args.arch)
    names = args.dies.split(',') if args.dies else sorted(
        d for d in os.listdir(base)
        if os.path.exists(os.path.join(base, d, 'evidence.json')))
    names = [d for d in names if alldies[d].arch == args.arch]
    rows = {}
    for d in names:
        rows[d] = DieRows(alldies[d], os.path.join(base, d, 'evidence.json'))
        print('loaded', d, flush=True)
    # Frame rows of clock region rows without activity: the order of the
    # frame rows along the clock region rows is learnt from the dies with
    # learnt frame rows among a few candidate orderings.
    orders = {
        'half, row': lambda k: (k[1], k[2]),
        'reversed half, row': lambda k: (-k[1], -k[2]),
        'outwards from the centre': lambda k: (-k[1], -k[2] if k[1] else k[2]),
        'inwards to the centre': lambda k: (k[1], k[2] if k[1] else -k[2]),
    }
    votes = collections.Counter()
    for dr in rows.values():
        for name, o in orders.items():
            n = dr.order_consistent(o)
            if n:
                votes[name] += n
    if votes:
        best = votes.most_common(1)[0][0]
        print(f'frame row order: {best} ({dict(votes)})', flush=True)
        for d, dr in rows.items():
            # Learnt frame rows contradicting the order while most of the
            # die's learnt rows fit it are dropped with their activity
            # (scored against the wrong frame row; xcu25 with few designs:
            # row 1 -> frame row 2).
            bad = dr.order_conflicts(orders[best])
            if bad:
                print(f'  {d}: learnt frame rows against the order dropped: '
                      f'{ {cr: dr.crmap[cr] for cr in bad} }', flush=True)
                for cr in bad:
                    del dr.crmap[cr]
                    dr.ev['scores'].pop(cr, None)
            if len(dr.crmap) < len(dr.rows) and \
                    dr.order_consistent(orders[best]) is not None:
                dr.apply_order(orders[best])
                print(f'  {d}: frame rows from the order', flush=True)
    # Kinds with activity somewhere take part in the alignment.
    active = set()
    for d, dr in rows.items():
        for cr, lst in dr.rows.items():
            sc = dr.ev['scores'].get(cr, {})
            for gx, k in lst:
                v = sc.get(gx)
                if v is not None and v.max() >= MIN_ACT:
                    active.add(k)
    silent = {k for dr in rows.values() for lst in dr.rows.values()
              for _, k in lst} - active
    nf_all = collections.Counter()
    for dr in rows.values():
        for cr in dr.rows:
            m = dr.majors(cr)
            if m:
                nf_all.update(nf for _, _, nf in m)

    def seqs(dr, cr):
        return dr.rows[cr]

    # Initial assignment: activity only, directly supported (no
    # propagation between rows or gap filling).  Kinds whose activity is
    # (almost) always explained by a more active adjacent column (tiles used
    # together with their neighbour) are left out: their activity does not
    # tell which frame column they own.
    cur = {}
    init = set(args.init_dies.split(',')) if args.init_dies else set(names)
    raw = {}
    explained = collections.defaultdict(lambda: [0, 0])
    for d, dr in rows.items():
        if d not in init:
            continue
        colmap, _ = TG.assign_activity(dr.grid, dr.cols, dr.ev, fill=False)
        for cr in dr.rows:
            m = dr.majors(cr)
            if not m:
                continue
            idx = {c[0]: j for j, c in enumerate(m)}
            s = seqs(dr, cr)
            act = explain_away(dr, cr, s, len(m))
            asg = {}
            for (gx, k), a in zip(s, act):
                j = idx.get(colmap.get((cr, gx)))
                v = dr.ev['scores'].get(cr, {}).get(gx)
                if j is None or v is None or v[j] < MIN_ACT:
                    continue
                asg[gx] = j
                explained[k][int(a is None or a[j] == 0)] += 1
            raw[(d, cr)] = (s, asg)
    dropped = {k for k, (n0, n1) in explained.items()
               if n1 >= 0.9 * (n0 + n1)} if INIT_EA else set()
    if dropped:
        print('activity explained by neighbours:', ' '.join(sorted(dropped)))
    for key, (s, asg) in raw.items():
        kinds = dict(s)
        cur[key] = {gx: j for gx, j in asg.items()
                    if kinds[gx] not in dropped}
    model = Model()
    for it in range(args.iters):
        # Frame counts are learnt from assignments the activity does not
        # contradict (an alignment must not reinforce its own mistakes).
        data = []
        for (d, cr), asg in cur.items():
            if d in learn_ex:
                continue
            dr = rows[d]
            m = dr.majors(cr)
            sc = dr.ev['scores'].get(cr, {})
            row = []
            for gx, k in seqs(dr, cr):
                j = asg.get(gx)
                nf = m[j][2] if j is not None else None
                v = sc.get(gx)
                if j is not None and v is not None and \
                        v.max() >= MIN_ACT and v[j] < 0.5 * v.max():
                    nf = None
                if SUPPORTED and j is not None and \
                        (v is None or v[j] < SUP_ACT):
                    # only assignments the activity supports
                    nf = None
                row.append((k, j, nf))
            data.append(row)
        model.fit(data, nf_all, silent)
        new = {}
        changed = 0
        for d, dr in rows.items():
            new.update(align_die(model, dr, d, active))
        for k, asg in new.items():
            if asg != cur.get(k):
                changed += 1
        cur = new
        print(f'iteration {it}: rows changed {changed}', flush=True)
        if not changed:
            break
    final = [[(k, cur[(d, cr)].get(gx),
               rows[d].majors(cr)[cur[(d, cr)][gx]][2]
               if gx in cur[(d, cr)] else None)
              for gx, k in seqs(rows[d], cr)]
             for (d, cr) in cur if d not in learn_ex]
    model.fit(final, nf_all, silent)
    with open(os.path.join(base, 'model.json'), 'w') as f:
        json.dump(model.to_json(), f, indent=1)
    table = Model()
    table.fit(final, nf_all)
    print('frame count table (kind: frame count x grid columns; silent '
          'kinds marked *):')
    for k, c in sorted(table.emit.items()):
        mark = '*' if k in silent else ''
        print(f'  {k}{mark}: ' + ', '.join(
            f'{nf} x{n}' for nf, n in sorted(c.items(), key=lambda x: -x[1])))
    # Output + self-consistency.
    # Frame counts of the frame columns each tile type's tiles are in (over
    # all dies): a type found in frame columns of several sizes shares them
    # with the column's owner (e.g. interconnect next to CLB, BRAM, DSP, I/O
    # columns) and only uses the frames of the smallest.
    type_nf = collections.defaultdict(set)
    shared = collections.defaultdict(lambda: [0, 0])  # type -> [alone, shared]
    for d, dr in rows.items():
        for cr in dr.rows:
            m = dr.majors(cr)
            if not m:
                continue
            full = attach_silent(dr.rows[cr], cur[(d, cr)], model)
            idx = {gx: m[j][2] for gx, j in full.items()}
            nshare = collections.Counter(full.values())
            kinds = dict(dr.rows[cr])
            for t in dr.grid.tiles.values():
                if dr.grid.crrow.get(t['gy']) == cr and t['gx'] in idx and \
                        t['type'] != 'NULL' and \
                        kinds.get(t['gx']) not in silent:
                    type_nf[t['type']].add(idx[t['gx']])
                    shared[t['type']][int(nshare[full[t['gx']]] > 1)] += 1
    # Only types (nearly) always sharing their frame column with another
    # grid column, next to several kinds of columns (at least 3 frame
    # counts: not e.g. a hard block column seen with two sizes); columns of
    # silent kinds (placed without activity) do not count.
    frames = {t: min(v) for t, v in sorted(type_nf.items())
              if len(v) >= 3 and shared[t][1] >= 0.9 * sum(shared[t])}
    with open(os.path.join(base, 'frames.json'), 'w') as f:
        json.dump(frames, f, indent=1, sort_keys=True)
    print('frame count of shared column tile types:',
          ' '.join(f'{t}:{n}' for t, n in frames.items()))
    for d, dr in rows.items():
        colmap = []
        tilemap = {}
        rep = collections.Counter()
        first = {}
        for cr in sorted(dr.rows):
            if dr.majors(cr):
                first[cr] = claim_unused(dr, cr, attach_silent(
                    dr.rows[cr], cur[(d, cr)], model))
        for cr in sorted(dr.rows):
            m = dr.majors(cr)
            if not m:
                rep['rows without frame row'] += 1
                continue
            asg = cur[(d, cr)]
            # Only a frame column the grid column has in every other row.
            ref = collections.defaultdict(set)
            for c2, f2 in first.items():
                if c2 != cr:
                    for gx, j in f2.items():
                        ref[gx].add((j, dr.majors(c2)[j][2]))
            ref = {gx: v for gx, v in ref.items() if len(v) == 1}
            full = size_fix(dr, cr, edge_frames(
                dr, cr, claim_unused(dr, cr, first[cr], ref), table),
                table, silent)
            for gx, j in sorted(full.items()):
                colmap.append([cr, gx, m[j][0]])
            for name, j in minority_tiles(dr, cr, full).items():
                tilemap[name] = m[j][0]
            used = set(asg.values())
            rep['frame columns'] += len(m)
            rep['frame columns unused'] += len(m) - len(used)
            kinds = dict(dr.rows[cr])
            for gx, j in asg.items():
                k = kinds[gx]
                c = model.emit.get(k, {})
                n = sum(c.values())
                # A frame count seen (over all dies) at most twice for a
                # common kind is suspicious.
                if n >= 20 and c.get(m[j][2], 0) <= 2:
                    rep['rare frame counts'] += 1
                    if args.verbose:
                        print(f'  {d} row {cr} x {gx} {k}: frame count '
                              f'{m[j][2]}, usually {dict(c)}')
                v = dr.ev['scores'].get(cr, {}).get(gx)
                if v is not None and v.max() >= 3 and v[j] < 0.5 * v.max():
                    rep['activity disagreements'] += 1
                    if args.verbose:
                        print(f'  {d} row {cr} x {gx} {k}: frame column '
                              f'index {j}, activity {int(np.argmax(v))} '
                              f'({v.max():.1f})')
            for gx, k in seqs(dr, cr):
                if gx not in full:
                    rep['active columns unassigned' if k in active else
                        'silent columns unassigned'] += 1
        if args.show and d in args.show.split(','):
            show(dr, cur, d, model, active)
        with open(os.path.join(base, d, 'colmap.json'), 'w') as f:
            json.dump(dict(crmap={str(cr): list(k)
                                  for cr, k in dr.crmap.items()},
                           colmap=colmap, tiles=tilemap), f)
        print(d, ', '.join(f'{k} {v}' for k, v in sorted(rep.items())),
              flush=True)


if __name__ == '__main__':
    main()
