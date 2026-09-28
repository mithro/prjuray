#!/usr/bin/env python3
"""Replay Vivado logs through run_designs.StallWatch as if they were growing
(64 KiB at a time) and print where it would have killed the run.

    stallreplay.py <vivado.log>...     (NL_STALL_ITERS as in run_designs)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'python'))
import run_designs  # noqa: E402

for log in sys.argv[1:]:
    data = open(log, 'rb').read()
    total = data.count(b'Nodes with overlaps')
    hit = None
    with tempfile.NamedTemporaryFile() as t:
        w = run_designs.StallWatch(t.name)
        for i in range(0, len(data), 65536):
            t.write(data[i:i + 65536])
            t.flush()
            if w.stalled():
                hit = data[:i + 65536].count(b'Nodes with overlaps')
                break
    print(f'{log}: ' + (f'killed at overlap line {hit} of {total} '
                        f'(count {w.last.decode()})' if hit else
                        f'not killed ({total} overlap lines)'))
