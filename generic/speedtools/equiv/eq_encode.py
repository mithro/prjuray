"""eq_encode.py <die> <tag> <n>: the sample chunk bytes of
_encode_chunk_joined (joined sorted features) must equal those of
_encode_chunk (feature sets) for every chunk of n designs."""
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)),
                                'python'))
import designdata as DD  # noqa: E402
import dies as dieslib  # noqa: E402
import mkdb  # noqa: E402

dn, tag, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
die = dieslib.load()[dn]
col = mkdb._collector(die.arch, dn)
ta = tb = 0
nch = 0
for d in DD.design_dirs(os.path.join(dieslib.BUILD, 'designs', dn, tag),
                        v2only=True)[:n]:
    ca, cb = {}, {}
    for src, out in ((col.sample_codes, ca), (col.sample_joined, cb)):
        for tt, k, fs, codes in src(d, rng=random.Random(mkdb.design_seed(d))):
            c = out.setdefault((tt, k), ([], []))
            (c[0].append((fs, codes)) if fs else c[1].append(codes))
    assert list(ca) == list(cb)
    for key in ca:
        t = time.time()
        a = mkdb._encode_chunk(*ca[key], col.rmap.names)
        t1 = time.time()
        b = mkdb._encode_chunk_joined(*cb[key], col.rmap.names)
        t2 = time.time()
        ta += t1 - t
        tb += t2 - t1
        nch += 1
        if a != b:
            print('DIFFERENT', d, key)
            sys.exit(1)
    print(os.path.basename(d), 'SAME', flush=True)
print(f'ALL SAME {nch} chunks; encode sets {ta:.1f} s, joined {tb:.1f} s')
