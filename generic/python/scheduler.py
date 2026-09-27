#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""File based work queue for fuzzing designs.

Work items are small JSON files in a queue directory, so any number of
runner processes, on this or other machines sharing the file system, can
pull work, and runners can be added or stopped at any time:

  <queue>/todo/<prio>_<die>_<tag>_s<seed>.json    waiting
  <queue>/running/<item>.<host>.<pid>             claimed (atomic rename)
  <queue>/done/<item>                             finished, with its status

  scheduler.py submit --queue Q --die xa7s15,xazu1eg --tag r5 --seeds 1:500 \
      [--prio 50] [--workdir DIR] [-- gen_design arguments]
  scheduler.py work --queue Q --jobs 16 [--reuse 8] [--threads 1] [--wait]
  scheduler.py status --queue Q
  scheduler.py requeue --queue Q        (stale claims of dead local runners)

Each design is run exactly as run_designs.py does (same outputs).  A runner
thread prefers items of the die its Vivado process has loaded.
"""
import argparse
import collections
import json
import os
import socket
import sys
import threading
import time

import dies as dieslib
import run_designs as rd

HOST = socket.gethostname()


def qdirs(queue):
    d = {k: os.path.join(queue, k) for k in ('todo', 'running', 'done')}
    for p in d.values():
        os.makedirs(p, exist_ok=True)
    return d


def submit(args):
    q = qdirs(args.queue)
    first, last = map(int, args.seeds.split(':'))
    n = 0
    for s in range(first, last + 1):
        for die in args.die.split(','):
            name = f'{args.prio:03d}_{die}_{args.tag}_s{s}.json'
            item = {'die': die, 'tag': args.tag, 'seed': s,
                    'gen_args': args.gen_args, 'workdir': args.workdir,
                    'timeout': args.timeout}
            if any(os.path.exists(os.path.join(q[k], name))
                   for k in ('todo', 'done')):
                continue
            tmp = os.path.join(q['todo'], f'.{name}.{HOST}.{os.getpid()}')
            with open(tmp, 'w') as f:
                json.dump(item, f)
            os.rename(tmp, os.path.join(q['todo'], name))
            n += 1
    print(f'submitted {n} items')


class Claimer:
    def __init__(self, q):
        self.q = q
        self.lock = threading.Lock()

    def claim(self, die_pref):
        """Claims a todo item (preferring die_pref); returns (item, path)."""
        with self.lock:
            names = sorted(n for n in os.listdir(self.q['todo'])
                           if n.endswith('.json'))
            if die_pref:
                pref = [n for n in names if f'_{die_pref}_' in n]
                # Keep the priority order: only prefer within the best
                # priority class.
                if pref and pref[0][:3] == names[0][:3]:
                    names = pref + names
            for n in names:
                dst = os.path.join(self.q['running'],
                                   f'{n}.{HOST}.{os.getpid()}')
                try:
                    os.rename(os.path.join(self.q['todo'], n), dst)
                except OSError:
                    continue  # taken by another runner
                try:
                    with open(dst) as f:
                        item = json.load(f)
                except (OSError, ValueError):
                    continue
                item['_name'] = n
                return item, dst
        return None, None


def work(args):
    q = qdirs(args.queue)
    alldies = dieslib.load()
    pool = None
    if args.reuse > 1:
        logdir = os.path.join(args.queue, 'workers')
        os.makedirs(logdir, exist_ok=True)
        pool = rd.WorkerPool(args.reuse, args.threads, logdir)
    claimer = Claimer(q)
    counts = collections.Counter()
    lock = threading.Lock()
    t0 = time.time()
    stop = os.path.join(args.queue, 'STOP')

    def loop():
        last = None
        while not os.path.exists(stop):
            item, path = claimer.claim(last)
            if item is None:
                if not args.wait:
                    return
                time.sleep(30)
                continue
            die = alldies[item['die']]
            last = item['die']
            workdir = item.get('workdir') or os.path.join(dieslib.BUILD,
                                                          'designs')
            wdir = os.path.join(workdir, die.name, item['tag'],
                                f's{item["seed"]}')
            try:
                _, status = rd.run_one(die, item['seed'], wdir,
                                       item.get('gen_args', []),
                                       item.get('timeout', args.timeout),
                                       args.threads, pool)
            except Exception as e:  # keep going
                status = f'exception {e}'
            item['status'] = status
            item['host'] = HOST
            item['end'] = time.time()
            with open(path, 'w') as f:
                json.dump(item, f)
            os.rename(path, os.path.join(q['done'], item['_name']))
            with lock:
                counts[status.split()[0]] += 1
                n = sum(counts.values())
                todo = len(os.listdir(q['todo']))
                el = time.time() - t0
                eta = el / n * todo
                print(f'[{time.strftime("%H:%M:%S")}] {n} done, {todo} todo '
                      f'{die.name} {item["tag"]} s{item["seed"]} {status} '
                      f'elapsed {el:.0f}s eta(this runner) {eta:.0f}s '
                      f'{dict(counts)}', flush=True)

    threads = [threading.Thread(target=loop) for _ in range(args.jobs)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        if pool:
            pool.close()


def status(args):
    q = qdirs(args.queue)
    by = collections.defaultdict(collections.Counter)
    for state in ('todo', 'running'):
        for n in os.listdir(q[state]):
            if n.startswith('.'):
                continue
            key = '_'.join(n.split('.')[0].split('_')[1:-1])
            by[key][state] += 1
    for n in os.listdir(q['done']):
        key = '_'.join(n.split('.')[0].split('_')[1:-1])
        try:
            st = json.load(open(os.path.join(q['done'], n)))['status']
        except (OSError, ValueError, KeyError):
            st = '?'
        by[key]['done:' + st.split()[0]] += 1
    for k in sorted(by):
        print(f'{k:30s} {dict(by[k])}')


def requeue(args):
    q = qdirs(args.queue)
    n = 0
    for name in os.listdir(q['running']):
        item, host, pid = name.rsplit('.', 2)
        if host != HOST:
            continue
        if os.path.exists(f'/proc/{pid}') and not args.force:
            continue
        os.rename(os.path.join(q['running'], name),
                  os.path.join(q['todo'], item))
        n += 1
    print(f'requeued {n} items')


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split('\n')[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split('\n', 2)[2])
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('submit')
    s.add_argument('--queue', required=True)
    s.add_argument('--die', required=True)
    s.add_argument('--tag', required=True)
    s.add_argument('--seeds', required=True, help='first:last')
    s.add_argument('--prio', type=int, default=50,
                   help='lower runs first (0-999)')
    s.add_argument('--workdir', default=None)
    s.add_argument('--timeout', type=int, default=None)
    s.add_argument('gen_args', nargs='*')
    w = sub.add_parser('work')
    w.add_argument('--queue', required=True)
    w.add_argument('--jobs', type=int, default=16)
    w.add_argument('--reuse', type=int, default=1)
    w.add_argument('--threads', type=int, default=2)
    w.add_argument('--directive', default=None,
                   help='place_design/route_design directive (e.g. Quick)')
    w.add_argument('--timeout', type=int, default=None)
    w.add_argument('--wait', action='store_true',
                   help='keep polling for new work (stop: touch <queue>/STOP)')
    st = sub.add_parser('status')
    st.add_argument('--queue', required=True)
    r = sub.add_parser('requeue')
    r.add_argument('--queue', required=True)
    r.add_argument('--force', action='store_true',
                   help='also items of live runners of this host')
    args = ap.parse_args()
    rd.install_cleanup()
    if args.cmd == 'work':
        rd.set_directive(args.directive)
    {'submit': submit, 'work': work, 'status': status,
     'requeue': requeue}[args.cmd](args)


if __name__ == '__main__':
    main()
