#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Cross-check the generic Series7 database against a prjxray-db checkout.

For every prjxray-db device whose die we have a tile grid for, compare:

1. The tile grid: prjxray `baseaddr/frames/offset/words` (per CLB_IO_CLK and
   BLOCK_RAM block) against our regions (`base/frames/offset/nbits`, offset
   and size in bits; one 7-series word is 32 bits).
2. The bits: prjxray `segbits_<type>[.block_ram].db` against our
   `segbits_<type>[.1].db`, `segbits_opt_<type>.db` and `defaults_<type>.db`.
   prjxray bits are `<frame>_<bit>` relative to the tile's baseaddr and
   `offset * 32`; ours are relative to the region's base and first bit.  The
   per tile type offset difference found by the tile grid comparison is
   applied when mapping prjxray bits to ours.

Usage:
  xcheck_prjxray.py --prjxray-db <path> --out <report.md>
      [--db build/db/Series7] [--build build] [--tilegrid-only]
"""
import argparse
import collections
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
URAY_DIR = os.path.dirname(os.path.dirname(HERE))

FAMILIES = ('artix7', 'kintex7', 'spartan7', 'zynq7', 'virtex7')
# Other architectures (tile grid comparison only): prjuray-db families.
ARCH_FAMILIES = {
    'Series7': FAMILIES,
    'UltraScale': ('kintexu', ),
    'UltraScalePlus': ('zynqusp', 'kintexuplus'),
}
ARCH = 'Series7'
BLOCKS = {'CLB_IO_CLK': 0, 'BLOCK_RAM': 1}
PX_SUFFIX = {0: '', 1: '.block_ram'}
WORD = 32
SAMPLE = 12


def far_fields(far):
    """Frame address -> (block, half, row, col, minor)."""
    if ARCH != 'Series7':
        import bitstream
        return bitstream.far_fields(ARCH, far)
    return ((far >> 23) & 7, (far >> 22) & 1, (far >> 17) & 0x1f,
            (far >> 7) & 0x3ff, far & 0x7f)


def parse_bit(s):
    f, b = s.split('_')
    return int(f), int(b)


def fmt_bit(fb):
    return '%02d_%03d' % fb


# ---------------------------------------------------------------------------
# Tile grid


def nbad(c):
    """Real mismatches in a Counter of comparison results."""
    return sum(v for k, v in c.items() if k not in ('ok', 'frames+'))


def ours_regions(tile):
    out = {}
    for r in tile['bits']:
        out.setdefault(r['block'], r)
    return out


def compare_tilegrid(px, ours):
    """Returns a dict of per-die results."""
    res = {
        'tiles': 0,
        'regions': 0,
        'ok': 0,
        # identical except that our region spans more minors
        'ok_frames': 0,
        # (type, block) -> Counter((frames, nbits)) of our regions
        'shape': collections.defaultdict(collections.Counter),
        # (type, block) -> Counter(kind of mismatch)
        'by_type': collections.defaultdict(collections.Counter),
        # (type, block) -> Counter((dframe, dbit)) offset deltas
        'delta': collections.defaultdict(collections.Counter),
        'examples': collections.defaultdict(list),
        # gx -> Counter((px col, our col)) for block 0
        'cols': collections.defaultdict(collections.Counter),
        'col_types': collections.defaultdict(set),
        # (px half,row) -> Counter((our half,row))
        'rows': collections.defaultdict(collections.Counter),
        'px_only': collections.Counter(),
        'ours_only': collections.Counter(),
        'missing_tiles': collections.Counter(),
        # prjxray (block, half, row) -> Counter(col) of regions missing in ours
        'missing_at': collections.defaultdict(collections.Counter),
        # tile type -> (alias type, start_offset)
        'alias': {},
    }
    for name, t in px.items():
        o = ours.get(name)
        if o is None:
            if t['bits']:
                res['missing_tiles'][t['type']] += 1
            continue
        res['tiles'] += 1
        oregs = ours_regions(o)
        pblocks = set()
        for bt, b in t['bits'].items():
            block = BLOCKS.get(bt)
            if block is None:
                continue
            pblocks.add(block)
            if 'alias' in b:
                res['alias'][t['type']] = (b['alias']['type'],
                                           b['alias']['start_offset'])
            key = (t['type'], block)
            r = oregs.get(block)
            if r is None:
                res['px_only'][key] += 1
                res['by_type'][key]['missing in ours'] += 1
                pf = far_fields(int(b['baseaddr'], 16))
                res['missing_at'][pf[:3]][pf[3]] += 1
                if len(res['examples'][key]) < 3:
                    res['examples'][key].append(
                        (name, b, None))
                continue
            res['regions'] += 1
            pbase = int(b['baseaddr'], 16)
            obase = int(r['base'], 16)
            pf = far_fields(pbase)
            of = far_fields(obase)
            bad = []
            if pf[:4] != of[:4]:
                if pf[1:3] != of[1:3]:
                    bad.append('row')
                if pf[3] != of[3]:
                    bad.append('col')
                if pf[0] != of[0]:
                    bad.append('block')
            if pf[4] != of[4]:
                bad.append('minor')
            # prjxray gives INT/HCLK tiles only the minors they use (26/28)
            # while our regions span the whole frame column: "frames+"
            # (ours larger) is a convention difference, "frames-" is not.
            if b['frames'] < r['frames']:
                bad.append('frames+')
            elif b['frames'] > r['frames']:
                bad.append('frames-')
            if b['offset'] * WORD != r['offset']:
                bad.append('offset')
            if b['words'] * WORD != r['nbits']:
                bad.append('size')
            res['delta'][key][(of[4] - pf[4],
                               r['offset'] - b['offset'] * WORD)] += 1
            if block == 0:
                res['cols'][t['grid_x']][(pf[3], of[3])] += 1
                res['col_types'][t['grid_x']].add(t['type'])
                res['rows'][pf[1:3]][of[1:3]] += 1
            if not bad:
                res['ok'] += 1
                res['by_type'][key]['ok'] += 1
            elif bad == ['frames+']:
                res['ok_frames'] += 1
                res['by_type'][key]['frames+'] += 1
            else:
                for x in bad:
                    res['by_type'][key][x] += 1
                if len(res['examples'][key]) < 3:
                    res['examples'][key].append((name, b, r))
        for block in oregs:
            if block not in pblocks:
                res['ours_only'][(t['type'], block)] += 1
    for t in ours.values():
        for block, r in ours_regions(t).items():
            res['shape'][(t['type'], block)][(r['frames'], r['nbits'])] += 1
    return res


def fmt_region_px(b):
    return '%s f%d o%d w%d (%d bits @%d)' % (
        b['baseaddr'].lower(), b['frames'], b['offset'], b['words'],
        b['words'] * WORD, b['offset'] * WORD)


def fmt_region_ours(r):
    if r is None:
        return '-'
    return '%s f%d %d bits @%d' % (r['base'], r['frames'], r['nbits'],
                                    r['offset'])


def px_regions(res):
    return res['regions'] + sum(res['px_only'].values())


def report_tilegrid(out, device, die, res):
    w = out.append
    w(f'### {device} (our die `{die}`)\n')
    w(f'Common tiles: {res["tiles"]}, prjxray regions: {px_regions(res)}, '
      f'identical: {res["ok"]}, identical except ours spans more minors: '
      f'{res["ok_frames"]}, missing in ours: {sum(res["px_only"].values())}, '
      f'different: {res["regions"] - res["ok"] - res["ok_frames"]}.\n')
    if res['missing_tiles']:
        w('Tiles with prjxray bits missing from our tile grid: ' +
          ', '.join(f'{k} ({v})' for k, v in
                    res['missing_tiles'].most_common()) + '\n')
    bad = [(k, c) for k, c in res['by_type'].items()
           if nbad(c)]
    if bad:
        w('| tile type | block | ok | mismatches | offset delta (frame, bits) '
          '| example (prjxray / ours) |')
        w('| --- | --- | --- | --- | --- | --- |')
        for (tt, block), c in sorted(bad, key=lambda x: (
                -nbad(x[1]), x[0])):
            mism = ', '.join(f'{k} {v}' for k, v in sorted(c.items())
                             if k != 'ok')
            deltas = ', '.join(f'{d} x{n}' for d, n in
                               res['delta'][(tt, block)].most_common(3))
            ex = res['examples'][(tt, block)]
            exs = '; '.join(f'`{n}` {fmt_region_px(b)} / {fmt_region_ours(r)}'
                            for n, b, r in ex[:1])
            w(f'| {tt} | {block} | {c.get("ok", 0)} | {mism} | {deltas} '
              f'| {exs} |')
        w('')
    else:
        w('All compared regions are identical.\n')
    if res['missing_at']:
        w('Regions missing in ours, by prjxray frame address '
          '(block, top/bottom, row): prjxray columns (regions):\n')
        for k in sorted(res['missing_at']):
            c = res['missing_at'][k]
            w(f'* {k}: ' + ', '.join(f'{col} ({n})'
                                     for col, n in sorted(c.items())))
        w('')
    if res['ours_only']:
        w('Regions only in our tile grid (prjxray has no bits for them): ' +
          ', '.join(f'{t}/{b} ({n})' for (t, b), n in
                    sorted(res['ours_only'].items())) + '\n')
    wrong_cols = []
    for gx in sorted(res['cols']):
        c = res['cols'][gx]
        wrong = [(p, o, n) for (p, o), n in c.items() if p != o]
        if wrong:
            wrong_cols.append((gx, wrong, sum(c.values())))
    if wrong_cols:
        w('Grid columns whose frame column differs (CLB_IO_CLK):\n')
        w('| grid x | tile types | prjxray col -> our col (tiles) |')
        w('| --- | --- | --- |')
        for gx, wrong, tot in wrong_cols:
            types = ', '.join(sorted(res['col_types'][gx]))
            s = ', '.join(f'{p}->{o} ({n}/{tot})' for p, o, n in wrong)
            w(f'| {gx} | {types} | {s} |')
        w('')
    else:
        w('All frame columns agree.\n')
    wrong_rows = []
    for pr in sorted(res['rows']):
        c = res['rows'][pr]
        wrong = [(o, n) for o, n in c.items() if o != pr]
        if wrong:
            wrong_rows.append((pr, wrong, sum(c.values())))
    if wrong_rows:
        w('Clock region rows whose frame row differs '
          '(prjxray (top/bottom, row) -> ours):\n')
        for pr, wrong, tot in wrong_rows:
            w(f'* {pr} -> ' + ', '.join(f'{o} ({n}/{tot} tiles)'
                                        for o, n in wrong))
        w('')
    else:
        w('All frame rows agree.\n')


# ---------------------------------------------------------------------------
# Bits


def load_px_segbits(pxdb):
    """(TYPE, block) -> {feature: (pos set, neg set)}, union over families,
    and (TYPE, block) -> families.  Features whose bits differ between
    families keep the first definition (conflicts are counted)."""
    feats = collections.defaultdict(dict)
    fams = collections.defaultdict(set)
    conflicts = collections.Counter()
    masks = collections.defaultdict(set)
    for fam in FAMILIES:
        d = os.path.join(pxdb, fam)
        if not os.path.isdir(d):
            continue
        for path in sorted(glob.glob(os.path.join(d, 'segbits_*.db'))):
            base = os.path.basename(path)[len('segbits_'):-len('.db')]
            if base.endswith('.origin_info'):
                continue
            block = 0
            if base.endswith('.block_ram'):
                block = 1
                base = base[:-len('.block_ram')]
            ttype = base.upper()
            fams[(ttype, block)].add(fam)
            for line in open(path):
                p = line.split()
                if not p:
                    continue
                name = p[0].split('.', 1)[1]
                pos = frozenset(parse_bit(b) for b in p[1:]
                                if not b.startswith('!'))
                neg = frozenset(parse_bit(b[1:]) for b in p[1:]
                                if b.startswith('!'))
                old = feats[(ttype, block)].get(name)
                if old is None:
                    feats[(ttype, block)][name] = (pos, neg)
                elif old != (pos, neg):
                    conflicts[(ttype, block)] += 1
        for path in sorted(glob.glob(os.path.join(d, 'mask_*.db'))):
            base = os.path.basename(path)[len('mask_'):-len('.db')]
            if base.endswith('.origin_info'):
                continue
            block = 0
            if base.endswith('.block_ram'):
                block = 1
                base = base[:-len('.block_ram')]
            for line in open(path):
                p = line.split()
                if len(p) == 2 and p[0] == 'bit':
                    masks[(base.upper(), block)].add(parse_bit(p[1]))
    return feats, fams, conflicts, masks


def load_ours(dbdir, ttype, block):
    suffix = ttype.lower() + (f'.{block}' if block else '')
    feats = {}
    for path in (os.path.join(dbdir, f'segbits_{suffix}.db'),
                 os.path.join(dbdir, f'segbits_opt_{suffix}.db')):
        if not os.path.exists(path):
            continue
        for line in open(path):
            p = line.split()
            if not p:
                continue
            name = p[0].split('.', 1)[1]
            pos = frozenset(parse_bit(b) for b in p[1:]
                            if not b.startswith('!'))
            neg = frozenset(parse_bit(b[1:]) for b in p[1:]
                            if b.startswith('!'))
            feats[name] = (pos, neg)
    defaults = set()
    path = os.path.join(dbdir, f'defaults_{suffix}.db')
    if os.path.exists(path):
        for line in open(path):
            if line.strip():
                defaults.add(parse_bit(line.split()[0]))
    have = any(os.path.exists(os.path.join(dbdir, f'{p}_{suffix}.db'))
               for p in ('segbits', 'segbits_opt', 'defaults'))
    return feats, defaults, have


def ours_pip_index(feats):
    """(dst, src) -> our feature name for plain PIP features."""
    idx = {}
    for name in feats:
        if '&' in name:
            continue
        if '<->' in name:
            a, b = name.split('<->', 1)
            idx.setdefault((a, b), name)
            idx.setdefault((b, a), name)
        elif '->' in name:
            src, dst = name.split('->', 1)
            idx.setdefault((dst, src), name)
    return idx


def compare_bits(pxf, pxmask, ours, defaults, delta, shape=None):
    """Maps prjxray bits onto ours and compares them.  delta = (our first
    minor - prjxray first minor, our first bit - prjxray first bit)."""
    df, db = delta

    def m(fb):
        return (fb[0] - df, fb[1] - db)

    px = {n: (frozenset(map(m, p)), frozenset(map(m, q)))
          for n, (p, q) in pxf.items()}
    pxmask = set(map(m, pxmask))
    px_bits = collections.defaultdict(list)
    for n, (p, q) in px.items():
        for b in p | q:
            px_bits[b].append(n)
    our_bits = collections.defaultdict(list)
    for n, (p, q) in ours.items():
        for b in p | q:
            our_bits[b].append(n)
    for b in defaults:
        our_bits[b].append('<default>')
    r = {}
    r['px_bits'] = len(px_bits)
    r['our_bits'] = len(our_bits)
    r['common'] = len(set(px_bits) & set(our_bits))
    px_only = sorted(set(px_bits) - set(our_bits))
    # prjxray bits outside our region of the tile type are a tile grid
    # difference (the bits belong to another tile of ours, or to none).
    if shape is not None:
        frames, nbits = shape
        r['px_only'] = [b for b in px_only
                        if 0 <= b[0] < frames and 0 <= b[1] < nbits]
        r['px_outside'] = [b for b in px_only
                           if not (0 <= b[0] < frames and 0 <= b[1] < nbits)]
    else:
        r['px_only'] = px_only
        r['px_outside'] = []
    r['shape'] = shape
    r['our_only'] = sorted(set(our_bits) - set(px_bits))
    r['our_only_not_mask'] = sorted(set(r['our_only']) - pxmask) \
        if pxmask else None
    r['px_bits_map'] = px_bits
    r['our_bits_map'] = our_bits
    # Name correspondence of PIPs.
    pipidx = ours_pip_index(ours)
    same, diff, nomatch_pip = [], [], []
    by_pos = collections.defaultdict(list)
    for n, (p, q) in ours.items():
        if p:
            by_pos[p].append(n)
    bitmatch, nomatch = [], []
    for n, (p, q) in sorted(px.items()):
        parts = n.split('.')
        on = None
        if len(parts) == 2:
            on = pipidx.get((parts[0], parts[1]))
        if on is not None:
            if ours[on][0] == p:
                same.append((n, on))
            else:
                diff.append((n, on))
            continue
        if len(parts) == 2 and not n.startswith(('SLICE', 'IOB', 'OLOGIC',
                                                 'ILOGIC', 'IDELAY', 'RAMB')):
            nomatch_pip.append(n)
        cand = by_pos.get(p) if p else None
        if cand:
            bitmatch.append((n, cand))
        else:
            nomatch.append(n)
    r['pip_same'] = same
    r['pip_diff'] = diff
    # ours has extra bits (correlation noise or bits shared with other
    # features) / misses bits / other.
    r['pip_diff_kind'] = collections.Counter(
        'extra' if ours[on][0] > px[n][0] else
        'missing' if ours[on][0] < px[n][0] else 'other'
        for n, on in diff)
    r['pip_nomatch'] = nomatch_pip
    r['bitmatch'] = bitmatch
    r['nomatch'] = nomatch
    r['px_feats'] = len(px)
    r['our_feats'] = len(ours)
    r['px'] = px
    r['ours'] = ours
    return r


def pick_delta(tg_results, ttype, block):
    c = collections.Counter()
    for res in tg_results:
        c.update(res['delta'].get((ttype, block), {}))
    if not c:
        return (0, 0), None
    d, n = c.most_common(1)[0]
    return d, (n, sum(c.values()))


def pick_shape(tg_results, ttype, block):
    """Most common (frames, nbits) of our regions of a tile type."""
    c = collections.Counter()
    for res in tg_results:
        c.update(res['shape'].get((ttype, block), {}))
    return c.most_common(1)[0][0] if c else None


def report_bits(out, key, fams, conflicts, r, delta, dstat):
    w = out.append
    ttype, block = key
    w(f'### {ttype}' + (' (BLOCK_RAM)' if block else '') + '\n')
    w(f'prjxray families: {", ".join(sorted(fams))}'
      + (f'; {conflicts} features differ between families'
         if conflicts else '') + '.  ')
    if delta != (0, 0):
        w(f'Bit mapping delta (frame, bits) applied: {delta} '
          f'(seen on {dstat[0]}/{dstat[1]} tiles).  ')
    w(f'Features: prjxray {r["px_feats"]}, ours {r["our_feats"]}.  '
      f'Bits: prjxray {r["px_bits"]}, ours {r["our_bits"]}, '
      f'common {r["common"]}.\n')

    def bitlist(bits, where, n=SAMPLE):
        s = []
        for b in bits[:n]:
            fs = where[b]
            s.append(f'`{fmt_bit(b)}` ({fs[0]}' +
                     (f' +{len(fs) - 1}' if len(fs) > 1 else '') + ')')
        return ', '.join(s) + (' ...' if len(bits) > n else '')

    if r['px_only']:
        w(f'* **prjxray bits absent from all our features/defaults: '
          f'{len(r["px_only"])}**: '
          + bitlist(r['px_only'], r['px_bits_map']))
    if r['px_outside']:
        w(f'* prjxray bits outside our region of the tile type '
          f'({r["shape"][0]} frames x {r["shape"][1]} bits): '
          f'{len(r["px_outside"])}: '
          + bitlist(r['px_outside'], r['px_bits_map'], 6))
    if r['our_only']:
        w(f'* our bits absent from prjxray segbits: {len(r["our_only"])}'
          + (f' ({len(r["our_only_not_mask"])} also outside the prjxray '
             f'mask)' if r['our_only_not_mask'] is not None else '')
          + ': ' + bitlist(r['our_only'], r['our_bits_map']))
    np_ = len(r['pip_same']) + len(r['pip_diff'])
    if np_ or r['pip_nomatch']:
        w(f'* PIP names: {np_} prjxray PIPs found in ours, '
          f'{len(r["pip_same"])} with identical bits, '
          f'{len(r["pip_diff"])} with different bits ('
          + ', '.join(f'ours {k} {v}' for k, v in
                      sorted(r['pip_diff_kind'].items())) + '); '
          f'{len(r["pip_nomatch"])} prjxray PIP-like features have no '
          f'name match.')
        for n, on in r['pip_diff'][:5]:
            pp = ' '.join(map(fmt_bit, sorted(r['px'][n][0])))
            op = ' '.join(map(fmt_bit, sorted(r['ours'][on][0])))
            w(f'  * differ: `{n}` [{pp}] vs `{on}` [{op}]')
    w(f'* prjxray features (not matched as a PIP by name) with an our '
      f'feature of identical set bits: {len(r["bitmatch"])}; with none: '
      f'{len(r["nomatch"])}.')
    ex = r['pip_same'][:3] + [(n, c[0]) for n, c in r['bitmatch'][:6]]
    for n, on in ex:
        w(f'  * `{ttype}.{n}` = `{ttype}.{on}`')
    if r['nomatch']:
        w('  * unmatched prjxray features (sample): ' +
          ', '.join(f'`{n}`' for n in r['nomatch'][:8]))
    w('')


# ---------------------------------------------------------------------------


def checkout_rev(path):
    """Commit id of a checkout, read from its HEAD file (None if unknown)."""
    d = os.path.join(path, '.git')
    head = os.path.join(d, 'HEAD')
    if not os.path.exists(head):
        return None
    ref = open(head).read().strip()
    if not ref.startswith('ref: '):
        return ref
    ref = ref[5:]
    p = os.path.join(d, ref)
    if os.path.exists(p):
        return open(p).read().strip()
    p = os.path.join(d, 'packed-refs')
    if os.path.exists(p):
        for line in open(p):
            x = line.split()
            if len(x) == 2 and x[1] == ref:
                return x[0]
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--prjxray-db', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--build', default=os.environ.get(
        'URAY_BUILD', os.path.join(URAY_DIR, 'build')))
    ap.add_argument('--db', help='default: <build>/db/Series7')
    ap.add_argument('--devices', help='comma separated prjxray devices')
    ap.add_argument('--arch', default='Series7',
                    help='Series7 (prjxray-db) or UltraScale(Plus) '
                    '(prjuray-db, implies --tilegrid-only)')
    ap.add_argument('--tilegrid-only', action='store_true',
                    help='compare the tile grids only (no bits)')
    args = ap.parse_args()
    os.environ['URAY_BUILD'] = args.build
    sys.path.insert(0, HERE)
    import dies as dieslib
    global ARCH
    ARCH = args.arch
    if ARCH != 'Series7':
        args.tilegrid_only = True
    dbdir = args.db or os.path.join(args.build, 'db', ARCH)

    alldies = dieslib.load()
    devices = []
    for fam in ARCH_FAMILIES[ARCH]:
        for path in sorted(glob.glob(os.path.join(
                args.prjxray_db, fam, '*', 'tilegrid.json'))):
            dev = os.path.basename(os.path.dirname(path))
            if args.devices and dev not in args.devices.split(','):
                continue
            die = None
            for d in alldies.values():
                # prjuray-db names devices by part
                if dev in d.devices or dev in d.parts:
                    die = d.name
            devices.append((fam, dev, die, path))

    out = []
    w = out.append
    w('# Cross-check of the generic Series7 database against prjxray-db\n')
    w(f'* prjxray-db: `{os.path.abspath(args.prjxray_db)}`')
    rev = checkout_rev(args.prjxray_db)
    if rev:
        w(f'* prjxray-db commit: `{rev}`')
    w(f'* our database: `{os.path.abspath(dbdir)}`\n')

    tg_results = []
    summary = []
    tg_sections = []
    for fam, dev, die, path in devices:
        ours_path = os.path.join(dbdir, die or '-', 'tilegrid.json')
        if die is None or not os.path.exists(ours_path):
            summary.append((fam, dev, die, None))
            continue
        print(f'tilegrid {dev} vs {die}', file=sys.stderr)
        with open(path) as f:
            px = json.load(f)
        with open(ours_path) as f:
            ours = json.load(f)
        res = compare_tilegrid(px, ours)
        nwrong = sum(1 for c in res['cols'].values()
                     if any(p != o for p, o in c))
        print(f'summary {dev} {die}: regions {px_regions(res)} identical '
              f'{res["ok"] + res["ok_frames"]} missing '
              f'{sum(res["px_only"].values())} different '
              f'{res["regions"] - res["ok"] - res["ok_frames"]} '
              f'wrong grid columns {nwrong}', file=sys.stderr)
        tg_results.append(res)
        summary.append((fam, dev, die, res))
        sec = []
        report_tilegrid(sec, dev, die, res)
        tg_sections.append(sec)

    w('## Tile grid summary\n')
    w('"regions" are prjxray tile regions (CLB_IO_CLK / BLOCK_RAM); '
      '"identical" includes regions where only our frame count is larger '
      '(prjxray gives INT/HCLK tiles 26-28 minors, ours the whole frame '
      'column); "missing" regions have no region of the same block in our '
      'tile grid.\n')
    w('| family | prjxray device | our die | common tiles | regions | '
      'identical | missing | different | mismatching tile types '
      '(regions) |')
    w('| --- | --- | --- | --- | --- | --- | --- | --- | --- |')
    for fam, dev, die, res in summary:
        if res is None:
            w(f'| {fam} | {dev} | {die or "not a WebPACK die"} | - | - | - '
              f'| - | - | {"no tilegrid for our die" if die else ""} |')
            continue
        nreg = px_regions(res)
        ok = res['ok'] + res['ok_frames']
        miss = sum(res['px_only'].values())
        bad = collections.Counter()
        for (tt, b), c in res['by_type'].items():
            n = nbad(c)
            if n:
                bad[tt + ('/BRAM' if b else '')] += n
        badtxt = ', '.join(f'{t} ({n})' for t, n in bad.most_common(6))
        if len(bad) > 6:
            badtxt += f', +{len(bad) - 6} types'
        w(f'| {fam} | {dev} | {die} | {res["tiles"]} | {nreg} | '
          f'{ok} ({100.0 * ok / max(1, nreg):.1f}%) | {miss} | '
          f'{res["regions"] - ok} | {badtxt} |')
    w('')

    # Tile types mismatching in several dies.
    agg = collections.defaultdict(collections.Counter)
    dies_bad = collections.defaultdict(set)
    for (fam, dev, die, res) in summary:
        if res is None:
            continue
        for key, c in res['by_type'].items():
            agg[key].update(c)
            if nbad(c):
                dies_bad[key].add(die)
    rows = [(k, c) for k, c in agg.items()
            if nbad(c)]
    if rows:
        w('### Mismatching tile types over all dies\n')
        w('| tile type | block | ok | mismatches | dies |')
        w('| --- | --- | --- | --- | --- |')
        for (tt, b), c in sorted(rows, key=lambda x: (
                -nbad(x[1]), x[0])):
            mism = ', '.join(f'{k} {v}' for k, v in sorted(c.items())
                             if k != 'ok')
            w(f'| {tt} | {b} | {c.get("ok", 0)} | {mism} | '
              f'{", ".join(sorted(dies_bad[(tt, b)]))} |')
        w('')

    if args.tilegrid_only:
        w('## Tile grid details\n')
        for sec in tg_sections:
            out.extend(sec)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, 'w') as f:
            f.write('\n'.join(out) + '\n')
        print(f'wrote {args.out}', file=sys.stderr)
        return

    # Bits.
    print('loading prjxray segbits', file=sys.stderr)
    pxfeats, pxfams, conflicts, masks = load_px_segbits(args.prjxray_db)
    bit_sections = []
    bsum = []
    only_px_types = []
    for key in sorted(pxfeats):
        ttype, block = key
        ours, defaults, have = load_ours(dbdir, ttype, block)
        if not have:
            only_px_types.append(key)
            continue
        delta, dstat = pick_delta(tg_results, ttype, block)
        r = compare_bits(pxfeats[key], masks.get(key, set()), ours,
                         defaults, delta,
                         pick_shape(tg_results, ttype, block))
        sec = []
        report_bits(sec, key, pxfams[key], conflicts.get(key, 0), r, delta,
                    dstat)
        bit_sections.append(sec)
        bsum.append((key, r, delta))
    # Tile types prjxray documents as an alias of another type (*_SING,
    # *_BOT_UTURN): compare ours with the aliased type's segbits.
    aliases = {}
    for res in tg_results:
        aliases.update(res['alias'])
    for atype, (base, start) in sorted(aliases.items()):
        if (atype, 0) in pxfeats or (base, 0) not in pxfeats or start:
            continue
        ours, defaults, have = load_ours(dbdir, atype, 0)
        if not have:
            continue
        delta, dstat = pick_delta(tg_results, atype, 0)
        r = compare_bits(pxfeats[(base, 0)], masks.get((base, 0), set()),
                         ours, defaults, delta,
                         pick_shape(tg_results, atype, 0))
        sec = []
        report_bits(sec, (atype, 0), pxfams[(base, 0)],
                    conflicts.get((base, 0), 0), r, delta, dstat)
        sec.insert(1, f'Compared with prjxray alias type {base}.\n')
        bit_sections.append(sec)
        bsum.append(((atype, 0), r, delta))
    px_types = set(k[0] for k in pxfeats) | set(aliases)
    our_types = set()
    for path in glob.glob(os.path.join(dbdir, 'segbits_*.db')):
        base = os.path.basename(path)[len('segbits_'):-len('.db')]
        if base.startswith('opt_'):
            base = base[4:]
        our_types.add(base.split('.')[0].upper())

    w('## Bit summary\n')
    w('prjxray bits are mapped onto our region coordinates (frame, bit from '
      'the region start); "prjxray only" bits are used by some prjxray '
      'feature (set or `!`) but by no feature or default of ours, i.e. '
      'candidate undocumented bits for us; "outside" prjxray bits fall '
      'outside our region of the tile type (a tile grid difference).  '
      '"PIP names" counts prjxray PIPs `<TYPE>.<dst>.<src>` found as our '
      '`<TYPE>.<src>-><dst>` with identical / different set bits; "bit '
      'match" counts the other prjxray features with an our feature of '
      'identical set bits.\n')
    w('| tile type | block | prjxray feats | our feats | prjxray bits | '
      'our bits | common | prjxray only | outside | ours only | PIP names '
      'same/diff | bit match | delta |')
    w('| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- '
      '| --- | --- |')
    for (tt, b), r, delta in sorted(bsum, key=lambda x: -len(x[1]['px_only'])):
        w(f'| {tt} | {b} | {r["px_feats"]} | {r["our_feats"]} | '
          f'{r["px_bits"]} | {r["our_bits"]} | {r["common"]} | '
          f'{len(r["px_only"])} | {len(r["px_outside"])} | '
          f'{len(r["our_only"])} | '
          f'{len(r["pip_same"])}/{len(r["pip_diff"])} | '
          f'{len(r["bitmatch"])} | {delta} |')
    w('')
    if only_px_types:
        w('Tile types with prjxray segbits but no database of ours: ' +
          ', '.join(t + ('/BRAM' if b else '')
                    for t, b in only_px_types) + '\n')
    w('Tile types with a database of ours but no prjxray segbits: ' +
      ', '.join(sorted(our_types - px_types)) + '\n')

    w('## Tile grid details\n')
    for sec in tg_sections:
        out.extend(sec)
    w('## Bit details\n')
    for sec in bit_sections:
        out.extend(sec)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, 'w') as f:
        f.write('\n'.join(out) + '\n')
    print(f'wrote {args.out}', file=sys.stderr)


if __name__ == '__main__':
    main()
