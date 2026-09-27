#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Generate, implement and post-process random fuzzing designs for a die.

For every seed a directory <workdir>/<die>/<tag>/s<seed> is produced holding:
  design.tcl            the generated netlist script
  nl.log                generator/implementation log
  design.features.gz    feature dump
  bits.npz              set (non ECC) bits of the bitstream
  run.stats             wall/CPU time and outcome of the run (JSON)

With --reuse N one Vivado process runs up to N designs of a die in turn
(generic/tcl/nl_server.tcl), which saves the Vivado start-up and device load
time of each design.
"""
import argparse
import collections
import gzip
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

import bitstream
import dies as dieslib

HERE = os.path.dirname(os.path.abspath(__file__))
TCL = os.path.join(os.path.dirname(HERE), 'tcl')
VIVADO_SETTINGS = os.environ.get(
    'URAY_VIVADO_SETTINGS', '/opt/xilinx/Vivado/2025.2/settings64.sh')
CLK_TCK = os.sysconf('SC_CLK_TCK')


def save_bits(bitfile, arch, out):
    _, frames, _ = bitstream.frames_explicit(bitfile, arch)
    far, word, bit = bitstream.set_bits(frames, arch)
    np.savez_compressed(out, far=far, word=word, bit=bit)


def budget_of(die, gen_args=()):
    """Repair loop time budget, scaled with the die size (with --region F,
    with the size of the part of the die the design uses)."""
    if not hasattr(die, '_ntiles'):
        die._ntiles = sum(1 for _ in open(die.tiles_tsv))
    n = die._ntiles
    gen_args = list(gen_args)
    if '--region' in gen_args:
        n *= float(gen_args[gen_args.index('--region') + 1])
    return 900 if n < 40000 else (1800 if n < 100000 else 3000)


def timeout_of(die, gen_args=()):
    """Hard wall clock limit of a design: the repair budget is only checked
    between attempts, and a single place/route can run for hours on an
    unroutable design.  Twice the budget plus 10 minutes kept 99.5% of the
    successful designs of the r1-r7 batches."""
    return 2 * budget_of(die, gen_args) + 600


def generate(die, seed, wdir, gen_args):
    """Writes design.tcl (and design.meta); returns False on failure."""
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
        return False
    return True


def postprocess(die, wdir):
    """Converts the Vivado outputs; returns the design status."""
    bit = os.path.join(wdir, 'design.bit')
    feat = os.path.join(wdir, 'design.features')
    if not (os.path.exists(bit) and os.path.exists(feat)):
        return 'failed'
    with open(feat, 'rb') as f:
        f.seek(max(0, os.path.getsize(feat) - 4096))
        complete = b'# t_done' in f.read()
    if not complete:
        # Vivado died while dumping (the bitstream is there, the feature
        # dump is truncated): not a usable design.
        return 'failed dump'
    save_bits(bit, die.arch, os.path.join(wdir, 'bits.npz'))
    with open(feat, 'rb') as fi, gzip.open(feat + '.gz', 'wb') as fo:
        shutil.copyfileobj(fi, fo)
    open(os.path.join(wdir, 'dump_v2'), 'w').close()
    os.unlink(feat)
    os.unlink(bit)
    # (NL_KEEP_LOGS=1: keep the Vivado logs of successful designs too,
    # e.g. to analyse their repair rounds.)
    keep = os.environ.get('NL_KEEP_LOGS') == '1'
    for junk in (() if keep else ('run.log', 'vivado.log')) + \
            ('clockInfo.txt',):
        p = os.path.join(wdir, junk)
        if os.path.exists(p):
            os.unlink(p)
    shutil.rmtree(os.path.join(wdir, '.Xil'), ignore_errors=True)
    return 'ok'


PAGE = os.sysconf('SC_PAGE_SIZE')


def tree_stats(pid):
    """(CPU seconds used so far, resident bytes now) of a process and its
    live descendants (CPU includes the children they waited for)."""
    procs = {}
    for p in os.listdir('/proc'):
        if not p.isdigit():
            continue
        try:
            with open(f'/proc/{p}/stat') as f:
                s = f.read()
        except OSError:
            continue
        rest = s[s.rfind(')') + 2:].split()
        procs[int(p)] = (int(rest[1]), sum(int(x) for x in rest[11:15]),
                         int(rest[21]))
    kids = collections.defaultdict(list)
    for p, (pp, _, _) in procs.items():
        kids[pp].append(p)
    cpu, rss, todo = 0, 0, [pid]
    while todo:
        p = todo.pop()
        if p in procs:
            cpu += procs[p][1]
            rss += procs[p][2]
        todo += kids.get(p, [])
    return cpu / CLK_TCK, rss * PAGE


def tree_cpu(pid):
    return tree_stats(pid)[0]


def set_directive(d):
    """place_design/route_design -directive for the designs run by this
    process (netlist.tcl reads NL_PLACE_DIRECTIVE/NL_ROUTE_DIRECTIVE)."""
    if d:
        os.environ['NL_PLACE_DIRECTIVE'] = d
        os.environ['NL_ROUTE_DIRECTIVE'] = d


def vivado_env(threads):
    env = dict(os.environ)
    env['NL_THREADS'] = str(threads)
    return env


# Process groups of the running Vivado processes (each runs in its own
# session so a timeout can kill it with all its children).  They are killed
# when this process exits or is terminated, so no Vivado outlives it.
_GROUPS = set()
_GROUPS_LOCK = threading.Lock()


def _spawn(*args, **kw):
    p = subprocess.Popen(*args, start_new_session=True, **kw)
    with _GROUPS_LOCK:
        _GROUPS.add(p.pid)
    return p


def _reaped(pid):
    with _GROUPS_LOCK:
        _GROUPS.discard(pid)


def kill_all_groups():
    with _GROUPS_LOCK:
        groups = list(_GROUPS)
    for g in groups:
        try:
            os.killpg(g, signal.SIGKILL)
        except OSError:
            pass


def _on_signal(signum, frame):
    kill_all_groups()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def install_cleanup():
    """Kill the Vivado process groups on exit, SIGTERM, SIGINT, SIGHUP."""
    import atexit
    atexit.register(kill_all_groups)
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, _on_signal)


def run_fresh(die, wdir, timeout, threads, budget):
    """One Vivado process for the design.  Returns (status, cpu seconds,
    peak resident bytes)."""
    vcmd = (f'source {VIVADO_SETTINGS} && NL_BUDGET={budget} exec '
            f'vivado -mode batch -nojournal -log vivado.log -source '
            f'design.tcl > run.log 2>&1')
    p = _spawn(['bash', '-c', vcmd], cwd=wdir, env=vivado_env(threads))
    t0 = time.time()
    peak = 0
    n = 0
    while True:
        pid, status, ru = os.wait4(p.pid, os.WNOHANG)
        if pid:
            # Leftover children of the group (if any) go too.
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            _reaped(p.pid)
            # ru_maxrss (KiB): the largest process of the tree.
            return 'done', ru.ru_utime + ru.ru_stime, max(
                peak, ru.ru_maxrss * 1024)
        if time.time() - t0 > timeout:
            cpu = tree_cpu(p.pid)
            os.killpg(p.pid, signal.SIGKILL)
            os.wait4(p.pid, 0)
            _reaped(p.pid)
            return 'timeout', cpu, peak
        n += 1
        if n % 10 == 0:
            peak = max(peak, tree_stats(p.pid)[1])
        time.sleep(1)


class Worker:
    """A Vivado process running designs of one die (nl_server.tcl)."""

    def __init__(self, die, threads, logdir):
        self.die = die
        self.ndone = 0
        self.log = os.path.join(logdir, f'vivado_{os.getpid()}_'
                                f'{threading.get_ident()}_{time.time():.0f}.log')
        self.home = tempfile.mkdtemp(prefix='nlsrv_', dir=logdir)
        env = vivado_env(threads)
        env['NL_VIVADO_LOG'] = self.log
        vcmd = (f'source {VIVADO_SETTINGS} && exec vivado -mode batch '
                f'-nojournal -log {self.log} -source '
                f'{os.path.join(TCL, "nl_server.tcl")}')
        self.p = _spawn(['bash', '-c', vcmd], cwd=self.home,
                        env=env, stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.out = None
        self.cond = threading.Condition()
        self.done = None
        self.ready = False
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.p.stdout:
            with self.cond:
                if self.out:
                    self.out.write(line)
                if line.startswith('NLREADY'):
                    self.ready = True
                    self.cond.notify_all()
                elif line.startswith('NLDONE '):
                    self.done = line.split()[-1]
                    self.cond.notify_all()
        with self.cond:
            self.done = 'eof'
            self.cond.notify_all()

    def alive(self):
        return self.p.poll() is None

    def run(self, wdir, timeout, budget):
        """Returns (status, cpu seconds, peak resident bytes sampled every
        5 s)."""
        cpu0 = tree_cpu(self.p.pid)
        peak = 0
        t0 = time.time()
        with self.cond:
            self.out = open(os.path.join(wdir, 'run.log'), 'w')
            self.done = None
            while not self.ready and self.done != 'eof':
                self.cond.wait(5)
                if time.time() - t0 > timeout:
                    break
        try:
            self.p.stdin.write(f'{wdir} {budget}\n')
            self.p.stdin.flush()
        except OSError:
            pass
        status = 'done'
        with self.cond:
            while self.done is None:
                self.cond.wait(5)
                peak = max(peak, tree_stats(self.p.pid)[1])
                if time.time() - t0 > timeout:
                    status = 'timeout'
                    break
            if self.done == 'eof':
                status = 'crash'
        cpu = tree_cpu(self.p.pid) - cpu0
        if status != 'done':
            self.kill()
        with self.cond:
            self.out.close()
            self.out = None
        self.ndone += 1
        return status, cpu, peak

    def kill(self):
        try:
            os.killpg(self.p.pid, signal.SIGKILL)
        except OSError:
            pass
        self.p.wait()
        _reaped(self.p.pid)

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(120)
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.kill()
        shutil.rmtree(self.home, ignore_errors=True)
        try:
            os.unlink(self.log)
        except OSError:
            pass


class WorkerPool:
    """One Vivado worker per scheduler thread, restarted after `reuse`
    designs, on a die change or when it died."""

    def __init__(self, reuse, threads, logdir):
        self.reuse = reuse
        self.threads = threads
        self.logdir = logdir
        self.local = threading.local()
        self.open = []
        self.lock = threading.Lock()

    def _close(self, w):
        with self.lock:
            if w in self.open:
                self.open.remove(w)
        w.close()
        if getattr(self.local, 'w', None) is w:
            self.local.w = None

    def get(self, die):
        w = getattr(self.local, 'w', None)
        if w and (w.die is not die or w.ndone >= self.reuse or
                  not w.alive()):
            self._close(w)
            w = None
        if w is None:
            w = Worker(die, self.threads, self.logdir)
            with self.lock:
                self.open.append(w)
            self.local.w = w
        return w

    def done(self, pending):
        """After a design of this thread: close its worker when it is used
        up, or when fewer designs of its die are pending (pending(die))
        than workers of that die are open (idle workers hold GiBs)."""
        w = getattr(self.local, 'w', None)
        if w is None:
            return
        if w.ndone >= self.reuse or not w.alive():
            self._close(w)
            return
        with self.lock:
            same = sum(1 for x in self.open if x.die is w.die)
            close = pending(w.die) < same
            if close:
                self.open.remove(w)
        if close:
            w.close()
            self.local.w = None

    def close(self):
        with self.lock:
            ws, self.open = self.open, []
        for w in ws:
            w.close()


def run_one(die, seed, wdir, gen_args, timeout, threads, pool=None):
    if os.path.exists(os.path.join(wdir, 'bits.npz')):
        return seed, 'cached'
    t0 = time.time()
    stats = {'start': t0, 'host': socket.gethostname(), 'threads': threads,
             'reuse': pool.reuse if pool else 1,
             'directive': os.environ.get('NL_PLACE_DIRECTIVE', '')}
    status, cpu, rss = 'generror', 0.0, 0
    if not timeout:
        timeout = timeout_of(die, gen_args)
    if generate(die, seed, wdir, gen_args):
        if pool:
            vstatus, cpu, rss = pool.get(die).run(wdir, timeout,
                                                   budget_of(die, gen_args))
        else:
            vstatus, cpu, rss = run_fresh(die, wdir, timeout, threads,
                                          budget_of(die, gen_args))
        status = vstatus if vstatus in ('timeout', 'crash') else \
            postprocess(die, wdir)
    t1 = time.time()
    stats.update(end=t1, wall=t1 - t0, cpu=cpu, status=status,
                 maxrss=rss)
    with open(os.path.join(wdir, 'run.stats'), 'w') as f:
        json.dump(stats, f)
    return seed, status


def job_memory(workdir, die):
    """Memory to reserve per job for a die: the larger of the 90th
    percentile and 1.2 x the median of the peak memory recorded in the
    run.stats of its earlier designs (0 when there are none)."""
    import glob
    rss = []
    for p in glob.glob(os.path.join(workdir, die, '*', 's*', 'run.stats')):
        try:
            v = json.load(open(p)).get('maxrss')
        except (OSError, ValueError):
            continue
        if v:
            rss.append(v)
    if not rss:
        return 0
    rss.sort()
    return max(rss[int(0.9 * (len(rss) - 1))], 1.2 * rss[len(rss) // 2])


class JobQueue:
    """Jobs handed to threads preferring the die a thread worked on last
    (a Vivado worker keeps its device loaded)."""

    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.lock = threading.Lock()
        self.local = threading.local()

    def take(self):
        with self.lock:
            if not self.jobs:
                return None
            last = getattr(self.local, 'die', None)
            for i, j in enumerate(self.jobs):
                if j[0] is last:
                    break
            else:
                i = 0
            job = self.jobs.pop(i)
        self.local.die = job[0]
        return job

    def pending(self, die):
        with self.lock:
            return sum(1 for j in self.jobs if j[0] is die)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--die', required=True, help='comma separated dies')
    ap.add_argument('--tag', default='fabric')
    ap.add_argument('--seeds', required=True, help='first:last')
    ap.add_argument('--jobs', type=int, default=16)
    ap.add_argument('--mem-budget', type=float, default=None,
                    help='GiB for all jobs: --jobs is lowered to fit the '
                    'peak memory measured on earlier designs of the dies')
    ap.add_argument('--timeout', type=int, default=None,
                    help='seconds per design (default: 2 x repair budget + 600)')
    ap.add_argument('--directive', default=None,
                    help='place_design/route_design directive (e.g. Quick)')
    ap.add_argument('--threads', type=int, default=2,
                    help='Vivado general.maxThreads')
    ap.add_argument('--reuse', type=int, default=1,
                    help='designs run by one Vivado process (1: a fresh '
                    'process per design)')
    ap.add_argument('--workdir',
                    default=os.path.join(dieslib.BUILD, 'designs'))
    ap.add_argument('gen_args', nargs='*')
    args = ap.parse_args()
    install_cleanup()
    set_directive(args.directive)
    alldies = dieslib.load()
    dlist = [alldies[n] for n in args.die.split(',')]
    if args.mem_budget:
        per = max(job_memory(args.workdir, d.name) for d in dlist)
        if per:
            jobs = max(1, min(args.jobs, int(args.mem_budget * 2**30 // per)))
            print(f'# {per / 2**30:.1f} GiB per job: {jobs} jobs in '
                  f'{args.mem_budget} GiB', flush=True)
            args.jobs = jobs
        else:
            print('# no memory measurements for these dies: --jobs '
                  f'{args.jobs}', flush=True)
    first, last = map(int, args.seeds.split(':'))
    jobs = [(die, s) for s in range(first, last + 1) for die in dlist]
    pool = None
    if args.reuse > 1:
        logdir = os.path.join(args.workdir, '.workers')
        os.makedirs(logdir, exist_ok=True)
        pool = WorkerPool(args.reuse, args.threads, logdir)
    queue = JobQueue(jobs)

    def job():
        j = queue.take()
        die, s = j
        wdir = os.path.join(args.workdir, die.name, args.tag, f's{s}')
        try:
            return die.name, s, run_one(die, s, wdir, args.gen_args,
                                        args.timeout, args.threads,
                                        pool)[1]
        except Exception as e:  # keep going on post-processing errors
            return die.name, s, f'exception {e}'
        finally:
            if pool:
                pool.done(queue.pending)

    t0 = time.time()
    done = 0
    counts = collections.Counter()
    try:
        with ThreadPoolExecutor(args.jobs) as ex:
            futs = [ex.submit(job) for _ in jobs]
            for f in as_completed(futs):
                name, seed, status = f.result()
                counts[status.split()[0]] += 1
                done += 1
                el = time.time() - t0
                eta = el / done * (len(jobs) - done)
                print(f'[{time.strftime("%H:%M:%S")}] {done}/{len(jobs)} '
                      f'{name} s{seed} {status} elapsed {el:.0f}s '
                      f'eta {eta:.0f}s ({time.strftime("%H:%M", time.localtime(time.time() + eta))}) '
                      f'{dict(counts)}',
                      flush=True)
    finally:
        if pool:
            pool.close()


if __name__ == '__main__':
    main()
