#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Summarise fuzzing design outcomes: success rate, surviving primitives and
primitives removed by the repair loop."""
import argparse
import collections
import glob
import os
import re


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('roots', nargs='+')
    args = ap.parse_args()
    ok = fail = 0
    final = collections.Counter()
    created = collections.Counter()
    removed = collections.Counter()
    for root in args.roots:
        for d in sorted(glob.glob(os.path.join(root, 's*'))):
            log = os.path.join(d, 'nl.log')
            tcl = os.path.join(d, 'design.tcl')
            if not os.path.exists(log) or not os.path.exists(tcl):
                continue
            ref_of = {}
            for line in open(tcl):
                if line.startswith('nl_cell ') or line.startswith('nl_iob '):
                    p = line.split()
                    ref = p[2] if p[0] == 'nl_cell' else p[4]
                    ref_of[p[1]] = ref
                    created[ref] += 1
            if os.path.exists(os.path.join(d, 'bits.npz')):
                ok += 1
            else:
                fail += 1
            for line in open(log):
                if line.startswith('final '):
                    p = line.split()[1:]
                    for k, v in zip(p[0::2], p[1::2]):
                        final[k] += int(v)
                elif line.startswith('removing '):
                    for n in re.findall(r'\b((?:c|io)\d+)\b', line):
                        removed[ref_of.get(n, '?')] += 1
    print(f'designs ok {ok} failed {fail}')
    print(f'{"primitive":24s} {"created":>8s} {"removed":>8s} {"final":>8s}')
    for ref in sorted(set(created) | set(final)):
        print(f'{ref:24s} {created[ref]:8d} {removed[ref]:8d} '
              f'{final[ref]:8d}')


if __name__ == '__main__':
    main()
