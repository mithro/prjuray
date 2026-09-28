"""relaxstat.py <glob>...: outcome and wall time by nl_finish relaxclk flag."""
import glob, os, sys, json, collections, statistics
res = collections.defaultdict(list)
for pat in sys.argv[1:]:
    for d in glob.glob(pat):
        try:
            last = open(d + '/design.tcl').read().rstrip().rsplit('\n', 1)[-1]
        except OSError:
            continue
        if not last.startswith('nl_finish'):
            continue
        flag = last.split()[1] if len(last.split()) > 1 else '0'
        ok = os.path.exists(d + '/bits.npz')
        try:
            st = json.load(open(d + '/run.stats'))
            wall, status = st['wall'], st['status']
        except (OSError, ValueError, KeyError):
            files = [os.path.join(d, f) for f in os.listdir(d)]
            wall = max(os.stat(f).st_mtime for f in files) - os.stat(d + '/design.tcl').st_mtime
            status = 'ok' if ok else 'fail?'
        res[flag].append((ok, wall, status))
for flag, rows in sorted(res.items()):
    n = len(rows)
    ok = sum(1 for r in rows if r[0])
    tw = sum(r[1] for r in rows)
    to = sum(1 for r in rows if r[2] == 'timeout')
    print(f'relaxclk={flag}: {n} designs, ok {ok} ({100*ok/n:.0f}%), timeouts {to}, '
          f'wall total {tw/3600:.1f} h, ok per slot hour {ok/(tw/3600):.2f}, '
          f'median wall {statistics.median(r[1] for r in rows):.0f} s')
