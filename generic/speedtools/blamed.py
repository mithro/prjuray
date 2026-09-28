"""blamed.py <glob>...: cell types removed after place_design failures (from
nl.log + design.tcl), per round, over all designs (ok and failed)."""
import glob, os, re, sys, collections
rounds = collections.Counter()
refs = collections.Counter()
nrm = collections.Counter()
other = collections.Counter()
for pat in sys.argv[1:]:
    for d in glob.glob(pat):
        try:
            log = open(d + '/nl.log', errors='replace').read().split('\n')
            tcl = open(d + '/design.tcl', errors='replace').read()
        except OSError:
            continue
        ref_of = {}
        for m in re.finditer(r'^nl_cell (\S+) (\S+)', tcl, re.M):
            ref_of[m.group(1)] = m.group(2)
        for m in re.finditer(r'^nl_iob (\S+) \S+ \S+ (\S+)', tcl, re.M):
            ref_of[m.group(1)] = m.group(2)
        for i, l in enumerate(log):
            if not l.startswith('place_design failed'):
                continue
            rounds['place failures'] += 1
            # what the repair did next
            for l2 in log[i + 1:i + 6]:
                if l2.startswith('removing'):
                    names = re.findall(r'\b((?:c|io)\d+)\b', l2)
                    n = int(l2.split()[1])
                    nrm[min(n, 10)] += 1
                    kinds = collections.Counter(ref_of.get(x, '?') for x in names)
                    for k in kinds:
                        refs[k] += 1
                    break
                if l2.startswith(('dropping', 'blaming', 'orphans', 'tying', 'driverless')):
                    other[l2.split(':')[0][:40]] += 1
print(dict(rounds))
print('cells removed per round:', dict(sorted(nrm.items())))
print('other repairs:', dict(other.most_common(8)))
print('rounds removing each cell type:')
for k, n in refs.most_common(25):
    print(f'  {n:5d} {k}')
