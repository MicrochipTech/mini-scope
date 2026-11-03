"""test_mdb_close.py - MdbSerial.close() ends the whole simulator, also when the caller dies.

    python tests/test_mdb_close.py [ELF]

Needs MPLAB X and the ELF of test.X, configuration "sim" (default: test.X/dist/sim/production/...elf).
Two MDB starts of 40 to 60 s each. The process tree is recorded by PID while it runs; after the close
none of these PIDs may exist. Left-over processes are ended by PID.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ELF = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, '..', 'test.X', 'dist', 'sim', 'production', 'test.X.production.elf')
CHILD = '''
import sys
sys.path.insert(0, %r)
from mdb_serial import MdbSerial
m = MdbSerial(%r, progress=lambda f, t: None)
print("PID", m.p.pid, flush=True)
sys.stdin.readline()            # the test records the process tree, then sends a line or kills this process
if sys.argv[1] in ("close", "hung"):
    if sys.argv[1] == "hung":
        m._cmd = lambda c: None     # MDB does not react to Halt and Quit
    m.close()
    print("CLOSED", flush=True)
'''


def table():
    out = subprocess.run(['powershell', '-NoProfile', '-Command',
                          'Get-CimInstance Win32_Process | Select ProcessId,ParentProcessId | ConvertTo-Json'],
                         capture_output=True, text=True).stdout
    return {x['ProcessId']: x['ParentProcessId'] for x in json.loads(out)}


def tree(root):
    t = table()
    found, todo = set(), [root]
    while todo:
        p = todo.pop()
        for pid, parent in t.items():
            if parent == p and pid not in found:
                found.add(pid)
                todo.append(pid)
    return found


def alive(pids):
    t = table()
    return sorted(p for p in pids if p in t)


def check(cond, text):
    print(('OK   ' if cond else 'FAIL ') + text)
    if not cond:
        check.failed = True


def run(mode):
    child = subprocess.Popen([sys.executable, '-c', CHILD % (os.path.join(HERE, '..', 'tools'), os.path.abspath(ELF)), mode],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    cmd_pid, seen = None, set()
    for line in child.stdout:
        if line.startswith('PID'):
            cmd_pid = int(line.split()[1])
            seen = tree(cmd_pid) | {cmd_pid}
            if mode != 'kill':
                child.stdin.write('go' + chr(10))
                child.stdin.flush()
            if mode == 'kill':
                break
        if line.startswith('CLOSED'):
            break
    return child, seen


check.failed = False
for mode in ('close', 'hung', 'kill'):
    t0 = time.time()
    child, seen = run(mode)         # seen: the PIDs below cmd.exe while MDB runs (the PID line comes after start-up)
    if mode == 'kill':
        subprocess.run(['taskkill', '/F', '/PID', str(child.pid)], capture_output=True)    # the hard kill: no /T
    child.wait(60)
    time.sleep(5)
    left = alive(seen)
    for p in left:
        subprocess.run(['taskkill', '/F', '/PID', str(p)], capture_output=True)
    check(len(seen) >= 2, f'{mode}: {len(seen)} processes (cmd and below) while MDB ran')
    check(not left, f'{mode}: processes of the simulator left {time.time() - t0:.0f} s after the start: {left}')
print('FAILED' if check.failed else 'all passed')
sys.exit(1 if check.failed else 0)
