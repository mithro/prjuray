#!/usr/bin/env python3
"""Extract the MMCM / PLL lock and loop filter lookup tables from the
Xilinx XAPP888 dynamic reconfiguration headers (mmcm_pll_drp_func_*.vh,
as shipped with the Clocking Wizard IP) into clockgen_tables.json.

Bitgen programs these table entries (indexed by the integer feedback
multiplier and, for the filter, the bandwidth) into the clock generator's
configuration; features.py turns them into per-bit features.

  clockgen_tables.py <dir with the .vh files>
"""
import json
import os
import re
import sys

FAMILIES = {
    '7s_mmcm': 'mmcm_pll_drp_func_7s_mmcm.vh',
    '7s_pll': 'mmcm_pll_drp_func_7s_pll.vh',
    'us_mmcm': 'mmcm_pll_drp_func_us_mmcm.vh',
    'us_pll': 'mmcm_pll_drp_func_us_pll.vh',
    'usp_mmcm': 'mmcm_pll_drp_func_us_plus_mmcm.vh',
    'usp_pll': 'mmcm_pll_drp_func_us_plus_pll.vh',
}


def parse(text):
    """{table name: [entry for divide 1, 2, ...]} of one header."""
    tables = {}
    for fn in re.finditer(r'function\s*\[[^\]]*\]\s*(\w+)(.*?)endfunction',
                          text, re.S):
        name, body = fn.groups()
        kind = {'mmcm_pll_lock_lookup': 'lock',
                'mmcm_pll_filter_lookup': 'filter'}.get(name)
        if kind is None:
            continue
        for m in re.finditer(r'(\w+)\s*=\s*\{(.*?)\};', body, re.S):
            var, lits = m.groups()
            vals = [int(v.replace('_', ''), 2) for v in
                    re.findall(r"\d+'b([01_]+)", lits)]
            if not vals:
                continue
            suffix = var[len('lookup'):].lstrip('_')
            # Concatenation: the first entry is the most significant and is
            # selected by divide == 1.
            tables[kind + ('_' + suffix if suffix else '')] = vals
    return tables


def main():
    src = sys.argv[1]
    out = {}
    for fam, fname in FAMILIES.items():
        with open(os.path.join(src, fname)) as f:
            out[fam] = parse(f.read())
        print(fam, {k: len(v) for k, v in out[fam].items()})
    dst = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'clockgen_tables.json')
    with open(dst, 'w') as f:
        json.dump(out, f, indent=0, sort_keys=True)
        f.write('\n')


if __name__ == '__main__':
    main()
