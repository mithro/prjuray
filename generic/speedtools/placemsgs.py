"""placemsgs.py <glob>...: error/critical message ids in failed place_design calls,
counted, with one example each; and how many offenders nl.log removed per attempt."""
import glob, os, re, sys, collections
ids = collections.Counter()
ex = {}
per_call = []
for pat in sys.argv[1:]:
    for d in glob.glob(pat):
        for f in ('vivado.log', 'run.log'):
            p = os.path.join(d, f)
            if os.path.exists(p):
                break
        else:
            continue
        txt = open(p, errors='replace').read()
        for m in re.finditer(r'Command: place_design[^\n]*\n(.*?)(?=Command: |\Z)', txt, re.S):
            body = m.group(1)
            if 'ERROR' not in body:
                continue
            got = set()
            for mm in re.finditer(r'^(ERROR|CRITICAL WARNING): \[([^\]]+)\] ([^\n]*)', body, re.M):
                k = (mm.group(1)[0], mm.group(2))
                ids[k] += 1
                got.add(k)
                ex.setdefault(k, mm.group(3)[:220])
            ph = re.findall(r'^Phase (\d+(?:\.\d+)*) ', body, re.M)
            per_call.append((ph[-1] if ph else '-', len(got)))
for k, n in ids.most_common(30):
    print(f'{n:6d} {k[0]} {k[1]:14s} {ex[k]}')
print('calls', len(per_call))
