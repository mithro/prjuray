import os, re, sys, glob
base = sys.argv[1]
for d in sorted(glob.glob(base + '/s*')):
    def rd(f):
        p = os.path.join(d, f)
        return open(p, errors='replace').read() if os.path.exists(p) else ''
    reg = rd('design.region').strip().replace('\n', ' ')[:120]
    tcl = rd('design.tcl')
    fin = re.findall(r'nl_finish[^\n]*', tcl)
    st = rd('run.stats').strip().replace('\n', ' ')[:160]
    vl = rd('vivado.log')
    ov = re.findall(r'Number of Nodes with overlaps = (\d+)', vl)
    ncell = len(re.findall(r'create_cell|nl_cell', tcl))
    print(os.path.basename(d), '|', reg, '|', fin[-1:] , '|', st, '| overlaps', len(ov), ov[-6:], '| cells', ncell)
