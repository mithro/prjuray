#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Summary of a CI run (generic/ci.sh) and comparison with a golden one.

  ci_summary.py summarize <db dir> <check log> <evalpred log> > summary.txt
  ci_summary.py compare <golden summary> <summary> [n]

A summary is "key value" lines: the database (files, feature lines, set /
clear / default bits and a content hash of the segbits / defaults files),
check.py totals (unowned, undocumented per owner, distinct undocumented
bits, per tile type undocumented) and evalpred.py totals and per tile type
missed / false bits.  compare prints the changed values (the content hash
first: an identical hash means an identical database) and exits 1 when any
value differs.
"""
import glob
import hashlib
import os
import re
import sys


def db_stats(dbdir):
    h = hashlib.sha256()
    files = feats = sets = clears = defaults = 0
    for p in sorted(glob.glob(os.path.join(dbdir, 'segbits_*.db')) +
                    glob.glob(os.path.join(dbdir, 'defaults_*.db'))):
        name = os.path.basename(p)
        with open(p, 'rb') as f:
            data = f.read()
        h.update(name.encode() + b'\0' + data + b'\0')
        files += 1
        for line in data.decode().splitlines():
            p_ = line.split()
            if not p_:
                continue
            if name.startswith('segbits_'):
                feats += 1
                for b in p_[1:]:
                    if b.startswith('!'):
                        clears += 1
                    else:
                        sets += 1
            else:
                defaults += 1
    return {'db.hash': h.hexdigest()[:16], 'db.files': files,
            'db.features': feats, 'db.set_bits': sets,
            'db.clear_bits': clears, 'db.default_bits': defaults}


def check_stats(path):
    out = {'check.designs': 0, 'check.set': 0, 'check.unowned': 0,
           'check.undocumented': 0, 'check.distinct': 0,
           'check.baseline_tileless': 0}
    in_types = False
    for line in open(path):
        m = re.match(r'.*: set (\d+) unowned (\d+) undocumented (\d+) '
                     r'bits (\d+) baseline_in_tileless_rows (\d+)', line)
        if m:
            out['check.designs'] += 1
            for k, v in zip(('set', 'unowned', 'undocumented', 'distinct',
                             'baseline_tileless'), m.groups()):
                out[f'check.{k}'] += int(v)
        if line.startswith('== undocumented'):
            in_types = True
            continue
        if in_types:
            m = re.match(r'(\S+): (\d+) \((\d+) distinct\)', line)
            if m:
                out[f'check.type.{m.group(1)}'] = int(m.group(2))
    return out


def evalpred_stats(path):
    out = {}
    for line in open(path):
        m = re.match(r'TOTAL set (\d+) correct (\d+) missed (\d+) false '
                     r'(\d+)', line)
        if m:
            for k, v in zip(('set', 'correct', 'missed', 'false'),
                            m.groups()):
                out[f'pred.{k}'] = int(v)
            continue
        m = re.match(r'\s+(\S+)\s+set\s+(\d+) missed\s+(\d+) false\s+(\d+)',
                     line)
        if m:
            out[f'pred.type.{m.group(1)}.missed'] = int(m.group(3))
            out[f'pred.type.{m.group(1)}.false'] = int(m.group(4))
    return out


def load(path):
    out = {}
    for line in open(path):
        p = line.split()
        if len(p) == 2 and not line.startswith('#'):
            out[p[0]] = p[1]
    return out


def main():
    cmd = sys.argv[1]
    if cmd == 'summarize':
        s = {}
        s.update(db_stats(sys.argv[2]))
        s.update(check_stats(sys.argv[3]))
        s.update(evalpred_stats(sys.argv[4]))
        for k, v in s.items():
            print(k, v)
        return 0
    a, b = load(sys.argv[2]), load(sys.argv[3])
    n = int(sys.argv[4]) if len(sys.argv) > 4 else 40
    head = [k for k in a if not k.startswith(('check.type.', 'pred.type.'))]
    head += [k for k in b if k not in a and k not in head and
             not k.startswith(('check.type.', 'pred.type.'))]
    diff = 0
    for k in head:
        va, vb = a.get(k, '-'), b.get(k, '-')
        mark = '' if va == vb else '   <== changed'
        diff += va != vb
        if va.lstrip('-').isdigit() and vb.lstrip('-').isdigit() and va != vb:
            mark = f'   {int(vb) - int(va):+d}'
        print(f'{k:28s} {va:>18s} {vb:>18s}{mark}')
    keys = sorted({k for k in list(a) + list(b)
                   if k.startswith(('check.type.', 'pred.type.'))
                   and a.get(k, '0') != b.get(k, '0')},
                  key=lambda k: -abs(int(b.get(k, 0)) - int(a.get(k, 0))))
    diff += len(keys)
    if keys:
        print(f'per tile type changes ({len(keys)}, largest first):')
    for k in keys[:n]:
        va, vb = int(a.get(k, 0)), int(b.get(k, 0))
        print(f'  {k:40s} {va:9d} {vb:9d} {vb - va:+9d}')
    print('CI: identical to golden' if not diff else
          f'CI: {diff} values differ from golden')
    return 1 if diff else 0


if __name__ == '__main__':
    sys.exit(main())
