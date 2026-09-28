"""placetimes.py <glob>...: elapsed time of failed vs successful place_design
calls, from the run.log/vivado.log kept for failed designs."""
import glob, os, re, sys, statistics
fail, ok = [], []
phase_fail = {}
for pat in sys.argv[1:]:
    for d in glob.glob(pat):
        for f in ('vivado.log', 'run.log'):
            p = os.path.join(d, f)
            if os.path.exists(p):
                break
        else:
            continue
        txt = open(p, errors='replace').read()
        # split into place_design commands
        for m in re.finditer(r'Command: place_design[^\n]*\n(.*?)(?=Command: |\Z)', txt, re.S):
            body = m.group(1)
            t = re.search(r'place_design: Time \(s\): cpu = [\d:]+ ; elapsed = (\d+):(\d+):(\d+)', body)
            if not t:
                continue
            sec = int(t.group(1)) * 3600 + int(t.group(2)) * 60 + int(t.group(3))
            if 'ERROR' in body:
                fail.append(sec)
                ph = re.findall(r'^Phase (\d+(?:\.\d+)*) ', body, re.M)
                last = ph[-1] if ph else '-'
                phase_fail[last] = phase_fail.get(last, 0) + 1
            else:
                ok.append(sec)
print(f'successful place_design: {len(ok)}, median {statistics.median(ok) if ok else 0} s, total {sum(ok)/3600:.1f} h')
print(f'failed place_design: {len(fail)}, median {statistics.median(fail) if fail else 0} s, total {sum(fail)/3600:.1f} h')
print('last placer phase reached by failed calls:', dict(sorted(phase_fail.items(), key=lambda x: -x[1])[:12]))
