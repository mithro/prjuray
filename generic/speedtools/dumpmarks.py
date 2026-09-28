"""dumpmarks.py <glob>: '# t_' markers (ms since dump start) of feature dumps, as deltas."""
import glob, gzip, sys, re
for d in sorted(glob.glob(sys.argv[1]))[:int(sys.argv[2]) if len(sys.argv) > 2 else 10]:
    try:
        marks = [l.split() for l in gzip.open(d + '/design.features.gz', 'rt') if l.startswith('# t_')]
    except OSError:
        continue
    prev = 0
    out = []
    for _, name, v in marks:
        v = int(v)
        out.append(f'{name[2:]} {(v - prev) / 1000:.0f}')
        prev = v
    print(d.split('/')[-1], ' | '.join(out))
