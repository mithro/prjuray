#!/usr/bin/env python3
"""phases.py <pipeline log> [mkdb log]: phase summary of a pipeline.py run
(mkdb sample phase, reused tasks, phase 2 end, the slowest mkdb tasks,
checks), from the logs."""
import re
import sys


def main():
    plog = sys.argv[1]
    for line in open(plog):
        if re.match(r'(\[\d\d:\d\d:\d\d\] )?(mkdb|checks|build_db|evidence|'
                    r'colalign|tilegrid|probe|windows|consistency)', line) \
                or line.startswith('vrun:'):
            print(line.rstrip()[:200])
    if len(sys.argv) < 3:
        return
    tasks = []
    last = None
    for line in open(sys.argv[2]):
        if re.match(r'# (samples of|sample phase|\d+ of \d+ tasks reused)',
                    line):
            print(line.rstrip())
        m = re.match(r'# task (\S+) .*?(\d+) s peak ([\d.]+) GiB; elapsed '
                     r'(\d+) s', line)
        if m:
            name = m.group(1)
            if re.fullmatch(r'\d+/\d+', name):
                name = line.split()[3]
            tasks.append((int(m.group(2)), name, float(m.group(3)),
                          int(m.group(4)), line.split(':')[0]))
            last = int(m.group(4))
    if tasks:
        print(f'# phase 2 elapsed {last} s; slowest task steps:')
        for dt, name, peak, el, _ in sorted(tasks, reverse=True)[:8]:
            print(f'#   {name:28s} {dt:5d} s peak {peak:.2f} GiB, done at '
                  f'{el} s')
        print('# last finished:', tasks[-1][4][:120])


if __name__ == '__main__':
    main()
