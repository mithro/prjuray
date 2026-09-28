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
"""
import resource
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

MARGIN = 1.25
FLOOR = 2 << 30


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
    with ProcessPoolExecutor(max(1, min(jobs, len(items)))) as ex:
        futs = {}
        nxt = 0
        while nxt < len(items) or futs:
            while nxt < len(items) and len(futs) < lim:
                futs[ex.submit(_call, (fn, items[nxt]))] = nxt
                nxt += 1
            done, _ = wait(futs, return_when=FIRST_COMPLETED)
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
