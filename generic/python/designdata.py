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


def load_features(d, sitekeys):
    return featlib.tile_features(os.path.join(d, 'design.features.gz'),
                                 sitekeys)
