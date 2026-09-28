"""placeattempts.py <glob>...: for ok designs, place_design failures before the
final placement and the time (s) until the first successful placement."""
import glob, os, re, sys, statistics, collections
rows = []
for pat in sys.argv[1:]:
    for d in glob.glob(pat):
        if not os.path.exists(d + '/bits.npz'):
            continue
        try:
            log = open(d + '/nl.log', errors='replace').read()
        except OSError:
            continue
        pf = len(re.findall(r'^place_design failed', log, re.M))
        rf = len(re.findall(r'^route_design failed', log, re.M))
        wf = len(re.findall(r'^write_bitstream failed', log, re.M))
        placed = [int(x) for x in re.findall(r'^placed (\d+)$', log, re.M)]
        done = re.findall(r'^written (\d+)$', log, re.M)
        if not placed or not done:
            continue
        rows.append((pf, rf, wf, placed[0], int(done[-1]), len(placed)))
n = len(rows)
print(f'{n} ok designs')
c = collections.Counter(min(r[0], 5) for r in rows)
print('place failures before success:', dict(sorted(c.items())))
print('route failures:', dict(sorted(collections.Counter(min(r[1], 5) for r in rows).items())))
print('bitgen failures:', dict(sorted(collections.Counter(min(r[2], 5) for r in rows).items())))
print('placements (incl. re-placements after route/bitgen repair):', dict(sorted(collections.Counter(min(r[5], 6) for r in rows).items())))
tot = sum(r[4] for r in rows)
first = sum(r[3] for r in rows)
print(f'time to written: total {tot/3600:.1f} h; to first placement {first/3600:.1f} h ({100*first/tot:.0f}%)')
clean = [r for r in rows if r[0] == 0 and r[1] == 0 and r[2] == 0]
print(f'clean (no repair) designs {len(clean)} ({100*len(clean)/n:.0f}%), their time {sum(r[4] for r in clean)/3600:.1f} h of {tot/3600:.1f} h')
