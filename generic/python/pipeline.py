#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Tile grids + bit database + check for a set of dies of one architecture."""
import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import dies as dieslib

HERE = os.path.dirname(os.path.abspath(__file__))


def run(cmd, log):
    with open(log, 'w') as f:
        r = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT)
    return r.returncode


def evidence(args):
    """Design activity evidence of a die (and its activity only tile grid,
    for comparison)."""
    die, arch, tags = args
    out = os.path.join(dieslib.DB, arch, die)
    os.makedirs(out, exist_ok=True)
    roots = ','.join(
        os.path.join(dieslib.BUILD, 'designs', die, t) for t in tags)
    return die, run([
        sys.executable,
        os.path.join(HERE, 'tilegrid.py'), '--die', die, '--designs', roots,
        '--evidence',
        os.path.join(out, 'evidence.json'), '--out',
        os.path.join(out, 'tilegrid_activity.json')
    ], os.path.join(out, 'evidence.log'))


def tilegrid(args):
    """Tile grid from the evidence and the structural frame column
    alignment."""
    die, arch = args
    out = os.path.join(dieslib.DB, arch, die)
    win = os.path.join(dieslib.DB, arch, 'windows.json')
    frm = os.path.join(dieslib.DB, arch, 'frames.json')
    return die, run([
        sys.executable,
        os.path.join(HERE, 'tilegrid.py'), '--die', die, '--evidence',
        os.path.join(out, 'evidence.json'), '--colmap',
        os.path.join(out, 'colmap.json'), '--out',
        os.path.join(out, 'tilegrid.json')
    ] + (['--windows', win] if os.path.exists(win) else []) + (
        ['--frames', frm] if os.path.exists(frm) else []),
        os.path.join(out, 'tilegrid.log'))


def probe(args):
    """Tile type windows of a die: a tile grid giving the doubtful tile
    types wide windows, a bit database of these types built with it (in
    <BUILD>/probe/<die>), and the rows their features use."""
    die, arch, tags = args
    root = os.path.join(dieslib.BUILD, 'probe', die)
    out = os.path.join(root, arch, die)
    os.makedirs(out, exist_ok=True)
    src = os.path.join(dieslib.DB, arch, die)
    rc = run([
        sys.executable,
        os.path.join(HERE, 'tilegrid.py'), '--die', die, '--evidence',
        os.path.join(src, 'evidence.json'), '--colmap',
        os.path.join(src, 'colmap.json'), '--probe', 'auto', '--out',
        os.path.join(out, 'tilegrid.json')
    ], os.path.join(out, 'tilegrid.log'))
    if rc:
        return die, rc
    types = []
    for line in open(os.path.join(out, 'tilegrid.log')):
        if line.startswith('probe '):
            types = line.split()[1:]
    if not types:
        return die, 0
    env = dict(os.environ, URAY_DB=root, PYTHONHASHSEED='0')
    with open(os.path.join(out, 'mkdb.log'), 'w') as f:
        rc = subprocess.run([
            sys.executable,
            os.path.join(HERE, 'mkdb.py'), '--arch', arch, '--dies', die,
            '--tag', ','.join(tags), '--types', ','.join(types), '--jobs',
            '16'
        ], stdout=f, stderr=subprocess.STDOUT, env=env).returncode
    if rc:
        return die, rc
    return die, run([
        sys.executable,
        os.path.join(HERE, 'windows.py'), '--die', die, '--probe-db',
        os.path.join(root, arch), '--probe-types', ','.join(types),
        '--out', os.path.join(out, 'windows.json')
    ], os.path.join(out, 'windows.log'))


def consistency(dlist, arch, tags, logdir):
    """Checks the designs of every die against the existing bit database
    (consistency.py, one die at a time) and writes the suspects to
    <logdir>/suspects_<arch>.txt.  Returns that path, or None when there is
    no database (or no suspect)."""
    dbdir = os.path.join(dieslib.DB, arch)
    if not any(f.startswith('segbits_') for f in os.listdir(dbdir)):
        print('consistency: no bit database yet', flush=True)
        return None
    suspects = []
    for d in dlist:
        roots = ','.join(
            os.path.join(dieslib.BUILD, 'designs', d, t) for t in tags)
        log = os.path.join(logdir, f'consistency_{d}.log')
        rc = run([
            sys.executable,
            os.path.join(HERE, 'consistency.py'), '--die', d, '--designs',
            roots
        ], log)
        found = [line.split(None, 1)[1].strip() for line in open(log)
                 if line.startswith('SUSPECT ')]
        print(f'consistency {d} rc {rc} suspects {len(found)}', flush=True)
        suspects += found
    if not suspects:
        return None
    path = os.path.join(logdir, f'suspects_{arch}.txt')
    with open(path, 'w') as f:
        f.write('\n'.join(suspects) + '\n')
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dies', required=True)
    ap.add_argument('--tags', required=True)
    ap.add_argument('--check-tags', default=None)
    ap.add_argument('--skip-tilegrid', action='store_true')
    ap.add_argument('--probe-windows', action='store_true',
                    help='learn the tile type windows of hard blocks (a '
                    'probe bit database per die) into <db>/<arch>/'
                    'windows.json before building the tile grids')
    ap.add_argument('--consistency', action='store_true',
                    help='when a bit database of the architecture exists, '
                    'check the designs against it (consistency.py) and leave '
                    'the suspects out of the new database')
    ap.add_argument('--jobs', type=int, default=24)
    ap.add_argument('--sample-jobs', type=int, default=None,
                    help='mkdb per design sample cache workers (mkdb '
                    '--sample-jobs; default --jobs).  The phase is light '
                    'per worker (Series7 ~0.3 GB, xcku025 ~2.4 GB)')
    args = ap.parse_args()
    alldies = dieslib.load()
    dlist = args.dies.split(',')
    arch = alldies[dlist[0]].arch
    assert all(alldies[d].arch == arch for d in dlist)
    tags = args.tags.split(',')
    logdir = os.path.join(dieslib.BUILD, 'logs')
    if not args.skip_tilegrid:
        with ProcessPoolExecutor(min(len(dlist), 8)) as ex:
            for die, rc in ex.map(evidence, [(d, arch, tags) for d in dlist]):
                print('evidence', die, 'rc', rc, flush=True)
        # Frame column alignment over every die of the architecture with
        # evidence (the kind tables are shared).
        rc = run([
            sys.executable,
            os.path.join(HERE, 'colalign.py'), '--arch', arch, '--exp',
            dieslib.DB, '--verbose'
        ] + (['--supported'] if arch != 'Series7' else []),
            os.path.join(logdir, f'colalign_{arch}.log'))
        print('colalign rc', rc, flush=True)
        if args.probe_windows:
            with ProcessPoolExecutor(min(len(dlist), 4)) as ex:
                for die, rc in ex.map(probe, [(d, arch, tags)
                                              for d in dlist]):
                    print('probe', die, 'rc', rc, flush=True)
            files = [os.path.join(dieslib.BUILD, 'probe', d, arch, d,
                                  'windows.json') for d in dlist]
            files = [f for f in files if os.path.exists(f)]
            rc = run([
                sys.executable,
                os.path.join(HERE, 'windows.py'), '--die', dlist[0],
                '--merge'] + files + [
                '--out', os.path.join(dieslib.DB, arch, 'windows.json')
            ], os.path.join(logdir, f'windows_{arch}.log'))
            print('windows rc', rc, flush=True)
        with ProcessPoolExecutor(min(len(dlist), 8)) as ex:
            for die, rc in ex.map(tilegrid, [(d, arch) for d in dlist]):
                print('tilegrid', die, 'rc', rc, flush=True)
    exclude = []
    if args.consistency:
        path = consistency(dlist, arch, tags, logdir)
        if path:
            exclude = ['--exclude', path]
    sj = []
    if args.sample_jobs:
        with open(os.path.join(HERE, 'mkdb.py')) as f:
            if '--sample-jobs' not in f.read():
                sys.exit('mkdb.py has no --sample-jobs')
        sj = ['--sample-jobs', str(args.sample_jobs)]
    t0 = time.time()
    rc = run([
        sys.executable,
        os.path.join(HERE, 'mkdb.py'), '--arch', arch, '--dies',
        args.dies, '--tag', args.tags, '--jobs',
        str(args.jobs)
    ] + sj + exclude, os.path.join(logdir, f'mkdb_{arch}.log'))
    print(f'mkdb {time.time() - t0:.0f} s', flush=True)
    print('build_db rc', rc, flush=True)
    # Checks of all dies in parallel, each checking its designs in
    # parallel with a share of --jobs proportional to its size (the tile
    # grid's size: bits per design), so that the big dies do not finish
    # last with one job.
    size = {d: os.path.getsize(os.path.join(dieslib.DB, arch, d,
                                            'tilegrid.json'))
            for d in dlist}
    cjobs = {d: max(1, min(20, round(args.jobs * size[d] /
                                     sum(size.values()))))
             for d in dlist}

    def check(d):
        roots = ','.join(
            os.path.join(dieslib.BUILD, 'designs', d, t)
            for t in (args.check_tags or args.tags).split(','))
        log = os.path.join(logdir, f'check_{d}.log')
        run([
            sys.executable,
            os.path.join(HERE, 'check.py'), '--die', d, '--designs', roots,
            '--max', '20', '--jobs', str(cjobs[d])
        ], log)
        return log
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    with ThreadPoolExecutor(len(dlist)) as ex:
        logs = list(ex.map(check, dlist))
    print(f'checks {time.time() - t0:.0f} s (jobs per die '
          f'{" ".join(f"{d}:{cjobs[d]}" for d in dlist)})', flush=True)
    for d, log in zip(dlist, logs):
        tail = open(log).read().strip().split('\n')
        summary = [l for l in tail if not l.startswith('/')]
        print(f'== check {d}')
        print('\n'.join(l[:300] for l in summary[:25]), flush=True)


if __name__ == '__main__':
    main()
