#!/usr/bin/env python3
"""scopemem.py [pattern] [--every S] [--log FILE]: memory of the running
vivado.slice scopes split into anon (process memory, what an OOM kill is
about) and file (page cache, reclaimable) - memory.peak includes the page
cache, so a scope at its cap may be fine.  --every S repeats every S
seconds (appending to --log), keeping per scope the largest anon seen."""
import glob
import os
import sys
import time

SLICE = '/sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/' \
    'vivado.slice'


def stat(cg):
    out = {}
    try:
        for line in open(os.path.join(cg, 'memory.stat')):
            k, v = line.split()
            out[k] = int(v)
        out['current'] = int(open(os.path.join(cg, 'memory.current')).read())
        out['max'] = open(os.path.join(cg, 'memory.max')).read().strip()
    except (OSError, ValueError):
        return None
    return out


def main():
    args = sys.argv[1:]
    every = log = None
    pat = ''
    while args:
        a = args.pop(0)
        if a == '--every':
            every = float(args.pop(0))
        elif a == '--log':
            log = args.pop(0)
        else:
            pat = a
    maxanon = {}
    while True:
        lines = []
        for cg in sorted(glob.glob(os.path.join(SLICE, '*.scope'))):
            name = os.path.basename(cg)
            if pat not in name:
                continue
            s = stat(cg)
            if not s:
                continue
            maxanon[name] = max(maxanon.get(name, 0), s.get('anon', 0))
            cap = s['max']
            cap = f'{int(cap) >> 30}G' if cap.isdigit() else cap
            lines.append(f'{time.strftime("%H:%M:%S")} {name} cur '
                         f'{s["current"] >> 20}M anon {s.get("anon", 0) >> 20}M '
                         f'(max seen {maxanon[name] >> 20}M) file '
                         f'{s.get("file", 0) >> 20}M cap {cap}')
        text = '\n'.join(lines)
        if log:
            with open(log, 'a') as f:
                f.write(text + '\n')
        else:
            print(text, flush=True)
        if not every:
            break
        time.sleep(every)


if __name__ == '__main__':
    main()
