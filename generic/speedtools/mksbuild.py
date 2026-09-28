"""Scratch build dir snapshot for mkdb comparisons.
mksbuild.py <out> <arch> <dies> <tags> [<source build dir>]: scratch build tree (meta symlink, tile grids copied, ok v2 designs symlinked) for private mkdb/check experiments"""
import os, sys, shutil, glob
B = sys.argv[5] if len(sys.argv) > 5 else os.environ['URAY_BUILD']
out, arch, dies, tags = sys.argv[1], sys.argv[2], sys.argv[3].split(','), sys.argv[4].split(',')
os.makedirs(out, exist_ok=True)
if not os.path.exists(os.path.join(out, 'meta')):
    os.symlink(os.path.join(B, 'meta'), os.path.join(out, 'meta'))
n = 0
for d in dies:
    os.makedirs(os.path.join(out, 'db', arch, d), exist_ok=True)
    for f in glob.glob(os.path.join(B, 'db', arch, d, '*.json')):
        shutil.copy(f, os.path.join(out, 'db', arch, d))
    for t in tags:
        td = os.path.join(out, 'designs', d, t)
        os.makedirs(td, exist_ok=True)
        for s in glob.glob(os.path.join(B, 'designs', d, t, 's*')):
            if os.path.exists(os.path.join(s, 'bits.npz')) and os.path.exists(os.path.join(s, 'dump_v2')):
                dst = os.path.join(td, os.path.basename(s))
                if not os.path.exists(dst):
                    os.symlink(s, dst)
                n += 1
print('designs', n)
