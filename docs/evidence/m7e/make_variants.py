r"""make_variants.py - card M7e, builds the broken PC-model variants in scratch copies.

    python docs/evidence/m7e/make_variants.py <scratch folder>

Per variant a copy of the repository (without .git) is made below the scratch folder, one line of firmware/scope.c
is broken and tests/sim.dll is built from it with gcc. The worktree is not changed. Then, in each copy:

    python tests\test_protocol.py, tests\test_script.py, tests\test_faults.py, tests\test_adapt.py
    python tools\hw_check.py --port SIM --secs 5

wr16: the W command writes a 4 byte value with 16 bits.  wr32: it writes a 2 byte value with 32 bits.
edge: the rising and the falling trigger edge are exchanged.  base: unchanged.
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..'))
W4 = '    else if (w == 4)\n        *(volatile uint32_t *)p = v;'
W2 = '    else if (w == 2)\n        *(volatile uint16_t *)p = (uint16_t)v;'
RISE = '(trig_type == TRIG_RISE && trig_prev < trig_level && v >= trig_level)'
FALL = '(trig_type == TRIG_FALL && trig_prev > trig_level && v <= trig_level)'
VARIANTS = {
    'base': [],
    'wr16': [(W4, '    else if (w == 4)\n        *(volatile uint16_t *)p = (uint16_t)v;')],
    'wr32': [(W2, '    else if (w == 2)\n        *(volatile uint32_t *)p = v;')],
    'edge': [(RISE, RISE.replace('TRIG_RISE', 'TRIG_FALL')), (FALL, FALL.replace('TRIG_FALL', 'TRIG_RISE'))],
}


def main():
    scratch = os.path.abspath(sys.argv[1])
    for name, edits in VARIANTS.items():
        d = os.path.join(scratch, name)
        shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(ROOT, d, ignore=shutil.ignore_patterns('.git', '.venv', 'runs', '__pycache__'))
        p = os.path.join(d, 'firmware', 'scope.c')
        with open(p) as f:
            s = f.read()
        for old, new in edits:
            assert s.count(old) == 1, (name, old)
            s = s.replace(old, new)
        with open(p, 'w') as f:
            f.write(s)
        r = subprocess.run(['gcc', '-O2', '-shared', '-Itests/mock', '-Ifirmware', '-o', 'tests/sim.dll',
                            'tests/mock/sim.c', 'firmware/scope.c'], cwd=d)
        print(name, 'built' if r.returncode == 0 else 'gcc failed', flush=True)


if __name__ == '__main__':
    main()
