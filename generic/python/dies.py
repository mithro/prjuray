#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Registry of dies (groups of Vivado devices sharing one tile grid)."""
import json
import os

URAY_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
BUILD = os.environ.get('URAY_BUILD', os.path.join(URAY_DIR, 'build'))
META = os.path.join(BUILD, 'meta')
# Database root (<DB>/<arch>/...), e.g. an experiment directory.
DB = os.environ.get('URAY_DB', os.path.join(BUILD, 'db'))

ARCH_OF_FAMILY = {
    'artix7': 'Series7',
    'kintex7': 'Series7',
    'spartan7': 'Series7',
    'zynq': 'Series7',
    'kintexu': 'UltraScale',
    'kintexuplus': 'UltraScalePlus',
    'zynquplus': 'UltraScalePlus',
    'artixuplus': 'UltraScalePlus',
    'virtexuplus': 'UltraScalePlus',
    'virtexuplusHBM': 'UltraScalePlus',
    'virtexuplus58g': 'UltraScalePlus',
}


class DieInfo:
    def __init__(self, d):
        self.name = d['devices'][0]['device']
        self.part = d['devices'][0]['part']
        self.family = d['arch']
        self.arch = ARCH_OF_FAMILY[self.family]
        self.devices = [x['device'] for x in d['devices']]
        # Every part (package / speed grade) of the die's devices.
        self.parts = []
        devs = set(self.devices)
        for line in open(os.path.join(META, 'parts.txt')):
            p = line.split()
            if p[3] in devs:
                self.parts.append(p[0])
        if not self.parts:
            self.parts = [x['part'] for x in d['devices']]

    @property
    def tiles_tsv(self):
        return os.path.join(META, 'tiles', self.name + '.tsv')

    @property
    def types_txt(self):
        return os.path.join(META, 'types', self.name + '.txt')

    @property
    def prims_txt(self):
        return os.path.join(META, 'prims', self.arch + '.txt')

    @property
    def base_dir(self):
        return os.path.join(META, 'base', self.name)


def part_resources():
    """part -> dict(lut, ff, dsp, bram, gt, io, uram)"""
    out = {}
    path = os.path.join(META, 'part_resources.txt')
    for line in open(path):
        p = line.split()
        out[p[0]] = dict(zip(('lut', 'ff', 'dsp', 'bram', 'gt', 'io', 'uram'),
                             map(int, p[1:])))
    return out


def fuzz_parts(die):
    """Parts to fuzz a die with: those of its largest device (all fabric
    resources enabled), in every package."""
    res = part_resources()
    def key(p):
        r = res.get(p, {})
        return (r.get('lut', 0), r.get('dsp', 0), r.get('bram', 0),
                r.get('uram', 0))
    best = max(key(p) for p in die.parts)
    return [p for p in die.parts if key(p) == best]


def load():
    with open(os.path.join(META, 'die_groups.json')) as f:
        groups = json.load(f)
    return {g['devices'][0]['device']: DieInfo(g) for g in groups}


def die_of_device(device):
    for d in load().values():
        if device in d.devices:
            return d
    return None
