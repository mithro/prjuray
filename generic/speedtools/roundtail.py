#!/usr/bin/env python3
"""roundtail.py <die> ... --tag <tag> [--jobs N]: round-level turnaround
of a design batch from the designs' run.stats (start / end / wall).

Prints the round's span, when 50 / 90 / 100% of the designs were done,
per die the first start / last end / median wall / failures, the busy
slots over time (concurrently running designs, every 10% of the span)
and the tail: the time from the moment the running count first dropped
below --jobs (slots idle) to the end, with the dies still running then.
"""
import argparse
import glob
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'python'))
import dies as dieslib  # noqa: E402


def hm(t):
    return time.strftime('%H:%M', time.localtime(t))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('dies', nargs='+')
    ap.add_argument('--tag', required=True)
    ap.add_argument('--jobs', type=int, default=None,
                    help='slots of the batch (default: the maximum '
                    'concurrency seen)')
    a = ap.parse_args()
    runs = []
    for d in a.dies:
        for p in glob.glob(os.path.join(dieslib.BUILD, 'designs', d, a.tag,
                                        's*', 'run.stats')):
            try:
                s = json.load(open(p))
            except (OSError, ValueError):
                continue
            if 'end' in s:
                runs.append((s['start'], s['end'], d, s.get('status', '?'),
                             os.path.basename(os.path.dirname(p))))
    if not runs:
        sys.exit('no run.stats')
    t0 = min(r[0] for r in runs)
    t1 = max(r[1] for r in runs)
    ends = sorted(r[1] for r in runs)
    n = len(runs)
    print(f'{n} designs, {hm(t0)} -> {hm(t1)} ({(t1 - t0) / 60:.0f} min); '
          f'50% done {(ends[n // 2] - t0) / 60:.0f} min, 90% '
          f'{(ends[int(n * 0.9) - 1] - t0) / 60:.0f} min, 100% '
          f'{(t1 - t0) / 60:.0f} min')
    # concurrency
    ev = sorted([(r[0], 1) for r in runs] + [(r[1], -1) for r in runs])
    cur = peak = 0
    conc = []
    for t, dlt in ev:
        cur += dlt
        peak = max(peak, cur)
        conc.append((t, cur))
    jobs = a.jobs or peak
    line = []
    for k in range(11):
        t = t0 + (t1 - t0) * k / 10
        c = 0
        for tt, cc in conc:
            if tt > t:
                break
            c = cc
        line.append(str(c))
    print(f'running designs every 10% of the span (slots {jobs}): '
          f'{" ".join(line)}')
    # tail: last time all slots were busy
    full = [t for t, c in conc if c >= jobs]
    tstart = max(full) if full else t0
    idle = sum(max(0, jobs - c) * (tn - t) for (t, c), (tn, _) in
               zip(conc, conc[1:]) if t >= tstart)
    tail_dies = sorted({r[2] for r in runs if r[1] > tstart})
    print(f'tail: {(t1 - tstart) / 60:.0f} min with idle slots '
          f'({idle / 3600:.1f} slot hours idle), dies running then: '
          f'{" ".join(tail_dies)}')
    print(f'{"die":10s} {"n":>3s} {"first start":>11s} {"last end":>8s} '
          f'{"med wall":>8s} {"fails":>5s}')
    for d in a.dies:
        rr = [r for r in runs if r[2] == d]
        if not rr:
            continue
        ok = [r[1] - r[0] for r in rr if r[3] == 'ok']
        med = statistics.median(ok) if ok else 0
        print(f'{d:10s} {len(rr):3d} {hm(min(r[0] for r in rr)):>11s} '
              f'{hm(max(r[1] for r in rr)):>8s} {med:7.0f}s '
              f'{sum(r[3] != "ok" for r in rr):5d}')


if __name__ == '__main__':
    main()
