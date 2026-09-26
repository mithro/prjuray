#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Generate, implement and post-process random fuzzing designs for a die.

For every seed a directory <workdir>/<die>/<tag>/s<seed> is produced holding:
  design.tcl            the generated netlist script
  nl.log                generator/implementation log
  design.features.gz    feature dump
  bits.npz              set (non ECC) bits of the bitstream
"""
import argparse
import collections
import gzip
import os
import random
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

import bitstream
import dies as dieslib

HERE = os.path.dirname(os.path.abspath(__file__))
VIVADO_SETTINGS = os.environ.get(
    'URAY_VIVADO_SETTINGS', '/opt/xilinx/Vivado/2025.2/settings64.sh')


def save_bits(bitfile, arch, out):
    _, frames, _ = bitstream.frames_explicit(bitfile, arch)
    far, word, bit = bitstream.set_bits(frames, arch)
    np.savez_compressed(out, far=far, word=word, bit=bit)


def run_one(die, seed, wdir, gen_args, timeout):
    if os.path.exists(os.path.join(wdir, 'bits.npz')):
        return seed, 'cached'
    os.makedirs(wdir, exist_ok=True)
    cmd = [
        sys.executable,
        os.path.join(HERE, 'gen_design.py'), '--tiles', die.tiles_tsv,
        '--prims', die.prims_txt, '--seed',
        str(seed), '--out', 'design.tcl', '--part',
        random.Random(seed).choice(dieslib.fuzz_parts(die))
    ] + gen_args
    r = subprocess.run(cmd, cwd=wdir, capture_output=True, text=True)
    if r.returncode:
        open(os.path.join(wdir, 'gen.err'), 'w').write(r.stderr)
        return seed, 'generror'
    # Repair loop time budget, scaled with the die size.
    ntiles = sum(1 for _ in open(die.tiles_tsv))
    budget = 900 if ntiles < 40000 else (1800 if ntiles < 100000 else 3000)
    vcmd = (f'source {VIVADO_SETTINGS} && NL_BUDGET={budget} vivado -mode '
            f'batch -nojournal -log vivado.log -source design.tcl > run.log '
            f'2>&1')
    try:
        subprocess.run(['bash', '-c', vcmd], cwd=wdir, timeout=timeout)
    except subprocess.TimeoutExpired:
        return seed, 'timeout'
    bit = os.path.join(wdir, 'design.bit')
    feat = os.path.join(wdir, 'design.features')
    if not (os.path.exists(bit) and os.path.exists(feat)):
        return seed, 'failed'
    save_bits(bit, die.arch, os.path.join(wdir, 'bits.npz'))
    with open(feat, 'rb') as fi, gzip.open(feat + '.gz', 'wb') as fo:
        shutil.copyfileobj(fi, fo)
    if b'# t_done' in open(feat, 'rb').read():
        open(os.path.join(wdir, 'dump_v2'), 'w').close()
    os.unlink(feat)
    os.unlink(bit)
    for junk in ('run.log', 'clockInfo.txt', 'vivado.log'):
        p = os.path.join(wdir, junk)
        if os.path.exists(p):
            os.unlink(p)
    shutil.rmtree(os.path.join(wdir, '.Xil'), ignore_errors=True)
    return seed, 'ok'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--die', required=True, help='comma separated dies')
    ap.add_argument('--tag', default='fabric')
    ap.add_argument('--seeds', required=True, help='first:last')
    ap.add_argument('--jobs', type=int, default=16)
    ap.add_argument('--timeout', type=int, default=7200)
    ap.add_argument('--workdir',
                    default=os.path.join(dieslib.BUILD, 'designs'))
    ap.add_argument('gen_args', nargs='*')
    args = ap.parse_args()
    alldies = dieslib.load()
    dlist = [alldies[n] for n in args.die.split(',')]
    first, last = map(int, args.seeds.split(':'))
    jobs = [(die, s) for s in range(first, last + 1) for die in dlist]
    t0 = time.time()
    done = 0
    counts = collections.Counter()
    with ThreadPoolExecutor(args.jobs) as ex:
        futs = {
            ex.submit(run_one, die, s,
                      os.path.join(args.workdir, die.name, args.tag, f's{s}'),
                      args.gen_args, args.timeout): (die.name, s)
            for die, s in jobs
        }
        for f in as_completed(futs):
            name, seed = futs[f]
            try:
                _, status = f.result()
            except Exception as e:  # keep going on post-processing errors
                status = f'exception {e}'
            counts[status.split()[0]] += 1
            done += 1
            el = time.time() - t0
            eta = el / done * (len(jobs) - done)
            print(f'[{time.strftime("%H:%M:%S")}] {done}/{len(jobs)} '
                  f'{name} s{seed} {status} elapsed {el:.0f}s '
                  f'eta {eta:.0f}s ({time.strftime("%H:%M", time.localtime(time.time() + eta))}) '
                  f'{dict(counts)}',
                  flush=True)


if __name__ == '__main__':
    main()
