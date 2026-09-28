#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Loading of fuzzing design results (bits + features) for a die."""
import glob
import os

import numpy as np

import bitstream
import features as featlib


class DieFrames:
    """Frame address list + baseline for a die."""

    def __init__(self, die):
        self.die = die
        self.arch = die.arch
        self.wpf = bitstream.ARCHES[die.arch]['words']
        idcode, frames, order = bitstream.frames_explicit(
            os.path.join(die.base_dir, 'baseline_crc.bit'), die.arch)
        self.idcode = idcode
        self.frames = order
        self.index = {f: i for i, f in enumerate(order)}
        far, word, bit = bitstream.set_bits(frames, die.arch)
        self.base = self.bit_ids(far, word, bit)

    def bit_ids(self, far, word, bit):
        """Global bit id = (frame index * words + word) * 32 + bit."""
        fi = np.array([self.index[int(f)] for f in far], dtype=np.int64) \
            if len(far) else np.zeros(0, dtype=np.int64)
        return (fi * self.wpf + word.astype(np.int64)) * 32 + bit.astype(
            np.int64)

    def decode_id(self, bid):
        cell, bit = divmod(int(bid), 32)
        fi, word = divmod(cell, self.wpf)
        return self.frames[fi], word, bit


DUMP_V2 = 'dump_v2'  # marker: feature dump includes routing-only sites


def design_dirs(roots, v2only=False):
    """Completed design directories under one or more (comma separated or
    list) roots.  v2only: only designs with the current feature dump."""
    if isinstance(roots, str):
        roots = roots.split(',')
    out = []
    for root in roots:
        out += sorted(
            d for d in glob.glob(os.path.join(root, 's*'))
            if os.path.exists(os.path.join(d, 'bits.npz')) and (
                not v2only or os.path.exists(os.path.join(d, DUMP_V2))))
    return out


def load_bits(dframes, d):
    z = np.load(os.path.join(d, 'bits.npz'))
    far = z['far']
    # Frame index lookup vectorised through a sorted table.
    keys = np.array(dframes.frames, dtype=np.uint32)
    order = np.argsort(keys)
    sk = keys[order]
    pos = np.searchsorted(sk, far)
    if len(far) and (pos.max() >= len(sk) or not np.array_equal(sk[pos], far)):
        raise ValueError(f'{d}: frame address not in the die frame list')
    fi = order[pos].astype(np.int64)
    return np.sort((fi * dframes.wpf + z['word'].astype(np.int64)) * 32 +
                   z['bit'].astype(np.int64))


# Per design feature caches, next to the build's designs (the build of the
# design directory's real path, so scratch trees of symlinked designs
# share them; pickle: private caches of the build tree written only here;
# stale files can be deleted at any time; URAY_FEATURE_CACHE=0 disables
# both):
#
# * <build>/cache/features/<die>/<tag>/<sN>.<code>.pkl: tile_features() of
#   the design.  Stamp: the dump (mtime, size) and feature_inputs_stamp()
#   (feature code, data files, the die's site files, STAMP_ENV switches).
#   <code> (that checksum) in the name: checkouts with different feature
#   code do not overwrite each other's entries.
# * <build>/cache/parse/<die>/<tag>/<sN>.<parse code>.pkl: parse_dump() of
#   the design (the dump read into features and BEL settings, ~80% of
#   tile_features).  Its code checksum covers only parse_dump and what it
#   uses (code_digest), so an edit of the derived features (derive() and
#   its helpers) reuses it and only reruns derive().
FEATURE_CACHE_VERSION = 3  # 2: joined feature strings, lz4; 3: sorted
_CODE = {}


def code_digest(path, roots):
    """Checksum of the source of the top level functions / classes /
    assignments named roots in the module at path and, transitively, of
    every top level name they refer to."""
    import ast
    import hashlib
    src = open(path).read()
    tree = ast.parse(src)
    top = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef,
                             ast.AsyncFunctionDef)):
            top.setdefault(node.name, []).append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else \
                [node.target]
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        top.setdefault(n.id, []).append(node)
    seen, todo, parts = set(), list(roots), []
    while todo:
        name = todo.pop()
        if name in seen or name not in top:
            continue
        seen.add(name)
        for node in top[name]:
            parts.append(f'{name}:{ast.get_source_segment(src, node)}')
            for n in ast.walk(node):
                if isinstance(n, ast.Name) and n.id in top:
                    todo.append(n.id)
    h = hashlib.blake2b(digest_size=8)
    for x in sorted(parts):
        h.update(x.encode() + b'\0')
    return int(h.hexdigest(), 16) & 0xFFFFFFFF


def feature_inputs_stamp(tiles_tsv):
    """Checksum of everything tile_features() depends on besides the dump:
    features.py and clockgen_tables.json, every file under generic/data/,
    the die's tiles_tsv and bonded pad list, the I/O standard VCCO table
    (gen_design.py), the environment variables named in
    features.STAMP_ENV and the files returned by features.stamp_files(
    tiles_tsv) (hooks for feature code reading other inputs).  Cached per
    tiles_tsv."""
    key = ('inputs', tiles_tsv)
    if key not in _CODE:
        import glob
        import zlib
        here = os.path.dirname(os.path.abspath(featlib.__file__))
        files = [os.path.join(here, 'features.py'),
                 os.path.join(here, 'clockgen_tables.json'), tiles_tsv,
                 os.path.join(os.path.dirname(os.path.dirname(tiles_tsv)),
                              'bonded',
                              os.path.basename(tiles_tsv)[:-4] + '.txt')]
        files += sorted(glob.glob(os.path.join(os.path.dirname(here), 'data',
                                               '**', '*'), recursive=True))
        hook = getattr(featlib, 'stamp_files', None)
        if hook:
            files += list(hook(tiles_tsv))
        crc = FEATURE_CACHE_VERSION
        for p in files:
            crc = zlib.crc32(p.encode(), crc)
            if os.path.isfile(p):
                with open(p, 'rb') as f:
                    crc = zlib.crc32(f.read(), crc)
        crc = zlib.crc32(repr(sorted(featlib._std_vcco().items())).encode(),
                         crc)
        for k in getattr(featlib, 'STAMP_ENV', ()):
            crc = zlib.crc32(f'{k}={os.environ.get(k)}'.encode(), crc)
        _CODE[key] = crc
    return _CODE[key]


def _feature_code_stamp(sitekeys):
    """(feature inputs crc, 0, parse code crc, tiles_tsv crc) of a
    SiteKeys (cached).  parse_dump() reads only the dump and the site key
    map (from tiles_tsv): its cache key is its source digest and the
    tiles_tsv content."""
    key = ('code', sitekeys.tiles_tsv)
    if key not in _CODE:
        import zlib
        here = os.path.dirname(os.path.abspath(featlib.__file__))
        pcode = code_digest(os.path.join(here, 'features.py'),
                            ['parse_dump'])
        with open(sitekeys.tiles_tsv, 'rb') as f:
            pkey = zlib.crc32(f.read())
        _CODE[key] = (feature_inputs_stamp(sitekeys.tiles_tsv), 0, pcode,
                      pkey)
    return _CODE[key]


def _cache_path(d, kind, code):
    real = os.path.realpath(d)
    parts = real.split(os.sep)
    if len(parts) < 5 or parts[-4] != 'designs':
        return None
    return os.path.join(os.sep.join(parts[:-4]), 'cache', kind,
                        *parts[-3:]) + f'.{code:08x}.pkl'


def feature_cache_path(d, sitekeys):
    return _cache_path(d, 'features', _feature_code_stamp(sitekeys)[0])


try:
    import lz4.frame as _lz4
except ImportError:  # zlib instead (slower)
    _lz4 = None


def _pack_feats(feats):
    """{tile: set} -> {tile: '\\n'.join(sorted)} (pickling millions of small
    strings is slow; sorted: a canonical form, see load_features_joined)."""
    return {t: '\n'.join(sorted(fs)) for t, fs in feats.items()}


def _unpack_feats(packed):
    import collections
    out = collections.defaultdict(set)
    for t, v in packed.items():
        out[t] = set(v.split('\n')) if v else set()
    return out


def _load(path, stamp):
    """(the object saved with this stamp) or None."""
    import pickle
    import zlib
    try:
        with open(path, 'rb') as f:
            if pickle.load(f) != stamp:
                return None
            kind = pickle.load(f)
            data = f.read()
        data = _lz4.decompress(data) if kind == 'lz4' else \
            zlib.decompress(data)
        return pickle.loads(data)
    except (OSError, EOFError, pickle.UnpicklingError, ValueError,
            zlib.error, RuntimeError, AttributeError):
        return None


def _save(path, stamp, obj):
    import pickle
    import zlib
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f'{path}.{os.getpid()}.tmp'
        data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
        with open(tmp, 'wb') as f:
            pickle.dump(stamp, f, protocol=pickle.HIGHEST_PROTOCOL)
            if _lz4 is not None:
                pickle.dump('lz4', f)
                f.write(_lz4.compress(data, compression_level=0))
            else:
                pickle.dump('zlib', f)
                f.write(zlib.compress(data, 1))
        os.replace(tmp, path)
    except OSError as e:
        print(f'# feature cache not written: {e}', flush=True)


def load_features(d, sitekeys):
    """{tile: set(features)} of a design (cached, see above)."""
    packed, feats = _features(d, sitekeys)
    return feats if feats is not None else _unpack_feats(packed)


def load_features_joined(d, sitekeys):
    """{tile: '\\n'.join(sorted features)} of a design (cached; a canonical
    form of load_features, cheap to compare and hash)."""
    packed, feats = _features(d, sitekeys)
    return packed if packed is not None else _pack_feats(feats)


def _features(d, sitekeys):
    """(joined features or None, feature sets or None) of a design."""
    src = os.path.join(d, 'design.features.gz')
    path = None
    if os.environ.get('URAY_FEATURE_CACHE', '1') != '0':
        path = feature_cache_path(d, sitekeys)
    if path is None:
        return None, featlib.tile_features(src, sitekeys)
    code = _feature_code_stamp(sitekeys)
    st = os.stat(src)
    stamp = (FEATURE_CACHE_VERSION, code[:2], st.st_mtime_ns, st.st_size)
    packed = _load(path, stamp)
    if packed is not None:
        return packed, None
    # The parsed dump (reused across edits of the derived features).
    ppath = _cache_path(d, 'parse', code[2])
    pstamp = (FEATURE_CACHE_VERSION, code[2:], st.st_mtime_ns, st.st_size)
    state = _load(ppath, pstamp)
    if state is None:
        state = featlib.parse_dump(src, sitekeys)
        _save(ppath, pstamp, (_pack_feats(state[0]),) + tuple(state[1:]))
    else:
        state = (_unpack_feats(state[0]),) + tuple(state[1:])
    feats = featlib.derive(state, sitekeys)
    packed = _pack_feats(feats)
    _save(path, stamp, packed)
    return packed, None
