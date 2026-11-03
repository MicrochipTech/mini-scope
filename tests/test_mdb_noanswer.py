"""test_mdb_noanswer.py - a simulator that never answers ends hw_check.py and run_script.py with a message.

    python tests/test_mdb_noanswer.py

MdbSerial is replaced by a function that raises MdbNoAnswer at once (no MPLAB X needed). Each tool must
print the message, write its report, exit 1 and show no traceback. The mutation runs the same cases on a
copy of the tools in which the catch is made unable to match: the cases must then fail.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
TMP = tempfile.mkdtemp(prefix='noanswer_')

DRIVER = '''
import argparse, os, sys
base, tool, report = sys.argv[1:4]
sys.path[:0] = [os.path.join(base, 'gui'), os.path.join(base, 'tools'), os.path.join(base, 'tests')]
import scope_gui as g
import mdb_serial
def dead(*a, **k):
    raise mdb_serial.MdbNoAnswer('the firmware in the simulator sent nothing within 90 s: test text')
mdb_serial.MdbSerial = dead
g.ARGS = argparse.Namespace(mdb='x.elf', sim=False)
if tool == 'hw_check':
    import hw_check
    sys.argv = ['hw_check.py', '--port', 'MDB', '--elf', 'x.elf', '--report', report]
    hw_check.main()
else:
    import run_script
    from elf_vars import VarTable
    from sim_serial import SYMBOLS, SYMBOL_TYPES
    run_script.make_table = lambda a: VarTable.from_symbols(SYMBOLS, SYMBOL_TYPES)
    sys.argv = ['run_script.py', os.path.join(base, 'scripts', 'tune_startup.py'), '--port', 'MDB', '--elf', 'x.elf',
                '--probe', '--report', report]
    run_script.main()
'''


def check(cond, text):
    print(('OK   ' if cond else 'FAIL ') + text)
    if not cond:
        check.failed = True


check.failed = False


def run(base, tool):
    report = os.path.join(TMP, f'{tool}_report.txt')
    if os.path.exists(report):
        os.remove(report)
    drv = os.path.join(TMP, 'driver.py')
    with open(drv, 'w') as f:
        f.write(DRIVER)
    r = subprocess.run([sys.executable, drv, base, tool, report], capture_output=True, text=True, cwd=TMP, timeout=120)
    return r.returncode, r.stdout + r.stderr, report


def cases(base, label):
    for tool in ('hw_check', 'run_script'):
        code, text, report = run(base, tool)
        check(code == 1, f'{label} {tool}: exit code 1 (got {code})')
        check('sent nothing within 90 s' in text, f'{label} {tool}: message text is printed')
        check('Traceback' not in text, f'{label} {tool}: no traceback')
        check(os.path.isfile(report), f'{label} {tool}: report file written')


def mutant():
    base = os.path.join(TMP, 'mut')
    for d in ('gui', 'tools', 'tests', 'scripts'):
        shutil.copytree(os.path.join(ROOT, d), os.path.join(base, d), ignore=shutil.ignore_patterns('__pycache__', 'dist'))
    for name, old in (('hw_check.py', 'except TimeoutError as e:'), ('run_script.py', 'except TimeoutError as e:       # MdbNoAnswer\n')):
        p = os.path.join(base, 'tools', name)
        s = open(p, encoding='utf-8').read()
        assert old in s, (name, old)
        open(p, 'w', encoding='utf-8').write(s.replace('except TimeoutError', 'except ZeroDivisionError'))
    return base


if __name__ == '__main__':
    cases(ROOT, 'real')
    real_failed = check.failed
    print('--- mutation: the catch cannot match; the traceback and report checks must FAIL')
    check.failed = False
    cases(mutant(), 'mutant')
    mutant_failed = check.failed
    shutil.rmtree(TMP, ignore_errors=True)
    if real_failed:
        print('FAIL: the tools do not handle the dead simulator')
    elif not mutant_failed:
        print('FAIL: the mutant was not caught')
    else:
        print('OK   the mutant was caught')
    sys.exit(1 if real_failed or not mutant_failed else 0)
