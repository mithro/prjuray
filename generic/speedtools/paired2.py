"""paired2.py <die> <tag>...: per phase mean over seeds ok in all tags."""
import sys, os, json, glob, re, statistics
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'python'))
import dies
B = os.path.join(dies.BUILD, 'designs')
die, tags = sys.argv[1], sys.argv[2:]
data = {}
for t in tags:
    for d in glob.glob(f'{B}/{die}/{t}/s*'):
        try:
            st = json.load(open(d + '/run.stats'))
        except (OSError, ValueError):
            continue
        if st['status'] != 'ok':
            continue
        log = open(d + '/nl.log').read()
        ph = {}
        for k, v in re.findall(r'^(placed|routed|written|dumped|done) (\d+)$', log, re.M):
            ph[k] = int(v)
        data.setdefault(os.path.basename(d), {})[t] = (st, ph)
common = [s for s, v in data.items() if all(t in v for t in tags)]
print(f'{len(common)} seeds')
for t in tags:
    rows = [data[s][t] for s in common]
    m = lambda f: statistics.mean(f(st, ph) for st, ph in rows)
    print(f'{t:10s} wall {m(lambda s,p: s["wall"]):6.1f} cpu {m(lambda s,p: s["cpu"]):6.1f} '
          f'pre-finish {m(lambda s,p: s["wall"] - p["done"]):5.1f} place {m(lambda s,p: p["placed"]):5.1f} '
          f'route {m(lambda s,p: p["routed"]-p["placed"]):5.1f} bitgen {m(lambda s,p: p["written"]-p["routed"]):5.1f} '
          f'dump {m(lambda s,p: p["dumped"]-p["written"]):5.1f}')
