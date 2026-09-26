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
from the activity only assignment, and alternates between estimating the
kind tables from all assignments and re-aligning every die.  Only kinds with
design activity on some die take part in the alignment; other non void
columns ("silent" kinds) are attached to the frame column of their
neighbours when both sides agree.

  colalign.py --arch Series7 --exp build/tilegrid_exp [--dies a,b,...]

reads <exp>/<arch>/<die>/evidence.json (tilegrid.py --evidence) and writes
<exp>/<arch>/<die>/colmap.json, <exp>/<arch>/model.json and a
self-consistency report on stdout.
"""
import argparse
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
S_MAJ = 2.0  # frame column without any grid column
S_COL = 3.0  # grid column of an active kind without a frame column
S_SILENT = 1.0  # same for a silent kind
ALPHA = 1.0  # emission smoothing
BETA = 1.0  # transition smoothing
MAXSKIP = 8  # consecutive unassigned grid columns


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
                    self.grid.tile_window(name) is None:
                continue
            counts[(self.grid.crrow[t['gy']], t['gx'])][t['type']] += 1
            self.gyrows[t['gy']].append((t['gx'], name))
        for lst in self.gyrows.values():
            lst.sort()
        self.rows = {}
        for (cr, gx), c in counts.items():
            # Tile types with sites first (a hard block column also holds
            # break / clock distribution tiles), then the most common.
            kind = sorted(c.items(), key=lambda kv: (
                -int(tinfo[kv[0]][1] > 0), -kv[1], kv[0]))[0][0]
            self.rows.setdefault(cr, []).append((gx, kind))
        for cr in self.rows:
            self.rows[cr].sort()
        self.crmap = dict(self.ev['crmap'])
        self.structural_rows()

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

    def majors(self, cr):
        key = self.crmap.get(cr)
        if key is None:
            return None
        return self.cols[key]

    def activity(self, cr, gx, M):
        v = self.ev['scores'].get(cr, {}).get(gx)
        if v is None:
            return None
        return v / (v.max() + 1.0)


class Model:
    def __init__(self):
        self.emit = collections.defaultdict(collections.Counter)
        self.pair = collections.defaultdict(lambda: [0, 0])  # [same, split]
        self.left = collections.defaultdict(lambda: [0, 0])  # (a, *)
        self.right = collections.defaultdict(lambda: [0, 0])  # (*, b)
        self.p0 = {}

    def fit(self, assignments, nf_all):
        """assignments: list of rows, each a list of (kind, frame column
        index or None, frame count or None)."""
        self.emit.clear()
        self.pair.clear()
        self.left.clear()
        self.right.clear()
        for row in assignments:
            prev = None
            for kind, j, nf in row:
                if j is None:
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
        else:
            rest = sum(v for k, v in self.p0.items() if k not in c)
            p = T / (n + T) * p0 / max(rest, 1e-6)
        return max(-ECAP, min(ECAP, math.log(p / p0)))

    def transition(self, a, b):
        """(log P(same frame column), log P(new frame column))"""
        same, split = self.pair.get((a, b), (0, 0))
        if same + split == 0:
            # Unseen pair: back off to what follows a and what precedes b
            # (e.g. a kind always alone in its frame column).
            s1, t1 = self.left.get(a, (0, 0))
            s2, t2 = self.right.get(b, (0, 0))
            same, split = s1 + s2, t1 + t2
        n = same + split
        p = (split + BETA * 0.5) / (n + BETA)
        return math.log(1 - p), math.log(p)

    def to_json(self):
        return dict(
            emission={k: dict(sorted((str(nf), n) for nf, n in c.items()))
                      for k, c in sorted(self.emit.items())},
            transitions=[[a, b, s, t]
                         for (a, b), (s, t) in sorted(self.pair.items())],
            p0={str(k): round(v, 4) for k, v in sorted(self.p0.items())})


def align_row(model, kinds, nfs, act, skip):
    """kinds: grid column kinds (left to right), nfs: frame counts of the
    frame columns, act: per column activity vector (or None), skip: cost of
    leaving each grid column unassigned.  Returns the frame column index (or
    None) of every grid column."""
    n, M = len(kinds), len(nfs)
    cs = np.concatenate([[0.0], np.cumsum(skip)])  # cs[i] = sum skip[:i]
    if n == 0 or M == 0:
        return [None] * n, 0.0
    NEG = -1e18
    E = np.zeros((n, M))
    for i, k in enumerate(kinds):
        E[i] = [model.emission(k, nf) for nf in nfs]
        if act[i] is not None:
            E[i] += W_ACT * act[i]
    dp = np.full((n, M), NEG)
    back = {}
    jidx = np.arange(M)
    for i in range(n):
        # start: columns 0..i-1 unassigned, frame columns 0..j-1 unused
        best = -cs[i] - S_MAJ * jidx
        arg = np.full((M, 2), -1)
        for ip in range(max(0, i - 1 - MAXSKIP), i):
            skipped = cs[i] - cs[ip + 1]
            ls, lt = model.transition(kinds[ip], kinds[i])
            same = dp[ip] + ls - skipped
            # split: max over j' < j of dp[ip][j'] - S_MAJ * (j - j' - 1)
            shifted = dp[ip] + S_MAJ * jidx
            run = np.maximum.accumulate(shifted)
            runarg = np.zeros(M, dtype=int)
            cur = 0
            for j in range(M):
                if shifted[j] >= shifted[cur]:
                    cur = j
                runarg[j] = cur
            split = np.full(M, NEG)
            split[1:] = run[:-1] - S_MAJ * (jidx[1:] - 1) + lt - skipped
            for j in range(M):
                if same[j] > best[j]:
                    best[j] = same[j]
                    arg[j] = (ip, j)
                if split[j] > best[j]:
                    best[j] = split[j]
                    arg[j] = (ip, runarg[j - 1])
        dp[i] = best + E[i]
        back[i] = arg
    # end
    tail = dp - (cs[n] - cs[1:])[:, None] - \
        S_MAJ * (M - 1 - jidx)[None, :]
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


def minority_tiles(dr, cr, full):
    """Hard block tiles (with sites) of another type than their column's
    kind, e.g. a PCIE block over part of a CLB column: they take the frame
    column of their nearest neighbours in their own grid row (both sides
    agreeing, or the strictly nearer one) when it differs from their
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
    ap.add_argument('--iters', type=int, default=6)
    ap.add_argument('--show', help='comma separated dies whose alignment '
                    'is printed')
    ap.add_argument('--init-dies', help='dies whose activity assignment '
                    'initialises the model (default: all)')
    args = ap.parse_args()
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
    # Kinds with activity somewhere take part in the alignment.
    active = set()
    for d, dr in rows.items():
        for cr, lst in dr.rows.items():
            sc = dr.ev['scores'].get(cr, {})
            for gx, k in lst:
                v = sc.get(gx)
                if v is not None and v.max() > 0:
                    active.add(k)
    nf_all = collections.Counter()
    for dr in rows.values():
        for cr in dr.rows:
            m = dr.majors(cr)
            if m:
                nf_all.update(nf for _, _, nf in m)

    def seqs(dr, cr):
        return dr.rows[cr]

    # Initial assignment: activity only.
    cur = {}
    init = set(args.init_dies.split(',')) if args.init_dies else set(names)
    for d, dr in rows.items():
        if d not in init:
            continue
        colmap, _ = TG.assign_activity(dr.grid, dr.cols, dr.ev)
        for cr in dr.rows:
            m = dr.majors(cr)
            if not m:
                continue
            idx = {c[0]: j for j, c in enumerate(m)}
            cur[(d, cr)] = {gx: idx[colmap[(cr, gx)]]
                            for gx, _ in seqs(dr, cr)
                            if (cr, gx) in colmap
                            and colmap[(cr, gx)] in idx}
    model = Model()
    for it in range(args.iters):
        data = []
        for (d, cr), asg in cur.items():
            dr = rows[d]
            m = dr.majors(cr)
            data.append([(k, asg.get(gx),
                          m[asg[gx]][2] if gx in asg else None)
                         for gx, k in seqs(dr, cr)])
        model.fit(data, nf_all)
        new = {}
        changed = 0
        for d, dr in rows.items():
            for cr in dr.rows:
                m = dr.majors(cr)
                if not m:
                    continue
                s = seqs(dr, cr)
                kinds = [k for _, k in s]
                act = [dr.activity(cr, gx, len(m)) for gx, _ in s]
                skip = [S_COL if k in active else S_SILENT for k in kinds]
                out, _ = align_row(model, kinds, [c[2] for c in m], act,
                                   skip)
                asg = {gx: j for (gx, _), j in zip(s, out) if j is not None}
                if asg != cur.get((d, cr)):
                    changed += 1
                new[(d, cr)] = asg
        cur = new
        print(f'iteration {it}: rows changed {changed}', flush=True)
        if not changed:
            break
    model.fit([[(k, cur[(d, cr)].get(gx),
                 rows[d].majors(cr)[cur[(d, cr)][gx]][2]
                 if gx in cur[(d, cr)] else None)
                for gx, k in seqs(rows[d], cr)]
               for (d, cr) in cur], nf_all)
    with open(os.path.join(base, 'model.json'), 'w') as f:
        json.dump(model.to_json(), f, indent=1)
    # Output + self-consistency.
    for d, dr in rows.items():
        colmap = []
        tilemap = {}
        rep = collections.Counter()
        for cr in sorted(dr.rows):
            m = dr.majors(cr)
            if not m:
                rep['rows without frame row'] += 1
                continue
            asg = cur[(d, cr)]
            full = attach_silent(dr.rows[cr], asg, model)
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
                if n >= 3 and c.get(m[j][2], 0) < 0.05 * n:
                    rep['frame count mismatches'] += 1
                v = dr.ev['scores'].get(cr, {}).get(gx)
                if v is not None and v.max() >= 3 and v[j] < 0.5 * v.max():
                    rep['activity disagreements'] += 1
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
