#!/usr/bin/env python3
"""Compare two check.py logs: total unowned and distinct undocumented bits,
and the per tile type undocumented counts that changed most.

    check_cmp.py <before check log> <after check log> [n]
"""
import re
import sys


def load(path):
    per_type = {}
    unowned = distinct = 0
    in_types = False
    with open(path) as f:
        for line in f:
            m = re.match(r'.*: set \d+ unowned (\d+) undocumented \d+ bits (\d+)',
                         line)
            if m:
                unowned += int(m.group(1))
                distinct += int(m.group(2))
            if line.startswith('== undocumented'):
                in_types = True
                continue
            if in_types:
                m = re.match(r'(\S+): (\d+) ', line)
                if m:
                    per_type[m.group(1)] = int(m.group(2))
    return per_type, unowned, distinct


def main():
    a, ua, da = load(sys.argv[1])
    b, ub, db = load(sys.argv[2])
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 25
    print('unowned', ua, '->', ub, 'distinct undocumented', da, '->', db)
    keys = sorted(set(a) | set(b),
                  key=lambda k: -abs(a.get(k, 0) - b.get(k, 0)))
    for k in keys[:n]:
        print(f'{k:28s} {a.get(k, 0):7d} {b.get(k, 0):7d} '
              f'{b.get(k, 0) - a.get(k, 0):+7d}')


if __name__ == '__main__':
    main()
