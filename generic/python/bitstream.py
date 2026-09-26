#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Architecture independent Xilinx 7-series / UltraScale / UltraScale+
bitstream reader.

Frames are returned as a dict {frame_address: numpy.uint32 array}.  Frame data
written through FDRI is assigned to frame addresses using the device's list of
valid frame addresses (see ``frame_list``); two zero padding frames follow
every change of block type / half / row, and the end of the write.
"""
import struct

import numpy as np

REG_CRC, REG_FAR, REG_FDRI, REG_CMD, REG_IDCODE, REG_MFWR = 0, 1, 2, 4, 12, 10
CMD_WCFG = 1

ARCHES = {
    'Series7': dict(words=101),
    'UltraScale': dict(words=123),
    'UltraScalePlus': dict(words=93),
}

ARCH_OF_FAMILY = {
    'artix7': 'Series7',
    'kintex7': 'Series7',
    'spartan7': 'Series7',
    'zynq': 'Series7',
    'kintexu': 'UltraScale',
    'kintexuplus': 'UltraScalePlus',
    'zynquplus': 'UltraScalePlus',
    'artixuplus': 'UltraScalePlus',
}


def far_fields(arch, far):
    """Returns (block, half, row, column, minor)."""
    if arch == 'UltraScalePlus':
        return ((far >> 24) & 7, 0, (far >> 18) & 0x3f, (far >> 8) & 0x3ff,
                far & 0xff)
    if arch == 'UltraScale':
        return ((far >> 23) & 7, 0, (far >> 17) & 0x3f, (far >> 7) & 0x3ff,
                far & 0x7f)
    return ((far >> 23) & 7, (far >> 22) & 1, (far >> 17) & 0x1f,
            (far >> 7) & 0x3ff, far & 0x7f)


def make_far(arch, block, half, row, col, minor):
    if arch == 'UltraScalePlus':
        return (block << 24) | (row << 18) | (col << 8) | minor
    if arch == 'UltraScale':
        return (block << 23) | (row << 17) | (col << 7) | minor
    return (block << 23) | (half << 22) | (row << 17) | (col << 7) | minor


def is_ecc_bit(arch, word, bit):
    if arch == 'Series7':
        return word == 50 and bit <= 12
    if arch == 'UltraScale':
        return word == 60 or (word == 61 and bit <= 15)
    return word == 45 or (word == 46 and bit <= 15)


def ecc_mask(arch):
    """uint32 array: 1 bits are ECC bits."""
    words = ARCHES[arch]['words']
    m = np.zeros(words, dtype=np.uint32)
    for w in range(words):
        for b in range(32):
            if is_ecc_bit(arch, w, b):
                m[w] |= np.uint32(1 << b)
    return m


def read_words(path):
    data = open(path, 'rb').read()
    sync = data.find(b'\xaa\x99\x55\x66')
    if sync < 0:
        raise ValueError(f'{path}: no sync word')
    body = data[sync + 4:]
    body = body[:len(body) // 4 * 4]
    return np.frombuffer(body, dtype='>u4').astype(np.uint32)


def iter_packets(words):
    """Yields (opcode, register, payload numpy array)."""
    i = 0
    n = len(words)
    last_reg = None
    while i < n:
        h = int(words[i])
        i += 1
        typ = h >> 29
        if typ == 1:
            op = (h >> 27) & 3
            reg = (h >> 13) & 0x3fff
            cnt = h & 0x7ff
            last_reg = reg
        elif typ == 2:
            op = (h >> 27) & 3
            reg = last_reg
            cnt = h & 0x7ffffff
        else:
            # Not a packet (e.g. a second sync word or dummy), skip.
            continue
        payload = words[i:i + cnt]
        i += cnt
        yield op, reg, payload
        if reg == REG_CMD and op == 2 and cnt and int(payload[0]) == 0xd:
            # DESYNC
            break


def frames_explicit(path, arch):
    """Frames from a per-frame-CRC bitstream: each FDRI is followed by a FAR
    write naming the frame just written, so no device knowledge is required.
    Returns (idcode, frames, ordered frame address list)."""
    words = read_words(path)
    wpf = ARCHES[arch]['words']
    frames = {}
    order = []
    pending = None
    idcode = None
    for op, reg, payload in iter_packets(words):
        if op != 2:
            continue
        if reg == REG_IDCODE and len(payload):
            idcode = int(payload[0])
        elif reg == REG_FDRI and len(payload) >= wpf:
            pending = payload[:wpf].copy()
        elif reg == REG_FAR and len(payload) and pending is not None:
            # The FAR written after an FDRI names the frame just written.
            far = int(payload[0])
            if far not in frames:
                frames[far] = pending
                order.append(far)
            pending = None
    return idcode, frames, order


def frames_sequential(path, arch, frame_list):
    """Frames from a normal bitstream using the device's ordered frame list."""
    words = read_words(path)
    wpf = ARCHES[arch]['words']
    order = {f: i for i, f in enumerate(frame_list)}
    frames = {}
    far = 0
    for op, reg, payload in iter_packets(words):
        if op != 2:
            continue
        if reg == REG_FAR and len(payload):
            far = int(payload[0])
        elif reg == REG_FDRI:
            nfr = len(payload) // wpf
            idx = order.get(far)
            if idx is None:
                raise ValueError(f'FDRI at unknown FAR {far:08x}')
            k = 0
            while k < nfr and idx < len(frame_list):
                cur = frame_list[idx]
                frames[cur] = payload[k * wpf:(k + 1) * wpf].copy()
                k += 1
                idx += 1
                if idx < len(frame_list):
                    a = far_fields(arch, cur)
                    b = far_fields(arch, frame_list[idx])
                    if a[:3] != b[:3]:
                        k += 2  # padding frames
            far = frame_list[idx] if idx < len(frame_list) else far
    return frames


def set_bits(frames, arch, skip_ecc=True):
    """Returns numpy structured arrays of (far, word, bit) for set bits."""
    m = ecc_mask(arch) if skip_ecc else None
    fars, wds, bts = [], [], []
    for far, d in frames.items():
        if m is not None:
            d = d & ~m
        nz = np.nonzero(d)[0]
        for w in nz:
            v = int(d[w])
            while v:
                lb = v & -v
                b = lb.bit_length() - 1
                fars.append(far)
                wds.append(int(w))
                bts.append(b)
                v ^= lb
    return np.array(fars, dtype=np.uint32), np.array(
        wds, dtype=np.uint16), np.array(bts, dtype=np.uint8)
