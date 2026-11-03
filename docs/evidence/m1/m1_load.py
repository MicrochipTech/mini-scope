"""m1_load.py - card 1.1: length of the model interrupt in the MPLAB X simulator.

    python docs/evidence/m1/m1_load.py

Builds a throwaway copy of the firmware in the temp folder (the worktree is not
touched) in which the model interrupt reads SCCP2 (free running, FP/4) at its
start and end, with the stream running. Starts the motor, lets it run in
closed loop, halts and prints the counters with MDB.

Reported lengths are the body of the interrupt including the TX kick of the
simulator build, without the hardware entry and exit overhead (about 20 cycles).
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'm0'))
from m0_common import (Frames, GNU, MPLABX, MdbSerial, mdb_lines, patch, sh, start,  # noqa: E402
                       symbols, write_cmd)

REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
BUILD = os.path.join(tempfile.gettempdir(), 'mini_scope_m1_load')
FCY = 32e6


def build():
    shutil.rmtree(BUILD, ignore_errors=True)
    os.makedirs(BUILD)
    shutil.copytree(os.path.join(REPO, 'firmware'), os.path.join(BUILD, 'firmware'))
    shutil.copytree(os.path.join(REPO, 'test.X'), os.path.join(BUILD, 'test.X'),
                    ignore=shutil.ignore_patterns('build', 'dist', 'debug', '.generated_files', 'Makefile-*',
                                                  '*.log'))
    patch(os.path.join(BUILD, 'firmware', 'example_main.c'), [
        ('void __attribute__((interrupt, no_auto_psv)) TUNE_ISR(void)\n{\n    TUNE_IF = 0;',
         'volatile uint16_t ld_fast_max, ld_slow_max, ld_fast_last, ld_slow_last;\n'
         'volatile uint16_t ld_s0, ld_s1, ld_s2, ld_s3;\n'
         'volatile uint32_t ld_sum, ld_n, ld_slow_n;\n'
         'void __attribute__((interrupt, no_auto_psv)) TUNE_ISR(void)\n{\n'
         '    uint16_t t0 = CCP2TMRL, d;\n    TUNE_IF = 0;'),
        ('    tune_model_step();\n}',
         '    tune_model_step();\n    d = CCP2TMRL - t0;\n'
         '    ld_sum += d;\n    ld_n++;\n'
         '    if (tune_div == 0) {\n        ld_slow_last = d;\n        ld_slow_n++;\n'
         '        if (d > ld_slow_max) ld_slow_max = d;\n'
         '        if (motor.state == 0 && d > ld_s0) ld_s0 = d;\n        if (motor.state == 1 && d > ld_s1) ld_s1 = d;\n'
         '        if (motor.state == 2 && d > ld_s2) ld_s2 = d;\n        if (motor.state == 3 && d > ld_s3) ld_s3 = d;\n'
         '    } else {\n        ld_fast_last = d;\n'
         '        if (d > ld_fast_max) ld_fast_max = d;\n    }\n}'),
        ('    TUNE_CON1Lbits.CCPON = 1;\n}',
         '    TUNE_CON1Lbits.CCPON = 1;\n    CCP2CON1L = 0;\n    CCP2CON1H = 0;\n    CCP2CON2L = 0;\n'
         '    CCP2CON2H = 0;\n    CCP2CON1Lbits.TMRPS = 1;\n    CCP2PRL = 0xFFFF;\n    CCP2CON1Lbits.CCPON = 1;\n}'),
    ])
    x = os.path.join(BUILD, 'test.X')
    sh([MPLABX + r'\mplab_platform\bin\prjMakefilesGenerator.bat', '.'], x)
    sh([GNU + r'\make.exe', '-f', 'Makefile', 'CONF=sim', 'build'], x)
    return os.path.join(x, 'dist', 'sim', 'production', 'test.X.production.elf')


def printed(ser, name):
    ser._cmd(f'Print {name}')
    t = time.time()
    while time.time() - t < 8:
        lines = mdb_lines(ser, 0.3)
        for i, line in enumerate(lines):
            if line.endswith(name + '='):
                return int(lines[i + 1] if i + 1 < len(lines) else mdb_lines(ser, 1)[0])
    raise TimeoutError(name)


def main():
    elf = build()
    sym = symbols(elf)
    m = {n: sym['motor'] + 2 * i for i, n in enumerate(
        ['run', 'state', 'speed', 'speed_ref', 'iq', 'fault'])}
    ser = MdbSerial(elf, progress=lambda f, t: None)
    start(ser, [m['state'], m['speed'], m['iq'], m['fault']])
    fr = Frames(ser, 4)
    ser.write(write_cmd(m['run'], 1))
    t = time.time()
    seen = set()
    while time.time() - t < 400 and 3 not in seen:
        seen.update(v[0] for _, v in fr.poll())
        time.sleep(0.3)
    t = time.time()
    n0 = fr.index
    while fr.index - n0 < 600 and time.time() - t < 200:       # 600 ms more in closed loop
        fr.poll()
        time.sleep(0.3)
    ser._cmd('Halt')
    mdb_lines(ser, 3)
    vals = {n: printed(ser, n) for n in ('ld_fast_max', 'ld_slow_max', 'ld_fast_last', 'ld_slow_last',
                                         'ld_sum', 'ld_n', 'ld_slow_n', 'ld_s0', 'ld_s1', 'ld_s2', 'ld_s3', 'scope_overflows')}
    ser._cmd('Quit')
    ser.p.wait(20)
    print('states seen', sorted(seen), 'samples', fr.index, 'lost', fr.lost)
    print(vals)
    avg = 4 * vals['ld_sum'] / vals['ld_n']
    print(f"passes {vals['ld_n']} (slow {vals['ld_slow_n']})")
    print(f"fast pass max {4 * vals['ld_fast_max']} cycles, slow pass max {4 * vals['ld_slow_max']} cycles, "
          f"last {4 * vals['ld_fast_last']} / {4 * vals['ld_slow_last']}")
    print('slow pass max by state after the pass (cycles): ' + ', '.join(
        f"{i}: {4 * vals[f'ld_s{i}']}" for i in range(4)))
    print(f"average {avg:.0f} cycles = {100 * avg / (FCY / 10000):.1f} % of 100 us, "
          f"worst case pass {100 * 4 * vals['ld_slow_max'] / (FCY / 10000):.1f} %")


if __name__ == '__main__':
    main()
