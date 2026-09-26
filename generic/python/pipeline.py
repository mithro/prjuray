#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Tile grids + bit database + check for a set of dies of one architecture."""
import argparse
import os
import subprocess
import sys
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
    out = os.path.join(dieslib.BUILD, 'db', arch, die)
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
    out = os.path.join(dieslib.BUILD, 'db', arch, die)
    return die, run([
        sys.executable,
        os.path.join(HERE, 'tilegrid.py'), '--die', die, '--evidence',
        os.path.join(out, 'evidence.json'), '--colmap',
        os.path.join(out, 'colmap.json'), '--out',
        os.path.join(out, 'tilegrid.json')
    ], os.path.join(out, 'tilegrid.log'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dies', required=True)
    ap.add_argument('--tags', required=True)
    ap.add_argument('--check-tags', default=None)
    ap.add_argument('--skip-tilegrid', action='store_true')
    ap.add_argument('--jobs', type=int, default=24)
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
            os.path.join(dieslib.BUILD, 'db'), '--verbose'
        ], os.path.join(logdir, f'colalign_{arch}.log'))
        print('colalign rc', rc, flush=True)
        with ProcessPoolExecutor(min(len(dlist), 8)) as ex:
            for die, rc in ex.map(tilegrid, [(d, arch) for d in dlist]):
                print('tilegrid', die, 'rc', rc, flush=True)
    rc = run([
        sys.executable,
        os.path.join(HERE, 'mkdb.py'), '--arch', arch, '--dies',
        args.dies, '--tag', args.tags, '--jobs',
        str(args.jobs)
    ], os.path.join(logdir, f'mkdb_{arch}.log'))
    print('build_db rc', rc, flush=True)
    for d in dlist:
        roots = ','.join(
            os.path.join(dieslib.BUILD, 'designs', d, t)
            for t in (args.check_tags or args.tags).split(','))
        log = os.path.join(logdir, f'check_{d}.log')
        run([
            sys.executable,
            os.path.join(HERE, 'check.py'), '--die', d, '--designs', roots,
            '--max', '20'
        ], log)
        tail = open(log).read().strip().split('\n')
        summary = [l for l in tail if not l.startswith('/')]
        print(f'== check {d}')
        print('\n'.join(l[:300] for l in summary[:25]), flush=True)


if __name__ == '__main__':
    main()
