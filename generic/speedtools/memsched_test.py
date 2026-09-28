#!/usr/bin/env python3
"""memsched.budget_map with a stale known peak: items grow to ~MB MiB over
~3 s; the known peak says 50 MiB.  Prints the largest total resident memory
of the pool seen by a sampler thread against the budget.

    memsched_test.py [MB per item] [budget GiB] [items] [jobs]
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'python'))
import memsched  # noqa: E402

MB = int(sys.argv[1]) if len(sys.argv) > 1 else 400
BUDGET = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
N = int(sys.argv[3]) if len(sys.argv) > 3 else 24
JOBS = int(sys.argv[4]) if len(sys.argv) > 4 else 16


def work(i):
    blocks = []
    for _ in range(10):
        b = bytearray(MB * 2**20 // 10)
        b[::4096] = b'x' * len(b[::4096])
        blocks.append(b)
        time.sleep(0.3)
    return i


def total_rss():
    me = os.getpid()
    tot = 0
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            with open(f'/proc/{pid}/stat') as f:
                ppid = int(f.read().rsplit(')', 1)[1].split()[1])
            if ppid != me:
                continue
            tot += memsched._proc_mem(int(pid))[1]
        except (OSError, ValueError, IndexError):
            pass
    return tot


mx = [0]
stop = False


def sampler():
    while not stop:
        mx[0] = max(mx[0], total_rss())
        time.sleep(0.1)


t = threading.Thread(target=sampler, daemon=True)
t.start()
t0 = time.time()
items = list(range(N))
n = 0
for i, r in memsched.budget_map(work, items, JOBS, BUDGET, label='test',
                                key=lambda it: 'k', known={'k': int(os.environ.get('KNOWN_MB', 50)) << 20}):
    n += 1
stop = True
t.join()
print(f'{n} items in {time.time() - t0:.1f} s; max pool RSS '
      f'{mx[0] / 2**30:.2f} GiB, budget {BUDGET} GiB')
