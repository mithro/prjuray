"""eq_parse_digest.py: the parse cache key (designdata.code_digest of
features.parse_dump) must change with every edit of parse_dump or of what
it uses, and must not change with edits of derive() and its helpers."""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(os.path.dirname(os.path.dirname(HERE)), 'python')
sys.path.insert(0, PY)
import designdata as DD  # noqa: E402

src = open(os.path.join(PY, 'features.py')).read()
base = DD.code_digest(os.path.join(PY, 'features.py'), ['parse_dump'])


def digest_of(text):
    with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False) as f:
        f.write(text)
    try:
        return DD.code_digest(f.name, ['parse_dump'])
    finally:
        os.remove(f.name)


def edit_in(func, text):
    """text with an extra statement at the start of top level def func."""
    m = re.search(rf'^def {func}\(.*?\):\n(    """.*?"""\n)?', text,
                  re.M | re.S)
    assert m, func
    return text[:m.end()] + '    _eq_probe = 1\n' + text[m.end():]


bad = 0
# must change
for func in ('parse_dump', 'cfg_features', 'lut_eqn_bits', 'open_any'):
    ch = digest_of(edit_in(func, src)) != base
    print(f'edit {func:22s} parse key {"changes" if ch else "SAME (WRONG)"}')
    bad += not ch
for old, new in ((r"_VEC = re.compile(", r"_VEC = re.compile(r'x' or "),
                 ('MAX_ZERO_VEC = 64', 'MAX_ZERO_VEC = 32')):
    assert src.count(old) == 1, old
    ch = digest_of(src.replace(old, new)) != base
    print(f'edit {old[:22]:22s} parse key {"changes" if ch else "SAME (WRONG)"}')
    bad += not ch
# must not change
for func in ('derive', 'hclk_row_features', 'leaf_clock_features',
             'clockgen_drp_features'):
    ch = digest_of(edit_in(func, src)) != base
    print(f'edit {func:22s} parse key {"CHANGES (WRONG)" if ch else "same"}')
    bad += ch
print('OK' if not bad else f'{bad} WRONG')
sys.exit(1 if bad else 0)
