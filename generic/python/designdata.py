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


# Per design feature cache: tile_features() of a design, pickled, next to
# the build's designs (<build>/cache/features/<die>/<tag>/<sN>.<code>.pkl,
# the build of the design directory's real path, so scratch trees of
# symlinked designs share it; <code> is the feature code checksum, so
# checkouts with different feature code do not overwrite each other's
# entries: stale ones can be deleted at any time).  The stamp covers the dump (mtime, size), the feature
# code (features.py, clockgen_tables.json, the I/O standard table from
# gen_design.py) and the site keys, so a tile grid change (a new sample
# cache) reuses the features and only a feature code change recomputes
# them.  URAY_FEATURE_CACHE=0 disables it.  (pickle: like mkdb's sample
# cache, a private cache of the build tree written only here.)
FEATURE_CACHE_VERSION = 1
_CODE = {}


def _feature_code_stamp(sitekeys):
    key = id(sitekeys)
    if key not in _CODE:
        import pickle
        import zlib
        here = os.path.dirname(os.path.abspath(featlib.__file__))
        crc = FEATURE_CACHE_VERSION
        for name in ('features.py', 'clockgen_tables.json'):
            p = os.path.join(here, name)
            if os.path.exists(p):
                with open(p, 'rb') as f:
                    crc = zlib.crc32(f.read(), crc)
        crc = zlib.crc32(repr(sorted(featlib._std_vcco().items())).encode(),
                         crc)
        # data files the features use (INT node maps) and feature switches
        data = os.path.join(os.path.dirname(here), 'data')
        if os.path.isdir(data):
            for name in sorted(os.listdir(data)):
                with open(os.path.join(data, name), 'rb') as f:
                    crc = zlib.crc32(f.read(), crc)
        crc = zlib.crc32(os.environ.get('URAY_PARK', '0').encode(), crc)
        sk = zlib.crc32(pickle.dumps(
            {k: v for k, v in vars(sitekeys).items()},
            protocol=pickle.HIGHEST_PROTOCOL))
        _CODE.clear()
        _CODE[key] = (crc, sk)
    return _CODE[key]


def feature_cache_path(d, sitekeys):
    real = os.path.realpath(d)
    parts = real.split(os.sep)
    if len(parts) < 5 or parts[-4] != 'designs':
        return None
    code = _feature_code_stamp(sitekeys)[0]
    return os.path.join(os.sep.join(parts[:-4]), 'cache', 'features',
                        *parts[-3:]) + f'.{code:08x}.pkl'


def load_features(d, sitekeys):
    """{tile: set(features)} of a design (cached, see above)."""
    import pickle
    import zlib
    src = os.path.join(d, 'design.features.gz')
    path = None
    if os.environ.get('URAY_FEATURE_CACHE', '1') != '0':
        path = feature_cache_path(d, sitekeys)
    if path is None:
        return featlib.tile_features(src, sitekeys)
    st = os.stat(src)
    stamp = (FEATURE_CACHE_VERSION, _feature_code_stamp(sitekeys),
             st.st_mtime_ns, st.st_size)
    try:
        with open(path, 'rb') as f:
            if pickle.load(f) == stamp:
                return pickle.loads(zlib.decompress(f.read()))
    except (OSError, EOFError, pickle.UnpicklingError, ValueError,
            zlib.error):
        pass
    feats = featlib.tile_features(src, sitekeys)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'wb') as f:
            pickle.dump(stamp, f, protocol=pickle.HIGHEST_PROTOCOL)
            f.write(zlib.compress(pickle.dumps(
                dict(feats), protocol=pickle.HIGHEST_PROTOCOL), 1))
        os.replace(tmp, path)
    except OSError as e:
        print(f'# feature cache not written: {e}', flush=True)
    return feats
