#!/usr/bin/env python3
# Copyright 2020-2026 F4PGA Authors
# SPDX-License-Identifier: Apache-2.0
"""Export the generated database into database/generic/:

  database/generic/<arch>/segbits_<type>.db, segbits_opt_<type>.db,
      defaults_<type>.db, registers.db, registers.txt
  database/generic/<arch>/<die>/tilegrid.json
  database/generic/devices.json   device/part -> {arch, die}
"""
import argparse
import glob
import json
import os
import shutil

import dies as dieslib


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out',
                    default=os.path.join(dieslib.URAY_DIR, 'database',
                                         'generic'))
    args = ap.parse_args()
    src = dieslib.DB
    devices = {}
    for die in dieslib.load().values():
        tg = os.path.join(src, die.arch, die.name, 'tilegrid.json')
        if not os.path.exists(tg):
            print('no tilegrid for', die.name)
            continue
        dst = os.path.join(args.out, die.arch, die.name)
        os.makedirs(dst, exist_ok=True)
        shutil.copy(tg, os.path.join(dst, 'tilegrid.json'))
        for dev in die.devices:
            devices[dev] = dict(arch=die.arch, die=die.name)
        for part in die.parts:
            devices[part] = dict(arch=die.arch, die=die.name)
    for arch in ('Series7', 'UltraScale', 'UltraScalePlus'):
        d = os.path.join(src, arch)
        if not os.path.isdir(d):
            continue
        dst = os.path.join(args.out, arch)
        os.makedirs(dst, exist_ok=True)
        for pat in ('segbits_*.db', 'defaults_*.db', 'registers.db',
                    'registers.txt'):
            for f in glob.glob(os.path.join(d, pat)):
                shutil.copy(f, dst)
    with open(os.path.join(args.out, 'devices.json'), 'w') as f:
        json.dump(devices, f, indent=1, sort_keys=True)
    shutil.copy(os.path.join(dieslib.URAY_DIR, 'generic', 'README.md'),
                os.path.join(args.out, 'README.md'))
    print('exported', len(devices), 'devices/parts to', args.out)


if __name__ == '__main__':
    main()
