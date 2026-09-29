#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Timing data of a die, from Vivado's speed models (tcl/dump_timing.tcl).

  timing.py dump <die> [--grades all|fuzz] [--jobs N]
  timing.py json <die>

dump runs Vivado: the full dump (speed models, tile and site timing) for the
die's fuzz part, and the speed model table only for one part of every other
speed grade (grade strings as Vivado lists them, e.g. -1, -1I, -2, -1LV).
json converts the raw dump into

  <out>/<arch>/<die>/tile_timing.npz  (numpy, per tile type T and kind K
      in pips/wires):  T:K:names  pip or wire names (without the tile),
      T:K:tiles  tile names, T:K:variants  [n variants x n names] speed
      indices (-1: the tile lacks it), T:K:tile_variant  variant of each
      tile; model_index / model_name map speed indices to model names.
      7-series has one variant per type; UltraScale(+) pips have instance
      specific models (xcku025: every INT tile differs).
  <out>/<arch>/<die>/site_timing.json
      {site_type: {"pins": {pin: [direction, model]},
                   "bels": {bel: {"type": bel_type, "models": [model...]}}}}
  <out>/<arch>/<die>/speed_models/<n>.json
      {model: {"type": type, "index": speed_index, <property>: value...}}
      one file per distinct model table
  <out>/<arch>/<die>/grades.json
      {grade: {"part": part, "models": "speed_models/<n>.json"}}

where model is the speed model's NAME_INTERNAL (pip and wire speed indices
resolved through the table).  Numeric property values become floats.  The
output root is $URAY_TIMING or build/timing/<Vivado version>.  Vivado comes
from $URAY_VIVADO_SETTINGS (default 2025.2), as in run_designs.py.
"""
import argparse
import collections
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import dies as dieslib

HERE = os.path.dirname(os.path.abspath(__file__))
TCL = os.path.join(os.path.dirname(HERE), 'tcl', 'dump_timing.tcl')
VIVADO_SETTINGS = os.environ.get(
    'URAY_VIVADO_SETTINGS', '/opt/xilinx/Vivado/2025.2/settings64.sh')
VIVADO_VERSION = os.path.basename(os.path.dirname(VIVADO_SETTINGS))


def out_root():
    return os.environ.get('URAY_TIMING', os.path.join(
        dieslib.BUILD, 'timing', VIVADO_VERSION))


def die_dir(die):
    return os.path.join(out_root(), die.arch, die.name)


def part_info(die):
    """part -> (device, package, speed grade) for the die's parts."""
    info = {}
    for line in open(os.path.join(dieslib.META, 'parts.txt')):
        p = line.split()
        if p[0] in die.parts:
            info[p[0]] = (p[3], p[4], p[5])
    return info


def grades_of(die):
    """speed grade -> parts of the die with that grade."""
    grades = collections.defaultdict(list)
    for part, (_, _, grade) in part_info(die).items():
        grades[grade].append(part)
    return grades


def run_vivado(part, outdir, models_only):
    os.makedirs(outdir, exist_ok=True)
    args = f'{part} {outdir}' + (' models' if models_only else '')
    cmd = (f'source {VIVADO_SETTINGS} && exec vivado -mode batch -nojournal '
           f'-log {outdir}/vivado.log -source {TCL} -tclargs {args}')
    with open(os.path.join(outdir, 'run.log'), 'w') as f:
        rc = subprocess.run(['bash', '-c', cmd], cwd=outdir, stdout=f,
                            stderr=subprocess.STDOUT).returncode
    return part, rc


def dump(die, which, jobs):
    raw = os.path.join(die_dir(die), 'raw')
    fuzz = dieslib.fuzz_parts(die)[0]
    grades = grades_of(die)
    todo = [(fuzz, os.path.join(raw, fuzz), False)]
    if which == 'all':
        info = part_info(die)
        dev_pkg = info[fuzz][:2]
        for g, parts in sorted(grades.items()):
            if fuzz in parts:
                continue
            # Prefer the fuzz part's device and package.
            same = [p for p in parts if info[p][:2] == dev_pkg]
            p = sorted(same or parts)[0]
            todo.append((p, os.path.join(raw, p), True))
    with ThreadPoolExecutor(jobs) as ex:
        for part, rc in ex.map(lambda t: run_vivado(*t), todo):
            print(f'{die.name} {part}: rc {rc}')
            if rc:
                sys.exit(f'vivado failed, see {raw}/{part}/run.log')


def num(v):
    try:
        return float(v)
    except ValueError:
        return v


def read_models(path):
    models = {}
    for line in open(path):
        if line.startswith('#'):
            continue
        f = line.rstrip('\n').split('\t')
        m = {'type': f[3], 'index': int(f[2])}
        for kv in f[4:]:
            k, _, v = kv.partition('=')
            m[k] = num(v)
        models[f[1]] = m
    return models


def to_json(die):
    dd = die_dir(die)
    raw = os.path.join(dd, 'raw')
    fuzz = dieslib.fuzz_parts(die)[0]
    grade_of = {p: g for g, ps in grades_of(die).items() for p in ps}

    # Speed model tables, one file per distinct table.
    os.makedirs(os.path.join(dd, 'speed_models'), exist_ok=True)
    tables, grades = {}, {}
    fuzz_models = None
    for part in sorted(os.listdir(raw)):
        path = os.path.join(raw, part, 'speed_models.txt')
        if not os.path.exists(path):
            continue
        models = read_models(path)
        if part == fuzz:
            fuzz_models = models
        text = json.dumps(models, sort_keys=True)
        h = hashlib.sha256(text.encode()).hexdigest()
        if h not in tables:
            name = f'speed_models/{len(tables)}.json'
            tables[h] = name
            with open(os.path.join(dd, name), 'w') as f:
                f.write(text)
        grades[grade_of.get(part, part)] = {'part': part, 'models': tables[h]}
    with open(os.path.join(dd, 'grades.json'), 'w') as f:
        json.dump(grades, f, indent=1, sort_keys=True)

    # Speed index -> model name (indices are unique, 65535 = none).
    by_index = {m['index']: n for n, m in fuzz_models.items()
                if m['index'] != 65535}

    def model(i):
        return by_index.get(int(i))

    # Tile timing: one row of speed indices per tile, packed per tile type
    # and kind into the distinct rows ("variants") and a tile -> variant map.
    arrays = {}
    stats = collections.Counter()

    def flush(key, names, tile_names, rows):
        if not rows:
            return
        m = np.stack(rows)
        variants, inverse = np.unique(m, axis=0, return_inverse=True)
        arrays[f'{key}:names'] = np.array(names)
        arrays[f'{key}:tiles'] = np.array(tile_names)
        arrays[f'{key}:variants'] = variants.astype(np.int32)
        arrays[f'{key}:tile_variant'] = inverse.reshape(-1).astype(np.int32)
        stats['entries'] += m.size
        stats['types_multi'] += len(variants) > 1
        stats['variants'] += len(variants)
        known = np.isin(variants, list(by_index)) | (variants == -1)
        stats['unknown'] += int((~known).sum())

    key = names = None
    tile_names, rows = [], []
    for line in open(os.path.join(raw, fuzz, 'tile_timing.txt')):
        if line[0] == 'R':
            _, t, idx = line.rstrip('\n').split('\t')
            tile_names.append(t)
            rows.append(np.array(idx.split(), dtype=np.int64))
        elif line[0] == 'T':
            ttype = line.rstrip('\n').split('\t')[1]
        elif line[0] == 'N':
            flush(key, names, tile_names, rows)
            f = line.rstrip('\n').split('\t')
            key, names = f'{ttype}:{f[1]}', f[2:]
            tile_names, rows = [], []
    flush(key, names, tile_names, rows)
    idx_items = sorted(by_index.items())
    arrays['model_index'] = np.array([i for i, _ in idx_items], dtype=np.int32)
    arrays['model_name'] = np.array([n for _, n in idx_items])
    np.savez_compressed(os.path.join(dd, 'tile_timing.npz'), **arrays)
    old = os.path.join(dd, 'tile_timing.json')  # earlier format
    if os.path.exists(old):
        os.remove(old)
    tiles = {k.split(':')[0] for k in arrays if k.count(':') == 2}

    sites = {}
    for line in open(os.path.join(raw, fuzz, 'site_timing.txt')):
        f = line.rstrip('\n').split('\t')
        if f[0] == 'S':
            cur = sites.setdefault(f[1], {'pins': {}, 'bels': {}})
        elif f[0] == 'I':
            cur['pins'][f[1]] = [f[2], model(f[3])]
        elif f[0] == 'B':
            cur['bels'][f[1]] = {'type': f[2], 'models': f[3:]}
    with open(os.path.join(dd, 'site_timing.json'), 'w') as f:
        json.dump(sites, f, sort_keys=True)

    print(f'{die.name}: {len(tiles)} tile types ({stats["entries"]} pip/wire '
          f'entries; {stats["types_multi"]} type/kinds with per tile variants, '
          f'{stats["variants"]} variants in all; {stats["unknown"]} indices '
          f'without a model), {len(sites)} site types, {len(grades)} speed '
          f'grades in {len(tables)} distinct tables')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('action', choices=('dump', 'json'))
    ap.add_argument('die')
    ap.add_argument('--grades', choices=('all', 'fuzz'), default='all')
    ap.add_argument('--jobs', type=int, default=4)
    a = ap.parse_args()
    die = dieslib.load()[a.die]
    if a.action == 'dump':
        dump(die, a.grades, a.jobs)
    else:
        to_json(die)


if __name__ == '__main__':
    main()
