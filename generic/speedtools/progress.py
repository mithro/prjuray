"""progress.py <total> <done-marker> <log>...: one progress line every 300 s until the marker exists."""
import os, sys, time
total, marker, logs = int(sys.argv[1]), sys.argv[2], sys.argv[3:]
t0 = time.time()
while not os.path.exists(marker):
    d = 0
    for l in logs:
        try:
            d += sum(1 for x in open(l) if x.startswith('['))
        except OSError:
            pass
    el = time.time() - t0
    eta = el / d * (total - d) if d else float('nan')
    fin = time.strftime('%H:%M', time.localtime(time.time() + eta)) if d else '?'
    print(f'{time.strftime("%H:%M")} done {d}/{total} designs, eta {eta:.0f}s (~{fin})', flush=True)
    time.sleep(300)
print('DONE', flush=True)
