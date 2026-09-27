#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Run time and failure cause statistics of fuzzing design directories.

For every root (e.g. build/designs/xa7s15/r4) prints the success rate, the
throughput (successful designs per hour of one job slot and, when
run.stats is present, per CPU core hour), where the time of successful
designs goes, and a histogram of failure causes with the time they wasted.

Usage: runstats.py [--list] <root> [<root> ...]
"""
import argparse
import collections
import glob
import json
import os
import re
import statistics
import time

PHASES = ('start', 'link', 'build', 'place', 'route', 'bitgen', 'dump',
          'post')


def classify(log, rlog):
    """Failure cause of a design from its nl.log / run.log text."""
    if 'giving up: time budget exceeded' in log:
        return 'budget'
    if re.search(r'hs_err_pid|Abnormal program termination|Segmentation',
                 rlog):
        return 'crash'
    if 'timeout' in rlog[-200:]:
        return 'timeout'
    sig = re.findall(
        r'^(place_design failed: .*|route_design failed: .*|'
        r'write_bitstream failed: .*|placed \d+|routed \d+|written \d+|'
        r'dumped \d+|route: nothing to repair|dropping pblocks.*)$', log,
        re.M)
    if not sig:
        return 'no_implementation' if 't_built' not in log else 'build'
    last = sig[-1]
    if last.startswith('place_design failed'):
        if 'could not place all instances' in last:
            return 'place: unplaceable, no culprit'
        if 'ZHOLD_DELAY' in last:
            return 'place: ZHOLD_DELAY'
        if 'missing a connection' in last:
            return 'place: LUT missing input'
        return 'place: other, no culprit'
    if last.startswith('route_design failed'):
        return 'route: no culprit'
    if last.startswith('write_bitstream failed'):
        return 'bitgen: no culprit'
    if last.startswith('placed'):
        return 'killed while routing'
    if last.startswith('routed'):
        return 'killed in bitgen'
    if last.startswith('written'):
        return 'killed in dump'
    if last.startswith('dumped'):
        return 'post-processing'
    return 'other: ' + re.sub(r'\d+', 'N', last)[:60]


def phases(log):
    """Seconds per phase of a successful design (None when unknown)."""
    st = {k: int(v) / 1000 for k, v in re.findall(
        r'^t_(start|linked|built) (\d+)$', log, re.M)}
    rel = collections.OrderedDict()
    for k, v in re.findall(r'^(placed|routed|written|dumped|done) (\d+)$',
                           log, re.M):
        rel[k] = int(v)  # last occurrence wins
    if 'built' not in st or 'done' not in rel:
        return None
    out = {}
    out['link'] = st['linked'] - st['start']
    out['build'] = st['built'] - st['linked']
    out['place'] = rel.get('placed', 0)
    out['route'] = rel.get('routed', out['place']) - out['place']
    out['bitgen'] = rel.get('written', 0) - rel.get('routed', 0)
    out['dump'] = rel.get('dumped', rel['done']) - rel.get('written', 0)
    return out


def design_info(d):
    log = rlog = ''
    try:
        log = open(os.path.join(d, 'nl.log'), errors='replace').read()
    except OSError:
        pass
    for f in ('run.log', 'vivado.log'):
        try:
            rlog += open(os.path.join(d, f), errors='replace').read()[-20000:]
        except OSError:
            pass
    if glob.glob(os.path.join(d, 'hs_err_pid*.log')):
        rlog += ' hs_err_pid'
    stats = {}
    try:
        stats = json.load(open(os.path.join(d, 'run.stats')))
    except (OSError, ValueError):
        pass
    files = [os.path.join(d, f) for f in os.listdir(d)]
    t0 = stats.get('start')
    if t0 is None:
        try:
            t0 = os.stat(os.path.join(d, 'design.tcl')).st_mtime
        except OSError:
            t0 = min(os.stat(f).st_mtime for f in files) if files else 0
    t1 = stats.get('end') or max(
        (os.stat(f).st_mtime for f in files), default=t0)
    ok = os.path.exists(os.path.join(d, 'bits.npz'))
    return {
        'ok': ok,
        'wall': stats.get('wall', t1 - t0),
        'cpu': stats.get('cpu'),
        'rss': stats.get('maxrss'),
        'cause': None if ok else stats.get('status_cause') or
        classify(log, rlog),
        'phases': phases(log) if ok else None,
        'recent': time.time() - t1 < 600 and not stats,
    }


def report(root, show_list):
    dirs = sorted(glob.glob(os.path.join(root, 's*')))
    infos = []
    running = 0
    for d in dirs:
        if not os.path.isdir(d):
            continue
        i = design_info(d)
        if i['recent'] and not i['ok']:
            running += 1
            continue
        i['dir'] = d
        infos.append(i)
    if not infos:
        return
    ok = [i for i in infos if i['ok']]
    fail = [i for i in infos if not i['ok']]
    wall = sum(i['wall'] for i in infos)
    cpus = [i['cpu'] for i in infos if i['cpu'] is not None]
    print(f'== {root}: {len(infos)} designs ({running} running), '
          f'{len(ok)} ok ({100 * len(ok) / len(infos):.0f}%), '
          f'{wall / 3600:.1f} slot hours')
    line = f'   ok per slot hour {len(ok) / max(wall, 1) * 3600:.2f}'
    if len(cpus) == len(infos):
        line += (f', ok per core hour {len(ok) / max(sum(cpus), 1) * 3600:.2f}'
                 f' (cpu {sum(cpus) / 3600:.1f} h, '
                 f'{sum(cpus) / max(wall, 1):.2f} cores/job)')
    print(line)
    if ok:
        print(f'   ok wall median {statistics.median(i["wall"] for i in ok):.0f} s'
              f', failed wall median '
              f'{statistics.median(i["wall"] for i in fail) if fail else 0:.0f} s,'
              f' {sum(i["wall"] for i in fail) / max(wall, 1) * 100:.0f}% of '
              f'the time spent on failures')
    rss = sorted(i['rss'] for i in infos if i.get('rss'))
    if rss:
        print(f'   peak memory per design: median {rss[len(rss) // 2] / 2**30:.1f}'
              f' GiB, max {rss[-1] / 2**30:.1f} GiB')
    ph = [i['phases'] for i in ok if i['phases']]
    if ph:
        tot = {k: sum(p[k] for p in ph) for k in ph[0]}
        s = sum(tot.values())
        print('   ok time split: ' + ', '.join(
            f'{k} {v / len(ph):.0f}s ({100 * v / s:.0f}%)'
            for k, v in tot.items()))
    causes = collections.Counter(i['cause'] for i in fail)
    cwall = collections.Counter()
    for i in fail:
        cwall[i['cause']] += i['wall']
    for c, n in causes.most_common():
        print(f'   {n:5d} {c:32s} {cwall[c] / 3600:6.1f} h '
              f'(median {statistics.median(i["wall"] for i in fail if i["cause"] == c):.0f} s)')
        if show_list:
            print('         ' + ' '.join(
                os.path.basename(i['dir']) for i in fail if i['cause'] == c))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--list', action='store_true',
                    help='list the designs of each failure cause')
    ap.add_argument('roots', nargs='+')
    args = ap.parse_args()
    for r in args.roots:
        report(r, args.list)


if __name__ == '__main__':
    main()
