"""m7a_compile.py - card M7a, compile check: configuration default and the 18 combinations of UART and sample clock.

    python docs/evidence/m7a/m7a_compile.py <throwaway worktree>

Runs make with the generated files inside the throwaway worktree only. Nothing is programmed. The
combination comes in through the make variable MP_EXTRA_CC_PRE, the project stays as it is. Every
result is checked against the symbols of its ELF.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'm0'))
from m0_common import GNU, MPLABX, symbols                      # noqa: E402

TIMERS = ('SCOPE_T1', 'SCOPE_CCP1', 'SCOPE_CCP2', 'SCOPE_CCP3', 'SCOPE_CCP4', 'SCOPE_NONE')
ISR = {'SCOPE_T1': 'T1Interrupt', 'SCOPE_NONE': None}


def run(cmd, cwd):
    env = dict(os.environ, PATH=GNU + ';' + os.environ['PATH'])
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def build(x, flags):
    make = [GNU + r'\make.exe', '-f', 'Makefile', 'CONF=default']
    run(make + ['clean'], x)
    rc, out = run(make + ['build', f'MP_EXTRA_CC_PRE={flags}'], x)
    warnings = [line for line in out.splitlines() if ': warning:' in line or 'Warning:' in line]
    elf = os.path.join(x, 'dist', 'default', 'production', 'test.X.production.elf')
    return rc, warnings, elf, out


def main():
    x = os.path.join(sys.argv[1], 'test.X')
    rc, out = run([MPLABX + r'\mplab_platform\bin\prjMakefilesGenerator.bat', '.'], x)
    print('makefiles generated, exit', rc, flush=True)
    rc, warnings, elf, out = build(x, '')
    print(f'default (UART1, T1) exit {rc} warnings {len(warnings)} simulator macro in ELF: '
          f'{"sim_rx" in symbols(elf)}', flush=True)
    bad = 0
    for u in (1, 2, 3):
        for t in TIMERS:
            rc, warnings, elf, out = build(x, f'-DSCOPE_UART={u} -DSCOPE_TIMER={t}')
            ok = 'n/a'
            if rc == 0:
                sym = symbols(elf)
                want = [f'U{u}RXInterrupt', f'U{u}TXInterrupt']
                if t in ISR:
                    want += [ISR[t]] if ISR[t] else []
                else:
                    want += [f'CCT{t[-1]}Interrupt']
                ok = all(k in sym for k in want)
            bad += rc != 0 or ok is not True or len(warnings) != 0
            print(f'UART{u} {t:11} exit {rc} warnings {len(warnings)} vectors present {ok}', flush=True)
            for w in warnings[:3]:
                print('   ', w[-140:])
            if rc:
                print(out[-600:])
    print(f'{bad} combinations with an error, a warning or a missing vector')


if __name__ == '__main__':
    main()
