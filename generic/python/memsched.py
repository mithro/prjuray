# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Process pool map bounded by a memory budget.

budget_map(fn, items, jobs, budget_gib) runs fn(item) in up to `jobs`
processes and yields (index, result) as they complete.  With a budget,
the number of items running at once is budget / (MARGIN x the largest
worker peak reported so far, at least `floor`); until the first result
only one item runs (no measurement yet).  The pool starts a process only
when no idle one is left, so this also bounds the processes.  Workers
report their peak through ru_maxrss (fn runs inside _call).

key(item) and known {key: peak bytes} (e.g. saved from an earlier run,
see peaks): when every item's key has a known peak, their maximum is the
starting estimate instead of the floor and the one-at-a-time start is
skipped.  peaks (a dict, filled in): key -> largest peak reported by a
worker after an item of that key (a worker's peak includes the items it
ran before: an overestimate, never an underestimate); record(result):
only results for which it is true count (e.g. real work, not cache
hits).

The estimate corrects itself while items run: every POLL seconds the
pool's worker processes are read from /proc (VmHWM, their peak so far, and
VmRSS): a worker above the estimate raises it at once (a known peak from
an earlier run can be stale, e.g. after new features made the samples
larger: rebuild_all6 US, known 2.03 GiB, OOM at 30 GiB with a 22 GiB
budget).  A new item is also only started while the workers' current
resident memory plus one estimated peak fits the budget.  If the workers
still exceed the budget (items growing faster than the estimate caught
up), the pool is killed and its running items start again with the
corrected estimate: fn must be restartable (mkdb's sample cache writes a
temporary file and renames it; tilegrid's design activity is pure).
"""
import os
import resource
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

MARGIN = 1.25
FLOOR = 2 << 30
POLL = 1.0


def _proc_mem(pid):
    """(VmHWM, VmRSS) bytes of a process, (0, 0) when gone."""
    hwm = rss = 0
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('VmHWM:'):
                    hwm = int(line.split()[1]) * 1024
                elif line.startswith('VmRSS:'):
                    rss = int(line.split()[1]) * 1024
    except OSError:
        pass
    return hwm, rss


def _workers_mem(ex):
    """(largest VmHWM, total VmRSS) over the pool's worker processes."""
    hwm = rss = 0
    for pid in list(getattr(ex, '_processes', None) or ()):
        h, r = _proc_mem(pid)
        hwm = max(hwm, h)
        rss += r
    return hwm, rss


def _call(args):
    fn, item = args
    r = fn(item)
    return r, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def budget_map(fn, items, jobs, budget_gib=None, floor=FLOOR, label='',
               log=print, key=None, known=None, peaks=None, record=None):
    peak = floor
    measured = False
    if key is not None and known and items and \
            all(key(it) in known for it in items):
        peak = max(known[key(it)] for it in items)
        measured = True

    def limit():
        if not budget_gib:
            return jobs
        if not measured:
            return 1
        return max(1, min(jobs, int(budget_gib * 2**30 // (peak * MARGIN))))

    lim = limit()
    if budget_gib and measured:
        log(f'# {label}: {budget_gib} GiB budget, {lim} at once (known '
            f'worker peak {peak / 2**30:.2f} GiB)')
    elif budget_gib:
        log(f'# {label}: {budget_gib} GiB budget, one at a time until a '
            'worker peak is measured')
    budget = (budget_gib or 0) * 2**30
    todo = list(range(len(items)))  # item indices not yet started, in order
    todo.reverse()
    ex = None
    try:
        while todo or ex is not None:
            if ex is None:
                ex = ProcessPoolExecutor(max(1, min(jobs, len(items))))
                futs = {}
            rss = 0
            if budget:
                hwm, rss = _workers_mem(ex)
                if hwm > peak:
                    peak = hwm
                    measured = True
                    new = limit()
                    if new != lim:
                        log(f'# {label}: running worker at '
                            f'{hwm / 2**30:.2f} GiB: {new} at once')
                        lim = new
                if rss > budget and len(futs) > 1:
                    # Over the budget (the estimate was too low): stop the
                    # pool and start its items again with the corrected
                    # estimate (items must be restartable).
                    peak = max(peak, rss // len(futs))
                    lim = limit()
                    log(f'# {label}: workers at {rss / 2**30:.2f} GiB over '
                        f'the budget: restarting {len(futs)} items, '
                        f'{lim} at once')
                    for pid in list(getattr(ex, '_processes', None) or ()):
                        try:
                            os.kill(pid, 9)
                        except OSError:
                            pass
                    ex.shutdown(wait=True, cancel_futures=True)
                    todo.extend(sorted(futs.values(), reverse=True))
                    ex = None
                    continue
            while todo and len(futs) < lim and \
                    (not budget or not futs or
                     rss + peak * MARGIN <= budget):
                i = todo.pop()
                futs[ex.submit(_call, (fn, items[i]))] = i
                rss += peak * MARGIN
            if not futs:
                ex.shutdown(wait=True)
                ex = None
                continue
            done, _ = wait(futs, timeout=POLL if budget else None,
                           return_when=FIRST_COMPLETED)
            for f in done:
                i = futs.pop(f)
                r, p = f.result()
                if peaks is not None and key is not None and \
                        (record is None or record(r)):
                    k = key(items[i])
                    peaks[k] = max(peaks.get(k, 0), p)
                if budget_gib and (not measured or p > peak):
                    measured = True
                    peak = max(peak, p)
                    new = limit()
                    if new != lim:
                        log(f'# {label}: worker peak {p / 2**30:.2f} GiB: '
                            f'{new} at once')
                        lim = new
                yield i, r
    finally:
        if ex is not None:
            ex.shutdown(wait=True, cancel_futures=True)
