"""test_script.py - gui/tune_api.py against the real scope.c and the virtual motor (sim.dll).

    python tests/test_script.py                 all tests
    python tests/test_script.py limit race      only the tests with these names (see TESTS at the end)
"""
import builtins
import csv
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import time
from unittest import mock

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, '..', 'gui'), os.path.join(HERE, '..', 'tools')]
import scope_gui                                  # noqa: E402
import script_tab                                 # noqa: E402
import tune_api                                   # noqa: E402
from elf_vars import VarTable                     # noqa: E402
from scope_gui import Scope                       # noqa: E402
from sim_serial import SimSerial, SYMBOLS, SYMBOL_TYPES   # noqa: E402

TABLE = VarTable.from_symbols(SYMBOLS, SYMBOL_TYPES)
FULL_TRIAL = tune_api.TRIAL_MS
ADDR = {n: a for n, (a, _) in SYMBOLS.items()}
RUN, RAMP, ALIVE, KP = ADDR['motor.run'], ADDR['startup.ramp_rate'], ADDR['tune_alive'], ADDR['motor.kp']
TMP = tempfile.mkdtemp(prefix='tune_test_')
threading.excepthook = lambda a: None           # Scope's reader dies with the port in the port error test

HEAD = '''from tune_api import *
import time


def setup():
    channels("motor.speed", "motor.state", "motor.fault", "motor.run")
    allow("motor.run", 0, 1)
    allow("startup.ramp_rate", 1, 500)
    allow("startup.align_current", 0, 1000)
    allow("startup.handover_speed", 100, 2000)
    allow("motor.kp", -32768, 3000)
    safe_value("motor.run", 0)


def start_motor():
    write("startup.align_current", 400)
    write("startup.handover_speed", 600)
    write("startup.ramp_rate", 100)
    write("motor.run", 1)
    n = 0
    while read("motor.state") != 3 and n < 200:
        wait_ms(10)
        n += 1
'''


def check(cond, text):
    print(('OK   ' if cond else 'FAIL ') + text)
    if not cond:
        check.failed = True


class LogSerial(SimSerial):
    """Logs at the port what the target receives: (kind, address, value), kind W, or a command byte."""

    def __init__(self):
        super().__init__()
        self.log = []

    def write(self, data):
        b = bytes(data)
        if b[:1] == b'W':
            for i in range(0, len(b), 8):
                addr, width = struct.unpack('<HB', b[i + 1:i + 4])
                self.log.append(('W', addr, int.from_bytes(b[i + 4:i + 4 + width], 'little', signed=False)))
        else:
            self.log.append((chr(b[0]), None, None))
        return super().write(b)

    def writes(self, addr=None):
        return [x for x in self.log if x[0] == 'W' and (addr is None or x[1] == addr)]


class NoisySerial(LogSerial):
    """pending: junk bursts inserted into the stream. drop: whole frames removed (counted as lost)."""
    pending = drop = on_start = 0

    def write(self, data):
        if bytes(data) == b'R':
            self.pending, self.on_start = self.on_start, 0      # the noise is for one start only, not for the read-back
        return super().write(data)

    def _advance(self):
        with self.lock:
            before = len(self.buf)
            super()._advance()
            if self.pending and len(self.buf) > before + 4:
                self.buf[(before + len(self.buf)) // 2:(before + len(self.buf)) // 2] = bytes(3)
                self.pending -= 1
            if self.drop and len(self.buf) > before:
                n = min(self.drop, (len(self.buf) - before) // 10)
                del self.buf[before:before + n * 10]
                self.drop -= n


class ChunkSerial(LogSerial):
    """Delivers what the target sends in bursts: nothing for `burst` seconds, then everything at once."""
    burst = 0.2
    time_scale = 5              # port timeouts wait for the bursts, as MdbSerial does with its 25

    def __init__(self):
        super().__init__()
        self.held = bytearray()
        self.t_out = time.perf_counter()
        self.bursts = False             # only while a stream runs: answers to commands come at once

    def write(self, data):
        c = bytes(data)[:1]
        if c == b'R':
            self.bursts = True
        elif c in (b'S', b'P', b'X'):
            self.bursts = False
        return super().write(data)

    def _advance(self):
        with self.lock:
            before = len(self.buf)
            super()._advance()
            self.held += self.buf[before:]
            del self.buf[before:]
            if not self.bursts or time.perf_counter() - self.t_out >= self.burst:
                self.buf += self.held
                self.held.clear()
                self.t_out = time.perf_counter()


class BadSerial(LogSerial):
    """silent: nothing comes back. broken: every access raises. hang: write blocks. drop_safe: W to
    motor.run with value 0 are swallowed this many times."""
    silent = broken = False
    drop_safe = 0

    def __init__(self):
        super().__init__()
        self.hang = threading.Event()
        self.release = threading.Event()

    def _bad(self):
        if self.broken:
            raise OSError('device disappeared')

    def read(self, n=1):
        self._bad()
        if self.silent:
            time.sleep(self.timeout)
            return b''
        return super().read(n)

    @property
    def in_waiting(self):
        self._bad()
        return 0 if self.silent else SimSerial.in_waiting.fget(self)

    def reset_input_buffer(self):
        self._bad()
        super().reset_input_buffer()

    def write(self, data):
        self._bad()
        if self.hang.is_set():
            self.release.wait(30)
        if self.drop_safe and bytes(data)[:1] == b'W' and struct.unpack('<H', bytes(data)[1:3])[0] == RUN \
                and bytes(data)[4] == 0:
            self.drop_safe -= 1
            return len(data)
        return super().write(data)


def go(body, ser=None, table=TABLE, hooks=None, during=None, trial=300, wait=30, name='t.py', on_event=None, script=None, render_png=None,
       head=HEAD):
    """Runs a script, returns session, result, Scope. body: the script without HEAD."""
    tune_api.TRIAL_MS = trial
    hooks = {'skip_checks': True, 'skip_probe': True, 'skip_build_id': True, **(hooks or {})}      # the small scripts have no check_idle(), tune_check has its own tests
    folder = tempfile.mkdtemp(dir=TMP)
    path = os.path.join(folder, name)
    if script:
        shutil.copy(script, path)
    else:
        with open(path, 'w') as f:
            f.write(head + body)
    ser = ser or LogSerial()
    sc = Scope(ser)
    sc.ping()
    ev = []

    def receive(e):
        ev.append(e)
        if on_event:
            on_event(e)

    s = tune_api.run(path, sc, table, on_event=receive, out_dir=os.path.join(folder, 'runs'), elf_name='x.elf', hooks=hooks, render_png=render_png)
    s.ev = ev
    s.path = path
    if during:
        during(s)
    r = s.wait(wait)
    if r is None:
        check(False, 'the run did not end in time')
        tune_api.abort('test timeout')
        s.wait(10)
        r = s.result or {'status': 'none', 'reason': '', 'errors': [], 'abort_ms': None, 'tuning_h': None}
    return s, r, sc


def wait_log(s, text, secs=20):
    end = time.time() + secs
    while time.time() < end:
        if text in logs(s):
            return True
        time.sleep(0.005)
    return False


def logs(s):
    return '\n'.join(e['text'] for e in list(s.ev) if e['type'] == 'log')


def run_txt(s):
    with open(os.path.join(s.folder, 'run.txt'), encoding='utf-8') as f:
        return f.read()


def peek(sc, *names):
    """Current values in the model, by a short stream."""
    if sc.start([(ADDR[n], SYMBOL_TYPES[n]) for n in names], 0, 1000):
        return None
    time.sleep(0.05)
    _, cols = sc.snapshot()
    sc.stop()
    return [int(c[-1]) for c in cols]


def line_of(s, text):
    """Line number of text in the script file."""
    with open(s.path) as f:
        return next(i for i, x in enumerate(f, 1) if text in x)


def refused(body, want, table=TABLE, name=None, head=HEAD, ser=None):
    """Runs a script that must be refused with this text, with nothing reaching the target."""
    s, r, sc = go(body, table=table, head=head, ser=ser)
    check(r['status'] == 'error' and want in r['reason'], f'{name or want}: {r["reason"]}')
    check(sc.ser.log == [('P', None, None)], f'{name or want}: nothing at the port after the ping: {sc.ser.log[:4]}')


def test_values():
    s, r, sc = go('''
def main():
    setup()
    stream_start()
    write("startup.ramp_rate", 123)
    write("startup.align_current", 456)
    wait_ms(50)
    printf("state %d\\n", read("motor.state"))
''', trial=FULL_TRIAL)
    check(r['status'] == 'ok' and not r['errors'], f'clean run: {r["status"]} {r["reason"]} {r["errors"]}')
    check(peek(sc, 'startup.ramp_rate', 'startup.align_current') == [123, 456], 'the values reach the model')
    log = sc.ser.log
    first = next(i for i, x in enumerate(log) if x[0] == 'W' and x[1] != ALIVE)
    check(log[first][1] == RAMP and log[first + 1][1] == ADDR['startup.align_current'], 'script writes in order')
    check('state 0' in logs(s), 'read() gives the raw value, printf() reaches the log')
    check(sum(1 for x in log[:first] if x[0] == 'W' and x[1] == ALIVE) >= 8,
          'heartbeat runs during the 2 s trial, nothing else is written before the end of the trial')
    check('aborted' not in logs(s) and r['abort_ms'] is not None, 'normal end: no "aborted", the safe end ran')
    check(sc.ser.writes(RUN) == [('W', RUN, 0)] * 2, 'normal end: safe values written twice')
    check('safe values read back: 1 of 1 correct' in logs(s), 'safe values read back')
    alive = [i for i, x in enumerate(log) if x[:2] == ('W', ALIVE)]
    safe = [i for i, x in enumerate(log) if x == ('W', RUN, 0)]
    check(alive[-1] > safe[-1], 'the heartbeat is still running after the second safe write')


def test_refuse_stream():
    # same declarations with one thing wrong each
    chans = 'channels("motor.speed", "motor.state")\n    '
    refused('def main():\n    channels("motor.speed")\n    safe_value("motor.run", 0)\n    stream_start()\n', 'no allow()')
    refused('def main():\n    channels("motor.speed")\n    allow("motor.run", 0, 1)\n    stream_start()\n', 'no safe_value()')
    refused('def main():\n    allow("motor.run", 0, 1)\n    safe_value("motor.run", 0)\n    stream_start()\n', 'no channels()')
    refused(f'def main():\n    {chans}allow("motor.run", 0, 1)\n    safe_value("motor.kp", 0)\n    stream_start()\n',
            'without allow()', name='safe_value not allowed')
    refused(f'def main():\n    {chans}allow("motor.run", 0, 1)\n    safe_value("motor.run", 2)\n    stream_start()\n',
            'outside allow()', name='safe_value outside its limits')
    refused('''def main():
    channels("motor.speed", "motor.state", "motor.fault", "motor.run", "motor.iq")
    allow("motor.run", 0, 1)
    safe_value("motor.run", 0)
    stream_start()
''', '5 channels')
    refused('''def main():
    channels("motor.speed", "motor.state", "motor.fault", "counter")
    allow("motor.run", 0, 1)
    safe_value("motor.run", 0)
    stream_start()
''', '104% of the UART', name='UART load')
    refused(f'def main():\n    {chans}allow("motor.run", 0, 1)\n    safe_value("motor.run", 0)\n'
            '    limit("motor.iq", 100)\n    stream_start()\n', 'not in channels()', name='limit not measured')
    refused('def main():\n    channels("motor.stat")\n', 'motor.state', name='name not in the table')
    missing = VarTable.from_symbols({k: v for k, v in SYMBOLS.items() if k != 'tune_alive'}, SYMBOL_TYPES)
    refused('def main():\n    setup()\n    stream_start()\n', 'tune_alive', table=missing)
    # 87 % passes
    s, r, sc = go('def main():\n    setup()\n    stream_start()\n')
    check(r['status'] == 'ok', '4 channels of 16 bit at 1 ms pass')
    # trial stream with a corrupted link
    ser = NoisySerial()
    ser.on_start = 50
    s, r, sc = go('def main():\n    setup()\n    stream_start()\n    write("motor.run", 1)\n', ser=ser)
    check(r['status'] == 'error' and 'trial stream' in r['reason'], f'trial with resyncs: {r["reason"]}')
    check([x for x in ser.writes() if x[1] != ALIVE] == [('W', RUN, 0)] * 2, 'failed trial: heartbeat and safe values only')


def test_refuse_write():
    s, r, sc = go('''
def main():
    setup()
    stream_start()
    write("startup.ramp_rate", 100)
    write("motor.ki", 5)        # LINE
''')
    want = f't.py:{line_of(s, "# LINE")}: error: write("motor.ki"): not allowed'
    check(r['status'] == 'error' and r['reason'].startswith(want), f'write without allow(): {r["reason"]}')
    check(want in logs(s) and want in run_txt(s), 'the error is in the log and in run.txt')
    check(not sc.ser.writes(ADDR['motor.ki']), 'nothing written')
    check(len(sc.ser.writes(RUN)) == 2, 'safe values after a refused write')
    s, r, sc = go('''
def main():
    setup()
    stream_start()
    write("startup.ramp_rate", 100)
    write("startup.ramp_rate", 501)     # LINE
''')
    want = f't.py:{line_of(s, "# LINE")}: error: write("startup.ramp_rate", 501): outside allow() 1 ... 500'
    check(r['status'] == 'error' and r['reason'].startswith(want), f'write outside allow(): {r["reason"]}')
    check(sc.ser.writes(RAMP) == [('W', RAMP, 100)], 'no clamping: the value was not written at all')
    s, r, sc = go('def main():\n    setup()\n    write("motor.run", 1)\n')
    check('before stream_start()' in r['reason'] and not sc.ser.writes(), f'write before the stream: {r["reason"]}')
    s, r, sc = go('def main():\n    setup()\n    stream_start()\n    x = read("motor.iq")     # LINE\n')
    want = f't.py:{line_of(s, "# LINE")}: error: read("motor.iq"): not in channels()'
    check(r['reason'].startswith(want), f'read of an unmeasured variable: {r["reason"]}')
    s, r, sc = go('def main():\n    setup()\n    stream_start()\n    allow("motor.ki", 0, 1)\n')
    check('must come before stream_start()' in r['reason'], f'declaration after the start: {r["reason"]}')
    s, r, sc = go('def main():\n    allow("motor.run", 0, 70000)\n')
    check('outside the range of u16' in r['reason'], f'allow() outside the C type: {r["reason"]}')


def test_limit():
    s, r, sc = go('''
def main():
    setup()
    limit("motor.speed", 500)
    stream_start()
    trace_begin()
    start_motor()
    wait_ms(5000)
    printf("NOT REACHED\\n")
''')
    check(r['status'] == 'aborted' and r['reason'].startswith('limit exceeded: motor.speed'), f'limit: {r["reason"]}')
    check('NOT REACHED' not in logs(s), 'the script was stopped')
    log = sc.ser.log
    i = next(k for k, x in enumerate(log) if x == ('W', RUN, 0))
    check(log[i + 1:].count(('W', RUN, 0)) == 1 and ('S', None, None) in log[i + 1:], 'safe values twice, before the stream stops')
    check(not sc.running, 'stream stopped')
    check(peek(sc, 'motor.run', 'motor.state') == [0, 0], 'motor.run is 0 in the model, stopped')
    check(r['abort_ms'] is not None and r['abort_ms'] < 100, f'abort time {r["abort_ms"]:.1f} ms')
    tr = s.traces[-1]
    check(tr['unfinished'] and tr['values']['motor.speed'].max() > 500, 'the open trace was kept')
    check(tr['values']['motor.run'][-1] == 0, 'the stream ran long enough to show run = 0')
    check('abort time' in run_txt(s) and 'limit exceeded' in run_txt(s), 'run.txt has reason and abort time')
    # magnitude: -32768 as well, whose absolute value does not fit an int16_t
    s, r, sc = go('''
def main():
    channels("motor.kp")
    allow("motor.kp", -32768, 3000)
    safe_value("motor.kp", 0)
    limit("motor.kp", 32767)
    stream_start()
    write("motor.kp", -32768)
    wait_ms(2000)
''')
    check(r['status'] == 'aborted' and 'motor.kp = -32768' in r['reason'], f'negative value: {r["reason"]}')
    # a spike between two polls of the monitor
    s, r, sc = go('''
def main():
    channels("motor.kp")
    allow("motor.kp", 0, 3000)
    safe_value("motor.kp", 0)
    limit("motor.kp", 1000)
    stream_start()
    write("motor.kp", 2000)
    time.sleep(0.003)
    write("motor.kp", 0)
    wait_ms(1000)
''')
    check(r['status'] == 'aborted' and 'motor.kp = 2000' in r['reason'], f'spike of a few samples: {r["reason"]}')
    s, r, sc = go('def main():\n    setup()\n    limit("motor.speed", 100000)\n    stream_start()\n    start_motor()\n')
    check(r['status'] == 'ok', 'a limit that is not reached does nothing')


def test_abort_in_wait():
    t0 = []

    def during(s):
        wait_log(s, 'WAITING')
        t0.append(time.time())
        check(tune_api.abort('Abort button') is True, 'abort() accepted')

    s, r, sc = go('''
def main():
    setup()
    stream_start()
    start_motor()
    printf("WAITING\\n")
    wait_ms(60000)
    printf("NOT REACHED\\n")
''', during=during)
    check(r['status'] == 'aborted' and 'Abort button' in r['reason'], f'abort: {r["reason"]}')
    check(time.time() - t0[0] < 3 and 'NOT REACHED' not in logs(s), f'wait_ms ended, run over after {time.time() - t0[0]:.1f} s')
    check(peek(sc, 'motor.run') == [0], 'motor.run is 0')
    check(tune_api.abort('late') is False, 'abort() without a run is harmless')
    # abort without a stream: wait_ms on the wall clock ends as well
    s, r, sc = go('def main():\n    printf("WAITING\\n")\n    wait_ms(60000)\n',
                  during=lambda s: (wait_log(s, 'WAITING'), tune_api.abort('x')))
    check(r['status'] == 'aborted' and not sc.ser.writes(), 'abort in wait_ms without a stream, nothing written')


def test_stuck():
    def during(s):
        wait_log(s, 'STUCK')
        time.sleep(0.2)
        tune_api.abort('stuck')

    s, r, sc = go('''
def main():
    setup()
    stream_start()
    start_motor()
    printf("STUCK\\n")
    while True:
        pass
''', during=during)
    check(r['status'] == 'aborted' and len(sc.ser.writes(RUN)) >= 3, f'stuck script, abort: {r["reason"]}')
    check(peek(sc, 'motor.run') == [0], 'motor.run is 0')
    check(not s.script_thread.is_alive(), 'the script thread was stopped')
    s, r, sc = go('''
def main():
    setup()
    limit("motor.speed", 1000)
    stream_start()
    write("startup.align_current", 400)
    write("startup.handover_speed", 600)
    write("startup.ramp_rate", 100)
    write("motor.run", 1)
    while True:
        pass
''')
    check(r['status'] == 'aborted' and 'limit exceeded' in r['reason'], f'stuck script, limit: {r["reason"]}')
    check(peek(sc, 'motor.run') == [0], 'the monitor stopped the motor without the script')
    # a script that catches everything cannot stop the end of the run, and cannot reach the next one
    builtins.tune_test_go = False
    def until_armed(s):
        while not s.armed and not s.done.is_set():
            time.sleep(0.005)
        time.sleep(0.1)
        tune_api.abort('zombie')

    s, r, sc = go('''
import builtins


def main():
    setup()
    stream_start()
    while not builtins.tune_test_go:
        try:
            while not builtins.tune_test_go:
                pass
        except BaseException:
            pass
    try:
        write("motor.run", 1)
    except BaseException as e:
        builtins.tune_test_zombie = type(e).__name__
''', during=until_armed)
    check(r['status'] == 'aborted' and s.script_thread.is_alive() and 'does not stop' in logs(s),
          'a script that swallows the stop: the run ends anyway, with a warning')

    def wake(s):
        builtins.tune_test_go = True
        s.script_thread_old.join(5)

    old = s.script_thread
    s2, r2, sc2 = go('''
def main():
    setup()
    stream_start()
    wait_ms(300)
''', during=lambda n: (setattr(n, 'script_thread_old', old), time.sleep(0.2), wake(n)))
    check(builtins.tune_test_zombie == 'RunAborted' and not [x for x in sc2.ser.writes() if x[1] == RUN and x[2]],
          'the old script thread gets RunAborted, also while the next run is active')


def test_exception():
    s, r, sc = go('''
def helper(x):
    return 1 / x            # LINE


def main():
    setup()
    stream_start()
    write("motor.run", 1)
    helper(0)
''')
    want = f't.py:{line_of(s, "# LINE")}: error: ZeroDivisionError'
    check(r['status'] == 'error' and r['reason'].startswith(want), f'exception: {r["reason"]}')
    check('tune_api' not in r['reason'], 'no line of tune_api.py')
    check(peek(sc, 'motor.run') == [0] and sc.ser.writes(RUN)[-1] == ('W', RUN, 0), 'safe values written')
    s, r, sc = go('def main():\n    x = 1 +\n', name='syntax.py')
    check(r['reason'].startswith(f'syntax.py:{line_of(s, "x = 1 +")}: error:'), f'syntax error: {r["reason"]}')
    s, r, sc = go('')
    check('no main()' in r['reason'], f'no main: {r["reason"]}')
    s, r, sc = go('def main():\n    expect(1 == 2, "must be equal")     # LINE\n')
    check(r['reason'] == f't.py:{line_of(s, "# LINE")}: error: must be equal', f'expect: {r["reason"]}')
    s, r, sc = go('def main():\n    wrong_name()      # LINE\n')
    check(r['reason'].startswith(f't.py:{line_of(s, "# LINE")}: error: NameError'), f'undefined name: {r["reason"]}')


def test_race():
    """A script hammers write() while the abort comes. Each of the two points of the race is forced: the
    abort comes while a write is before the gate ('pre') or inside it, past the recheck ('post'), and that
    write then waits longer than the monitor needs. The others come at a free moment."""
    bad = 0
    for n, stage in enumerate(('pre', 'post', 'pre', 'post', None, None, None)):
        ser = LogSerial()
        calls = [0]

        def hook(at):
            if at == stage:
                calls[0] += 1
                if calls[0] == 20:
                    tune_api.abort('race')
                    time.sleep(0.1)

        def during(s):
            if stage is None:
                wait_log(s, 'HAMMER')
                time.sleep(0.05 + 0.007 * n)
                tune_api.abort('race')

        s, r, sc = go('''
def main():
    setup()
    stream_start()
    printf("HAMMER\\n")
    n = 1
    while True:
        write("motor.run", 1)
        write("startup.ramp_rate", n)
        n = n % 500 + 1
''', ser=ser, hooks={'before_send': hook}, during=during)
        log = ser.writes()
        first = next(i for i, x in enumerate(log) if x == ('W', RUN, 0))
        late = [x for x in log[first:] if x[1] != ALIVE and x != ('W', RUN, 0)]
        bad += len(late)
        check(r['status'] == 'aborted', f'abort at {stage}: {len(log[:first])} writes before the safe write, {len(late)} script writes after it')
    check(bad == 0, 'no script write after the safe write')


def test_silent():
    ser = BadSerial()
    t0 = []

    def during(s):
        wait_log(s, 'UP')
        ser.silent = True
        t0.append(time.time())

    s, r, sc = go('''
def main():
    setup()
    stream_start()
    start_motor()
    printf("UP\\n")
    wait_ms(60000)
''', ser=ser, during=during)
    check(r['status'] == 'aborted' and 'stream stopped' in r['reason'], f'silent port: {r["reason"]}')
    t = time.time() - t0[0]
    check(t < 5, f'run over after {t:.1f} s')
    check(ser.writes(RUN)[-2:] == [('W', RUN, 0)] * 2, 'safe values sent to the silent port')
    check(any('read-back' in e for e in r['errors']), f'read-back reports the failure: {r["errors"]}')
    ser.silent = False
    check(peek(sc, 'motor.run') == [0], 'the writes did reach the model')


def test_port_error():
    ser = BadSerial()

    def during(s):
        wait_log(s, 'UP')
        ser.broken = True

    s, r, sc = go('''
def main():
    setup()
    stream_start()
    start_motor()
    trace_begin()
    printf("UP\\n")
    wait_ms(60000)
''', ser=ser, during=during)
    check(r['status'] == 'aborted' and r['reason'] == 'port error: device disappeared', f'port error: {r["reason"]}')
    check('safe values not read back: the port is gone' in logs(s), 'the read-back is skipped with a message')
    check('port error' in run_txt(s) and 'result aborted' in run_txt(s), 'run.txt was written')
    check(os.path.exists(os.path.join(s.folder, 'trace_001.csv')), 'the open trace was kept')
    # a port that hangs in a write: the run still ends
    ser = BadSerial()
    tune_api.PORT_TIMEOUT_S = 0.5

    def hang(s):
        wait_log(s, 'UP')
        ser.hang.set()
        time.sleep(0.2)
        tune_api.abort('hang')

    t0 = time.time()
    s, r, sc = go('''
def main():
    setup()
    stream_start()
    printf("UP\\n")
    while True:
        write("motor.run", 0)
''', ser=ser, during=hang)
    tune_api.PORT_TIMEOUT_S = 5.0
    ser.release.set()
    check(r['status'] == 'aborted' and any('does not return' in e for e in r['errors']),
          f'hanging port: ends after {time.time() - t0:.1f} s, {r["errors"][:1]}')


def test_readback():
    body = '''
def main():
    setup()
    stream_start()
    write("motor.kp", 7)
    write("motor.run", 1)
    wait_ms(50)
'''
    ser = BadSerial()
    ser.drop_safe = 2
    s, r, sc = go(body, ser=ser)
    check('safe values differ, rewriting: motor.run = 1' in logs(s) and r['status'] == 'ok',
          f'both safe writes lost: rewritten once, {r["status"]}')
    check(peek(sc, 'motor.run') == [0], 'and correct afterwards')
    ser = BadSerial()
    ser.drop_safe = 99
    s, r, sc = go(body + '    define("X", 1)\n', ser=ser)
    check(r['status'] == 'error' and any('safe value not reached: motor.run is 1' in e for e in r['errors']),
          f'rewrite lost too: {r["errors"]}')
    check('safe value not reached' in run_txt(s) and not r['tuning_h'], 'reported in run.txt, no tuning.h')
    s, r, sc = go('''
def main():
    channels("motor.speed")
    allow("motor.kp", 0, 10)
    allow("motor.ki", 0, 10)
    allow("motor.ref_offset", 0, 10)
    allow("motor.current_ref", 0, 10)
    allow("motor.current_kp", 0, 10)
    safe_value("motor.kp", 1)
    safe_value("motor.ki", 2)
    safe_value("motor.ref_offset", 3)
    safe_value("motor.current_ref", 4)
    safe_value("motor.current_kp", 5)
    stream_start()
''')
    check('safe values read back: 5 of 5 correct' in logs(s), 'five safe values are read back in two groups')


def test_heartbeat():
    hold = threading.Event()

    def during(s):
        wait_log(s, 'UP')
        hold.set()

    s, r, sc = go('''
def main():
    setup()
    stream_start()
    start_motor()
    printf("UP %d\\n", read("motor.state"))
    n = 0
    while read("motor.fault") == 0 and n < 300:
        wait_ms(10)
        n += 1
    printf("AFTER fault %d run %d state %d\\n", read("motor.fault"), read("motor.run"), read("motor.state"))
''', hooks={'hold_heartbeat': hold}, during=during)
    check('UP 3' in logs(s), 'motor in closed loop')
    check('AFTER fault 5 run 0 state 0' in logs(s), 'heartbeat stopped: the model sets fault 5 and stops (read before the safe end)')
    s, r, sc = go('''
def main():
    setup()
    stream_start()
    start_motor()
    n = 0
    bad = 0
    while n < 300:
        if read("motor.state") != 3 or read("motor.fault") != 0:
            bad += 1
        wait_ms(10)
        n += 1
    printf("BAD %d\\n", bad)
''')
    beats = len(sc.ser.writes(ALIVE))
    check('BAD 0' in logs(s) and r['status'] == 'ok', f'heartbeat keeps the motor alive for 3 s, {beats} beats')
    check(13 <= beats <= 40, 'interval about 200 samples')


def test_folder():
    src = '''
def main():
    setup()
    unit("motor.speed", 0.5, "rpm")
    summary_channel("motor.speed")
    table("ramp", "good")
    stream_start()
    trace_begin()
    start_motor()
    trace_end("start %d", 1)
    write("motor.run", 0)
    wait_ms(100)
    row(100, 1)
    trace_begin()
    wait_ms(50)
    trace_end("confirm")
    define("RAMP_RATE", 100)
    summary_mark(100)
    summary_text("result %d", 100)
'''
    s, r, sc = go(src, name='tune_x.py')
    names = sorted(os.listdir(s.folder))
    check(names == ['results.csv', 'run.txt', 'trace_001.csv', 'trace_002.csv', 'tune_x.py', 'tuning.h'], f'run folder: {names}')
    check(os.path.basename(s.folder).startswith('tune_x_') and len(os.path.basename(s.folder)) == len('tune_x_20261031_120000'),
          f'folder name {os.path.basename(s.folder)}')
    with open(os.path.join(s.folder, 'trace_001.csv'), newline='') as f:
        rows = list(csv.reader(f))
    check(rows[0] == ['time_ms', 'motor.speed', 'motor.state', 'motor.fault', 'motor.run', 'motor.speed [rpm]'], f'trace columns {rows[0]}')
    last = rows[-1]
    check(float(last[5]) == int(last[1]) * 0.5 and int(last[1]) > 500, f'raw {last[1]}, physical {last[5]}')
    check(int(rows[1][0]) <= 5 and all(int(a[0]) + 1 == int(b[0]) for a, b in zip(rows[1:-1], rows[2:])), 'time in ms from the trace start')
    with open(os.path.join(s.folder, 'results.csv'), newline='') as f:
        rows = list(csv.reader(f))
    check(rows == [['ramp', 'good', 'status'], ['100', '1', 'ok']], f'results.csv {rows}')
    txt = run_txt(s)
    for want in ('script tune_x.py', 'port LogSerial', 'ELF x.elf', 'result ok', 'write ', 'startup.ramp_rate = 100',
                 'motor.run = 1', 'motor.run = 0 (safe)', 'trace 001 "start 1"', 'abort time', 'summary_text result 100'):
        check(want in txt, f'run.txt has "{want}"')
    with open(os.path.join(s.folder, 'tuning.h')) as f:
        check('#define RAMP_RATE 100\n' in f.read(), 'tuning.h')
    with open(os.path.join(s.folder, 'tune_x.py')) as f:
        check(f.read() == HEAD + src, 'copy of the script')
    res = next(e for e in s.ev if e['type'] == 'result')
    check(res['status'] == 'ok' and res['summary_mark'] == 100 and res['summary_channel'] == 'motor.speed', 'result event')
    kinds = {e['type'] for e in s.ev}
    check(kinds >= {'state', 'log', 'unit', 'table', 'row', 'trace', 'result'}, f'events {sorted(kinds)}')
    states = [e['state'] for e in s.ev if e['type'] == 'state']
    check(states == ['starting', 'trial', 'running', 'safe_end', 'done'], f'states {states}')
    # trace longer than KEEP
    check(tune_api.KEEP == scope_gui.KEEP, 'KEEP equals scope_gui.KEEP')
    tune_api.KEEP = 100
    s, r, sc = go('''
def main():
    setup()
    stream_start()
    trace_begin()
    wait_ms(200)
    trace_end("long")       # LINE
''')
    tune_api.KEEP = scope_gui.KEEP
    want = f't.py:{line_of(s, "# LINE")}: error: trace of'
    check(r['reason'].startswith(want) and 'longer than KEEP' in r['reason'], f'long trace: {r["reason"]}')
    # output folder unwritable: refused before anything happens
    ser = LogSerial()
    sc = Scope(ser)
    path = os.path.join(TMP, 'a_file')
    open(path, 'w').close()
    script = os.path.join(TMP, 'x.py')
    with open(script, 'w') as f:
        f.write(HEAD)
    try:
        tune_api.run(script, sc, TABLE, out_dir=os.path.join(path, 'runs'), hooks={'skip_checks': True, 'skip_probe': True})
        check(False, 'unwritable folder')
    except tune_api.RunRefused as e:
        check('cannot be written' in str(e) and not ser.log and tune_api._active is None, f'unwritable folder: {e}')


CONFIRM = '''
def main():
    setup()
    table("ramp")
    stream_start()
    trace_begin()
    wait_ms(50)
%s
    trace_end("sweep")
    row(1)
    trace_begin()
    printf("IN CONFIRM\\n")
    wait_ms(%d)
%s
    trace_end("confirm")
    define("RAMP_RATE", 100)
%s
'''


def test_invalid():
    def noise(ser):
        def f(e):
            if e['type'] == 'log' and 'IN CONFIRM' in e['text']:
                ser.pending = 2
        return f

    s, r, sc = go(CONFIRM % ('', 100, '', ''))
    check(r['tuning_h'] and not s.traces[-1]['invalid'], 'clean: tuning.h written')
    ser = NoisySerial()
    s, r, sc = go(CONFIRM % ('', 400, '', ''), ser=ser, on_event=noise(ser))
    check(s.traces[1]['invalid'] and not s.traces[0]['invalid'], f'resyncs during the trace: invalid ({sc.resyncs if sc else ""})')
    check(r['status'] == 'ok' and r['tuning_h'] is None and 'tuning.h not written' in logs(s), 'an invalid confirming trace gives no tuning.h')
    check('invalid' in run_txt(s), 'marked in run.txt')
    ser = NoisySerial()
    body = '''
def main():
    setup()
    table("ramp")
    stream_start()
    trace_begin()
    printf("IN CONFIRM\\n")
    wait_ms(400)
    trace_end("x")
    row(1)
    wait_ms(100)
    row(2)
'''
    s, r, sc = go(body, ser=ser, on_event=noise(ser))
    rows = [e['invalid'] for e in s.ev if e['type'] == 'row']
    check(rows == [True, False], f'row invalid if lost or resyncs rose since the last row: {rows}')
    with open(os.path.join(s.folder, 'results.csv'), newline='') as f:
        check(list(csv.reader(f))[1] == ['1', 'invalid'], 'marked in results.csv')
    s, r, sc = go(CONFIRM % ('', 100, '    trace_failed()', ''))
    check(s.traces[1]['failed'] and r['tuning_h'] is None, 'a failed trace after the last row: no tuning.h')
    s, r, sc = go(CONFIRM % ('    trace_failed()', 100, '', ''))
    check(s.traces[0]['failed'] and r['tuning_h'], 'a failed trace of the sweep, before the last row, is allowed')
    s, r, sc = go(CONFIRM % ('', 100, '', '    x = 1 / 0'))
    check(r['status'] == 'error' and r['tuning_h'] is None, 'exception after define(): no tuning.h')
    s, r, sc = go(CONFIRM % ('', 100, '', '    wait_ms(60000)'), during=lambda s: (wait_log(s, 'IN CONFIRM'), time.sleep(0.3), tune_api.abort('x')))
    check(r['status'] == 'aborted' and r['tuning_h'] is None, 'abort after define(): no tuning.h')
    s, r, sc = go(CONFIRM % ('', 100, '', '    trace_begin()'))
    check(r['tuning_h'] is None and s.traces[-1]['unfinished'], 'an unfinished trace: no tuning.h')
    s, r, sc = go('def main():\n    setup()\n    stream_start()\n    return\n')
    check(r['status'] == 'ok' and r['tuning_h'] is None, 'no define(): no tuning.h')
    # lost frames are counted as time by wait_ms and mark the trace
    ser = NoisySerial()

    def drop(e):
        if e['type'] == 'log' and 'DROP' in e['text']:
            ser.drop = 200

    s, r, sc = go('''
def main():
    setup()
    stream_start()
    trace_begin()
    printf("DROP\\n")
    t = time.time()
    wait_ms(400)
    printf("WALL %d\\n", (time.time() - t) * 1000)
    trace_end("x")
''', ser=ser, on_event=drop)
    wall = int(logs(s).split('WALL ')[1].split()[0])
    check(s.traces[0]['invalid'] and 350 <= wall <= 520, f'wait_ms(400) with 200 samples lost: {wall} ms, the lost ones count as time')


def test_session():
    names = {}
    exec('from tune_api import *', names)      # noqa: S102
    names.pop('__builtins__')
    want = {'channels', 'allow', 'safe_value', 'limit', 'stream_start', 'write', 'read', 'wait_ms', 'trace_begin', 'trace_end',
            'trace_failed', 'table', 'row', 'summary_channel', 'summary_text', 'summary_mark', 'unit', 'define', 'printf',
            'Q15', 'expect', 'RISING', 'FALLING', 'capture_channels', 'capture_period_us', 'capture_trigger', 'capture_start',
            'capture_arm', 'capture_wait', 'capture_max', 'capture_final', 'capture_rise_us', 'capture_length', 'capture_value',
            'capture_save'}
    check(set(names) == want, f'the script gets exactly {len(want)} names')
    check(tune_api.Q15(0.25) == 8192, 'Q15(0.25) = 8192')
    try:
        tune_api.write('motor.run', 1)
        check(False, 'API outside a run')
    except tune_api.ScriptError as e:
        check('inside a running script' in str(e), f'outside a run: {e}')
    check(issubclass(tune_api.RunAborted, BaseException) and not issubclass(tune_api.RunAborted, Exception), 'RunAborted is no Exception')

    def second(s):
        try:
            tune_api.run(s.path, s.scope, TABLE, out_dir=TMP)
            check(False, 'second run')
        except tune_api.RunRefused as e:
            check('another run' in str(e), f'second run: {e}')
        tune_api.abort('x')

    s, r, sc = go('def main():\n    setup()\n    stream_start()\n    wait_ms(60000)\n', during=second)
    s, r, sc = go('def main():\n    stream_start()\n')
    check('no allow()' in r['reason'], 'declarations of the last run are gone')


def test_stuck_receiver():
    block = threading.Event()
    s, r, sc = go('def main():\n    setup()\n    stream_start()\n    write("motor.run", 1)\n', on_event=lambda e: block.wait(20))
    block.set()
    check(r['status'] == 'ok' and peek(sc, 'motor.run') == [0], 'an on_event that blocks does not hold up the safe end')


class FullDisk:
    """run.txt whose close() raises, as on a full disk."""

    def __init__(self, f):
        self.f = f

    def __getattr__(self, name):
        return getattr(self.f, name)

    def close(self):
        self.f.close()
        raise OSError('disk full')


def full_disk_open(path, *args, **kw):
    f = builtins.open(path, *args, **kw)
    return FullDisk(f) if os.path.basename(path) == 'run.txt' else f


TRIAL = '''
def main():
    channels("motor.kp")
    allow("motor.kp", 0, 100)
    safe_value("motor.kp", 77)
    %s
    stream_start()
    printf("NOT REACHED")
'''


def until_stream(s):
    end = time.time() + 10
    while not s.streaming and time.time() < end:
        time.sleep(0.005)
    time.sleep(0.05)


def check_trial_end(s, r, sc, what):
    """The safe values of TRIAL are at the port, twice, a read-back stream after them, and they read back."""
    log = sc.ser.log
    check(sc.ser.writes(KP)[-2:] == [('W', KP, 77)] * 2, f'{what}: safe values at the port, twice: {sc.ser.writes(KP)}')
    check({x[1] for x in sc.ser.writes()} <= {ALIVE, KP}, f'{what}: nothing else was written')
    check(not s.armed and 'NOT REACHED' not in logs(s), f'{what}: the trial did not pass, the script is stopped')
    check('safe values read back: 1 of 1 correct' in logs(s), f'{what}: read back')
    if ('W', KP, 77) in log:
        last = len(log) - log[::-1].index(('W', KP, 77))
        check(('C', None, None) in log[last:], f'{what}: a read-back stream follows the safe values')
    check(r['abort_ms'] is not None, f'{what}: abort time {r["abort_ms"]}')
    got = peek(sc, 'motor.kp')
    check(got == [77], f'{what}: 77 in the model: {got}')


def test_trial_end():
    def stop(s):
        until_stream(s)
        tune_api.abort('Abort button')

    s, r, sc = go(TRIAL % 'pass', during=stop, trial=5000)
    check(r['status'] == 'aborted' and 'Abort button' in r['reason'], f'abort in the trial: {r["reason"]}')
    check_trial_end(s, r, sc, 'abort in the trial')

    def exceed(s):
        until_stream(s)
        s.scope.write(KP, 's16', 500)

    s, r, sc = go(TRIAL % 'limit("motor.kp", 400)', during=exceed, trial=5000)
    check(r['status'] == 'aborted' and 'limit exceeded: motor.kp = 500' in r['reason'], f'limit in the trial: {r["reason"]}')
    check_trial_end(s, r, sc, 'limit in the trial')

    ser = NoisySerial()
    ser.on_start = 50
    s, r, sc = go(TRIAL % 'pass', ser=ser)
    check(r['status'] == 'error' and 'trial stream' in r['reason'], f'corrupted trial: {r["reason"]}')
    ser.on_start = ser.pending = 0
    check_trial_end(s, r, sc, 'corrupted trial')

    # past the checks, but the stream did not start: safe values all the same, nothing to keep
    start, calls = Scope.start, []

    def no_stream(self, *args, **kw):
        calls.append(args)
        return 'Target does not respond' if len(calls) == 1 else start(self, *args, **kw)

    Scope.start = no_stream
    try:
        s, r, sc = go(TRIAL % 'pass')
    finally:
        Scope.start = start
    check(r['status'] == 'error' and 'Target does not respond' in r['reason'], f'stream did not start: {r["reason"]}')
    check_trial_end(s, r, sc, 'stream did not start')

    # the first start of the read-back is disturbed, the second works: the read-back is tried once more
    start, calls = Scope.start, []

    def bad_readback(self, *args, **kw):
        calls.append(args)
        return 'Target does not respond' if len(calls) == 2 else start(self, *args, **kw)

    Scope.start = bad_readback
    try:
        s, r, sc = go(TRIAL % 'pass')
    finally:
        Scope.start = start
    check(len(calls) >= 3, f'read-back start failed once, then started again: {len(calls)} starts')
    check('safe values read back: 1 of 1 correct' in logs(s), f'read-back retried: {logs(s)[-300:]}')
    check(not any('read-back' in e for e in r['errors']), f'no error left from the first start: {r["errors"]}')

    # refused: the ELF is not verified, not a byte goes out
    check_id = tune_api.build_id_check
    tune_api.build_id_check = lambda session: 'the ELF does not match the target'
    try:
        refused('def main():\n    setup()\n    stream_start()\n', 'the ELF does not match', name='wrong build')
        s, r, sc = go(TRIAL % 'pass')
    finally:
        tune_api.build_id_check = check_id
    check(s.hb_thread is None and r['abort_ms'] is None, 'refused: no heartbeat, no safe write')
    check('safe values not written: stream_start() did not pass its checks' in logs(s), f'refused: the log says so: {logs(s)}')


def test_guard():
    body = 'def main():\n    setup()\n    stream_start()\n    write("motor.run", 1)\n    define("X", 1)\n'

    def ended(s, r, what):
        check(s.done.is_set() and isinstance(r, dict) and 'status' in r and 'folder' in r, f'{what}: done is set, result is a dict')
        check(r['status'] == 'error', f'{what}: status error: {r["status"]}')
        check(not r.get('tuning_h') and not os.path.exists(os.path.join(s.folder, 'tuning.h')), f'{what}: no tuning.h')
        check([e['state'] for e in s.ev if e['type'] == 'state'][-1:] == ['done'] and any(e['type'] == 'result' for e in s.ev),
              f'{what}: the GUI gets result and done')
        check(tune_api._active is None, f'{what}: no run left active')
        tune_api._active = None
        r2 = go('def main():\n    setup()\n    stream_start()\n')[1]
        check(r2['status'] == 'ok', f'{what}: the next run starts')

    tune_api.open = full_disk_open
    try:
        s, r, sc = go(body, wait=5)
    finally:
        del tune_api.open
    check(any('disk full' in e for e in r['errors']), f'close raises: {r["errors"]}')
    ended(s, r, 'close raises')

    def boom(self, *args):
        raise ValueError('a bug')

    write_tuning = tune_api.Session._write_tuning
    tune_api.Session._write_tuning = boom
    try:
        s, r, sc = go(body, wait=5)
    finally:
        tune_api.Session._write_tuning = write_tuning
    check(any('a bug' in e for e in r['errors']), f'_finish raises: {r["errors"]}')
    ended(s, r, '_finish raises')

    def after(s, r, sc, what, safe=True, read_back=True):
        check(any('a bug' in e for e in r['errors']), f'{what}: {r["errors"]}')
        check(('S', None, None) in sc.ser.log[-40:] and not sc.running and sc.thread is None, f'{what}: the stream is stopped')
        check(s.hb_stop.is_set() and s.hb_thread and not s.hb_thread.is_alive(), f'{what}: the heartbeat is stopped')
        check(not s.script_thread.is_alive(), f'{what}: the script thread ended')
        check(not safe or sc.ser.writes(RUN)[-2:] == [('W', RUN, 0)] * 2, f'{what}: safe values written')
        check(not read_back or 'safe values read back' in logs(s), f'{what}: read back all the same')
        ended(s, r, what)

    for name in ('_save_open_trace', '_keep_stream', '_stop_script', '_verify_safe', '_send_safe'):
        keep = getattr(tune_api.Session, name)
        setattr(tune_api.Session, name, boom)
        try:
            s, r, sc = go(body, wait=15)
        finally:
            setattr(tune_api.Session, name, keep)
        after(s, r, sc, f'{name} raises', safe=name != '_send_safe', read_back=name != '_verify_safe')

    drain = tune_api.Session._drain

    def bad_drain(self, check=True):        # check=False: only the safe end
        if not check:
            raise ValueError('a bug')
        drain(self, check)

    tune_api.Session._drain = bad_drain
    try:
        s, r, sc = go(body, wait=15)
    finally:
        tune_api.Session._drain = drain
    after(s, r, sc, 'the drain of the safe end raises')


def test_limit_width():
    """Scope keeps stream samples as float64 (np.zeros(0) in start(), np.concatenate in _stream()), so
    np.abs() of the most negative integer works. This keeps it so, through a real stream."""
    table = VarTable.from_symbols({**SYMBOLS, 'probe8': (KP, 1), 'probe32': (KP, 4), 'probef': (KP, 4)},
                                  {**SYMBOL_TYPES, 'probe8': 's8', 'probe32': 's32', 'probef': 'f32'})
    for name, low in (('probe8', -128), ('motor.kp', -32768), ('probe32', -2 ** 31)):
        top = -low - 1
        head = f'def main():\n    channels("{name}")\n    allow("{name}", {low}, {top})\n    safe_value("{name}", 0)\n'
        s, r, sc = go(head + f'    stream_start()\n    write("{name}", {low})\n    wait_ms(50)\n'
                      f'    printf("READ %d", read("{name}"))\n', table=table)
        check(f'READ {low}' in logs(s), f'read() of {name} gives {low}: {r["reason"]}')
        check(sc.snapshot()[1][0].dtype == float, f'{name}: stream column is {sc.snapshot()[1][0].dtype}')
        s, r, sc = go(head + f'    limit("{name}", {top})\n    stream_start()\n    write("{name}", {low})\n'
                      '    wait_ms(2000)\n    printf("NOT REACHED")\n', table=table)
        check(r['status'] == 'aborted' and f'{name} = {low} ' in r['reason'], f'limit({name}, {top}) on {low}: {r["reason"]}')
        check('NOT REACHED' not in logs(s), f'{name}: the script was stopped')
    s, r, sc = go('def main():\n    channels("probef")\n    allow("probe32", 0, 0x7FFFFFFF)\n    safe_value("probe32", 0)\n'
                  '    limit("probef", 1000)\n    stream_start()\n    write("probe32", 0x7FC00000)\n    wait_ms(2000)\n', table=table)
    check(r['status'] == 'aborted' and 'probef = nan' in r['reason'], f'NaN trips the limit: {r["reason"]}')


def test_startup():
    """scripts/tune_startup.py on SIM through tools/run_script.py, as a subprocess: exit code, run folder, report."""
    import re
    import subprocess
    work = tempfile.mkdtemp(dir=TMP)
    tool = os.path.join(HERE, '..', 'tools', 'run_script.py')
    script = os.path.join(HERE, '..', 'scripts', 'tune_startup.py')
    cmd = [sys.executable, tool, '--port', 'SIM', '--out', 'runs', script]
    p = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=120)
    check(p.returncode == 0, f'tune_startup.py on SIM: exit code {p.returncode}')
    folders = os.listdir(os.path.join(work, 'runs')) if os.path.isdir(os.path.join(work, 'runs')) else []
    check(len(folders) == 1, f'one run folder: {folders}')
    folder = os.path.join(work, 'runs', folders[0]) if folders else work
    with open(os.path.join(folder, 'results.csv'), newline='') as f:
        rows = list(csv.reader(f))
    # start times depend on the 10 ms polling against the model, so only their range and order are fixed
    t = [int(r[2]) for r in rows[1:4]]
    check(rows[0] == ['ramp_rate', 'good_starts', 'time_ms', 'status'] and [r[:2] for r in rows[1:]] == [['50', '1'], ['100', '1'], ['200', '1'], ['400', '0']]
          and rows[4][2:] == ['-1', 'failed'] and all(r[3] == 'ok' for r in rows[1:4]), f'results.csv {rows[1:]}')
    check(all(0 < x < 400 and x % 10 == 0 for x in t) and t[2] < t[0], f'start times of the good ramps: {t} ms')
    traces = sorted(x for x in os.listdir(folder) if x.startswith('trace_'))
    check(len(traces) == 9, f'4 traces of the starts and 5 confirming traces: {len(traces)}')
    with open(os.path.join(folder, 'tuning.h')) as f:
        h = f.read()
    for want in ('#define RAMP_RATE 100\n', '#define ALIGN_CURRENT 400\n', '#define HANDOVER_SPEED 600\n'):
        check(want in h, f'tuning.h has {want.strip()}')
    with open(os.path.join(folder, 'run.txt'), encoding='utf-8') as f:
        txt = f.read()
    for want in ('trace 004 "ramp 400, start 1": ', 'trace 009 "confirm 5": ', 'startup.align_current = 400\n',
                 'startup.handover_speed = 600\n', 'result ok'):
        check(want in txt, f'run.txt has "{want.strip()}"')
    seq = re.findall(r'^write \d+ ms startup.ramp_rate = (\d+)$', txt, re.M)
    check(seq == ['50', '100', '200', '400', '100'], f'run.txt: the ramp rates written by the script: {seq}')
    check(len(re.findall(r'^write \d+ ms motor.run = 1$', txt, re.M)) == 9, 'run.txt: 9 starts')
    check('motor.run = 0 (safe)' in txt, 'run.txt: the safe value')
    with open(os.path.join(work, 'run_report.txt'), encoding='utf-8') as f:
        rep = f.read()
    check('status ok' in rep and 'port SIM' in rep and 'script tune_startup.py' in rep and 'W204 x3, W205 x2' in rep, 'run_report.txt')
    bad = [w for w in ('motor', 'align', 'handover', 'ramp', TMP, work, os.sep) if w in rep]
    check(not bad, f'run_report.txt holds no path, name or value: {bad}')
    # an error stops before the port is opened: exit code 1, no run folder
    broken = os.path.join(work, 'broken.py')
    with open(script, encoding='utf-8') as f:
        src = f.read()
    with open(broken, 'w', encoding='utf-8') as f:
        f.write(src.replace('def main():', 'def main():\n    nothing_here()', 1))
    shutil.rmtree(os.path.join(work, 'runs'))
    p = subprocess.run([sys.executable, tool, '--port', 'SIM', '--out', 'runs', broken], cwd=work, capture_output=True,
                       text=True, timeout=120)
    check(p.returncode == 1 and not os.path.exists(os.path.join(work, 'runs')), f'script with an error: exit code {p.returncode}, no run folder')
    check('E102' in p.stdout and 'Connected' not in p.stdout, 'the error is printed with its rule, the port was not opened')
    p = subprocess.run([sys.executable, tool, '--check', script], cwd=work, capture_output=True, text=True, timeout=120)
    check(p.returncode == 0 and 'Connected' not in p.stdout, f'--check needs no port: exit code {p.returncode}')


def run_speed(work, script, out):
    """tune_speed.py (or a copy) on SIM through tools/run_script.py: the process and its run folder."""
    import subprocess
    tool = os.path.join(HERE, '..', 'tools', 'run_script.py')
    p = subprocess.run([sys.executable, tool, '--port', 'SIM', '--out', out, script], cwd=work, capture_output=True,
                       text=True, timeout=120)
    path = os.path.join(work, out)
    folders = os.listdir(path) if os.path.isdir(path) else []
    return p, os.path.join(path, folders[0]) if folders else work


def test_speed():
    """scripts/tune_speed.py on SIM: the best point of docs/evidence/5.2.md, three runs in a row with the same gains,
    and a copy with high gains and no soft rule, which the hard limit must end. Overshoot and settling times move by a
    few units from run to run (the model runs on the wall clock), so the table is checked in ranges."""
    import re
    work = tempfile.mkdtemp(dir=TMP)
    script = os.path.join(HERE, '..', 'scripts', 'tune_speed.py')
    gains = []
    for k in range(3):
        out = f'runs{k}'
        p, folder = run_speed(work, script, out)
        check(p.returncode == 0, f'tune_speed.py run {k + 1}: exit code {p.returncode}')
        with open(os.path.join(folder, 'tuning.h')) as f:
            h = f.read()
        gains.append(tuple(re.findall(r'#define (\w+) (\d+)', h)))
        if k > 0:
            continue
        with open(os.path.join(folder, 'results.csv'), newline='') as f:
            rows = list(csv.reader(f))
        data = {(int(r[0]), int(r[1]), int(r[2])): (int(r[3]), int(r[4])) for r in rows[1:]}
        check(rows[0] == ['kp', 'ki', 'offset', 'overshoot_pct', 'settle_ms', 'status'] and len(rows) == 1 + 2 * 10
              and all(r[5] == 'ok' for r in rows[1:]), f'results.csv: 10 pairs at 2 points: {len(rows) - 1} rows')
        check([r[:2] for r in rows[1:7]] == [['200', '60'], ['200', '60'], ['200', '150'], ['200', '150'], ['200', '300'], ['200', '300']]
              and rows[-1][:2] == ['1500', '60'], 'the grid is walked from gentle to aggressive, kp 200 stops at ki 300, kp 1500 ends it')
        # The model settles in about 21 ms (20 to 26 ms in 8 runs on an idle PC). The script knows the start of a step
        # to one read chunk of the port plus one poll step of 10 ms, and the chunk grows with the load of the PC
        # (42 ms was seen once). So: the model, 10 ms less for the cursor, at most 30 ms (chunk) and 10 ms (poll)
        # more. The separation from the other pairs is the overshoot, not the settling time.
        best = [(data[(500, 150, o)]) for o in (300, -300)]
        check(all(os_ <= 5 and 10 <= st <= 61 for os_, st in best), f'best point kp 500, ki 150: (overshoot, settle) {best}')
        # as good: little overshoot and a settling time within 20 ms of the best point at the same step; the next pair
        # (200, 60) settles in 52 to 86 ms on SIM, 30 ms later than the best point, and a load of the PC moves both
        # by the same chunk
        others = [(kp, ki) for kp, ki, o in data if (kp, ki) != (500, 150) and data[(kp, ki, o)][0] <= 5 and data[(kp, ki, o)][1] <= data[(500, 150, o)][1] + 20]
        check(not others, f'no other pair is as good: {others}')
        check(all(data[(1500, 60, o)][0] > 20 for o in (300, -300)), 'kp 1500 overshoots above the soft limit')
        with open(os.path.join(folder, 'run.txt'), encoding='utf-8') as f:
            txt = f.read()
        for want in ('kp 200: overshoot', 'no larger ki', 'kp 1500 overshoots', 'ending the grid', 'result ok', 'motor.run = 0 (safe)',
                     'motor.ref_offset = 0 (safe)'):
            check(want in txt, f'run.txt has "{want}"')
        check(len(re.findall(r'^trace \d+ ', txt, re.M)) == 20 + 6, 'run.txt: 20 traces of the table and 6 of the confirming')
        stages = subprocess_check(work, script)
        check(stages.returncode == 0 and 'error' not in stages.stdout.replace('0 errors', ''), 'check stages: no errors')
        w = [l for l in stages.stdout.splitlines() if ': warning: ' in l and '[W204]' not in l and '[W205]' not in l]
        check(not w, f'check stages: no warning except W204 and W205 as in tune_startup.py: {w}')
    check(gains[0] == (('SPEED_KP', '500'), ('SPEED_KI', '150')) and gains[0] == gains[1] == gains[2], f'tuning.h in three runs: {gains}')

    # high gains and no soft rule: only limit() is left, and it must end the run with the safe values written
    with open(script, encoding='utf-8') as f:
        src = f.read()
    for name, value in (('KP_LIST', '[2000, 4000]'), ('KI_LIST', '[1000]'), ('SOFT_OVERSHOOT', '1000')):
        src, n = re.subn(r'^%s .*$' % name, '%s = %s' % (name, value), src, flags=re.M)
        check(n == 1, f'copy: {name} replaced')
    copy = os.path.join(work, 'tune_speed_high.py')
    with open(copy, 'w', encoding='utf-8') as f:
        f.write(src)
    p, folder = run_speed(work, copy, 'runs_high')
    with open(os.path.join(folder, 'run.txt'), encoding='utf-8') as f:
        txt = f.read()
    check(p.returncode != 0 and 'result aborted' in txt and 'limit' in txt, f'high gains: aborted by limit(), exit code {p.returncode}')
    check(not os.path.exists(os.path.join(folder, 'tuning.h')), 'high gains: no tuning.h')
    with open(os.path.join(folder, 'results.csv'), newline='') as f:
        rows = list(csv.reader(f))
    check(len(rows) > 1 and rows[1][:2] == ['2000', '1000'], f'high gains: the table so far is kept, {len(rows) - 1} rows')
    check('motor.run = 0 (safe)' in txt and 'motor.ref_offset = 0 (safe)' in txt, 'high gains: the safe values were written')


def subprocess_check(work, script):
    import subprocess
    tool = os.path.join(HERE, '..', 'tools', 'run_script.py')
    return subprocess.run([sys.executable, tool, '--check', script], cwd=work, capture_output=True, text=True, timeout=120)


def startup_table(ser=None):
    """scripts/tune_startup.py through tune_api on a serial: the rows of its table as (ramp, good, time_ms)."""
    rows = []
    script = os.path.join(HERE, '..', 'scripts', 'tune_startup.py')
    s, r, sc = go('', ser=ser, script=script, wait=120, on_event=lambda e: rows.append(tuple(e['values'])) if e['type'] == 'row' else None)
    return r['status'], [x for x in rows]


MODEL_MS = {50: 221, 100: 161, 200: 131}        # docs/evidence/m1/model_check.txt


def test_cursor():
    """The script's time is its own cursor: the same table on every port, chunky delivery or not.
    The model runs on the wall clock, so the write lands up to one reader period after the newest sample the
    script knows, and the script counts in steps of 10 ms: a start time is right to within one poll step (-10 to +20 ms).
    On a port delivering in chunks (200 ms here) the time of a write is known to the PC only to one chunk, so a
    start time may be off by up to the chunk: the limit is model + chunk + 30 ms. good_starts stays exact."""
    plain, goods = [], []
    chunk = int(ChunkSerial.burst * 1000)
    for label, make in (('plain', LogSerial), ('bursts of 200 ms', ChunkSerial)):
        for k in range(5 if label == 'plain' else 3):
            status, rows = startup_table(make())
            t = {r[0]: r[2] for r in rows}
            g = {r[0]: r[1] for r in rows}
            hi = 20 if label == 'plain' else chunk + 30
            ok = status == 'ok' and all(-10 <= t.get(q, -99) - m <= hi for q, m in MODEL_MS.items()) and t.get(400) == -1
            check(ok, f'{label}, run {k + 1}: {status}, time_ms {[t.get(q) for q in MODEL_MS]} against {list(MODEL_MS.values())}')
            if label == 'plain':
                plain.append(t)
                goods.append(g)
            else:
                ref = plain[0]
                check(g == goods[0], f'{label}, run {k + 1}: good_starts exactly as without bursts {list(goods[0].values())}')
                check(all(-10 <= t.get(q, -99) - ref[q] <= chunk + 30 for q in MODEL_MS), f'{label}, run {k + 1}: within chunk + 30 ms of the table without bursts {[ref[q] for q in MODEL_MS]}')


def test_cursor_read():
    """read() gives the value at the cursor, not the newest one, also when a sample was lost."""
    body = '''
def main():
    setup()
    stream_start()
    write("startup.align_current", 400)
    write("startup.handover_speed", 600)
    write("startup.ramp_rate", 100)
    write("motor.run", 1)
    seen = []
    for n in range(40):
        wait_ms(10)
        seen.append(read("motor.state"))
    printf("states %s\\n", ",".join(str(x) for x in seen))
'''
    for label, make in (('plain', LogSerial), ('bursts', ChunkSerial)):
        s, r, sc = go(body, ser=make(), wait=60)
        line = [x for x in logs(s).split(chr(10)) if x.startswith('states ')]
        seen = [int(x) for x in line[0][7:].split(',')] if line else []
        first = [i for i, x in enumerate(seen) if x == 3]
        check(r['status'] == 'ok' and first, f'{label}: closed loop seen at cursor step {first[:1]} of 40 ({r["status"]})')
        check(seen == sorted(seen), f'{label}: states never go back: {seen[:5]} ...')
        results = locals().setdefault('results', {})
        results[label] = first[:1]
    # the write lands at a time the PC knows only to one chunk (20 steps here), plus one poll step
    a, b = results['plain'], results['bursts']
    check(a and b and abs(a[0] - b[0]) <= int(ChunkSerial.burst * 100) + 3, f'the same step on both, to within one chunk + 3 steps: {results}')


def png_size(path):
    with open(path, 'rb') as f:
        head = f.read(24)
    return head[:8] == b'\x89PNG\r\n\x1a\n' and struct.unpack('>II', head[16:24])


class Recorder:
    """render_png that keeps the dicts it got and returns the real picture, or raises."""

    def __init__(self, fail=None):
        self.ds, self.fail = [], fail

    def __call__(self, d):
        self.ds.append(d)
        if self.fail:
            raise self.fail
        return scope_gui.render_png(d)


SUMMARY_BODY = '''
def main():
    setup()
    unit("motor.speed", 0.5, "rpm")
    summary_channel("motor.speed")
    table("ramp", "starts")
    stream_start()
    for ramp in (100, 200):
        write("startup.ramp_rate", ramp)
        trace_begin()
        start_motor()
        if ramp == 200:
            trace_failed()
        wait_ms(50)
        write("motor.run", 0)
        trace_end("ramp %d, start 1", ramp)
        wait_ms(100)
        row(ramp, 1)
    summary_mark(100)
    summary_text("Suggested ramp rate: %d", 100)
'''


def test_summary():
    rec, seen = Recorder(), []

    def on_event(e):
        if e['type'] == 'result':
            seen.append(os.path.exists(os.path.join(e['folder'], 'summary.png')))

    s, r, sc = go(SUMMARY_BODY, render_png=rec, on_event=on_event, wait=60)
    png = os.path.join(s.folder, 'summary.png')
    check(r['status'] == 'ok' and r['summary_png'] == png, f'normal run: summary.png in the run folder, status {r["status"]}')
    check(png_size(png) == (1920, 1080), f'summary.png is 1920 x 1080: {png_size(png)}')
    check(seen == [True], f'the picture exists when the result event comes: {seen}')
    d = rec.ds[0]
    names = [x[0] for x in d['series']]
    check(len(names) == 2 and '(failed)' in names[1] and d['series'][1][3]['linestyle'] == '--'
          and 'linestyle' not in d['series'][0][3], f'failed trace dashed, good one solid: {names}')
    check(d['series'][0][3]['linewidth'] > 2 and d['mark_row'] == 0, f'trace and row of the mark highlighted: row {d["mark_row"]}')
    check(d['ylabel'] == 'motor.speed [rpm]' and d['legend'] == 'outside' and d['note'] == 'Suggested ramp rate: 100',
          f'unit on the axis, legend outside, result line on top: {d["ylabel"]!r}, {d["note"]!r}')
    raw = s.traces[0]['values']['motor.speed']
    check(len(raw) and (d['series'][0][2] == raw * 0.5).all(), f'y in rpm: {len(raw)} samples, raw x 0.5')
    check('ELF x.elf' in d['info'][0] and 'port ' in d['info'][0], f'port and ELF: {d["info"][0]}')
    check(d['columns'] == ['ramp', 'starts', 'status'] and d['rows'][1] == ['200', '1', 'failed'], f'table from row(): {d["rows"]}')

    # abort: the picture comes with the reason on top
    rec = Recorder()

    def during(s):
        wait_log(s, 'WAITING')
        tune_api.abort('Abort button')

    s, r, sc = go(SUMMARY_BODY.replace('    summary_mark(100)', '    printf("WAITING\\n")\n    wait_ms(60000)'), render_png=rec,
                  during=during, wait=60)
    check(r['status'] == 'aborted' and png_size(os.path.join(s.folder, 'summary.png')) == (1920, 1080),
          f'abort: picture exists, {r["status"]}')
    check(rec.ds[0]['note'].startswith('aborted') and 'Abort button' in rec.ds[0]['note'], f'reason on top: {rec.ds[0]["note"]!r}')
    shutil.copy(os.path.join(s.folder, 'summary.png'), os.path.join(TMP, 'summary_abort.png'))

    # error before stream_start(): no summary channel, no curves, still a picture
    rec = Recorder()
    s, r, sc = go('''
def main():
    setup()
    raise ValueError("boom")
''', render_png=rec)
    d = rec.ds[0]
    check(r['status'] == 'error' and png_size(os.path.join(s.folder, 'summary.png')) == (1920, 1080),
          f'script error before stream_start(): picture exists, {r["status"]}')
    check(d['series'] == [] and 'summary_channel() was not set' in d['empty_text'] and 'boom' in d['note'],
          f'no curves, reason given: {d["empty_text"]!r}, {d["note"]!r}')

    # a render_png that raises: the run ends, the log has the line
    s, r, sc = go(SUMMARY_BODY, render_png=Recorder(fail=ValueError('no font')), wait=60)
    check(r['status'] == 'ok' and r['summary_png'] is None and not os.path.exists(os.path.join(s.folder, 'summary.png')),
          f'render_png raises: run ends {r["status"]}, no picture')
    check('summary.png not written' in logs(s) and 'no font' in logs(s) and 'summary.png not written' in run_txt(s),
          'the exception is in the log and in run.txt')

    # more than 12 traces and rows
    rec = Recorder()
    s, r, sc = go('''
def main():
    setup()
    summary_channel("motor.speed")
    table("n")
    stream_start()
    for n in range(14):
        trace_begin()
        wait_ms(5)
        trace_end("t %d", n)
        row(n)
''', render_png=rec, wait=60)
    d = rec.ds[0]
    check(len(d['series']) == 12 and len(d['rows']) == 12 and len(d['notes']) == 2 and 'first 12 of 14 traces' in d['notes'][0]
          and 'first 12 of 14 table rows' in d['notes'][1], f'14 traces and rows: 12 shown, notes {d["notes"]}')


# -- capture mode (card 6.1) ------------------------------------------------------------------------------------------

CAPHEAD = '''from tune_api import *
import time


def setup():
    capture_channels("motor.current_ref", "motor.current")
    capture_period_us(100)
    capture_trigger("motor.current_ref", RISING, 250, 10)
    allow("motor.current_ref", 0, 1000)
    allow("motor.current_kp", 0, 2000)
    allow("motor.current_ki", 0, 500)
    safe_value("motor.current_ref", 0)


def step():
    capture_arm()
    write("motor.current_ref", 500)
    wait_ms(100)
    write("motor.current_ref", 0)
    return capture_wait(2000)
'''
REF, CUR = ADDR['motor.current_ref'], ADDR['motor.current']


class ScaledSerial(LogSerial):
    """A port whose target time runs slower than the wall clock, as MdbSerial does (much less)."""
    time_scale = 4


class TimedSerial(ScaledSerial):
    """Also notes the wall clock of every write to tune_alive and of the start command."""

    def __init__(self):
        super().__init__()
        self.beats, self.starts = [], []

    def write(self, data):
        b = bytes(data)
        if b[:1] == b'W' and struct.unpack('<H', b[1:3])[0] == ALIVE:
            self.beats.append(time.perf_counter())
        elif b[:1] == b'R':
            self.starts.append(time.perf_counter())
        return super().write(b)


class BadHeaderSerial(LogSerial):
    """The header of a capture with 10 % pretrigger says one more pretrigger sample than was asked for.
    Byte by byte: the reader takes what has arrived, so the header can come in pieces."""
    header = 0
    last = b''

    def read(self, n=1):
        out = bytearray(super().read(n))
        for i, b in enumerate(out):
            self.last = (self.last + bytes([b]))[-4:]
            if self.last == b'\xc5\x00\x04\x66':         # sync, depth 1024, low byte of pretrigger 102
                self.header += 1
                out[i] = 0x67
        return bytes(out)


def test_capture():
    s, r, sc = go('''
def main():
    setup()
    limit("motor.current", 1500)
    capture_start()
    write("motor.current_kp", 400)
    printf("WAIT %d\\n", step())
    printf("NUM max %d final %d rise %d length %d\\n", capture_max("motor.current"), capture_final("motor.current"),
           capture_rise_us("motor.current"), capture_length())
    printf("VAL %d %d\\n", capture_value("motor.current", 0), capture_value("motor.current_ref", 150))
    capture_save("kp %d", 400)
''', head=CAPHEAD)
    check(r['status'] == 'ok' and not r['errors'], f'capture run: {r["status"]} {r["reason"]} {r["errors"]}')
    log = logs(s)
    check('WAIT 0' in log, 'the trigger fired, capture_wait() returned 0')
    m = re.search(r'NUM max (\d+) final (\d+) rise (-?\d+) length (\d+)', log)
    vals = [int(x) for x in m.groups()] if m else []
    check(len(vals) == 4 and 495 <= vals[0] <= 510 and vals[1] == 500 and 300 <= vals[2] <= 700 and vals[3] == 1024,
          f'kp 400 against the model (max 502, final 500, rise 500 us, 1024 samples): {vals}')
    check('VAL 0 500' in log, 'capture_value(): first sample 0, sample 150 is after the step')
    with open(os.path.join(s.folder, 'trace_001.csv'), newline='') as f:
        rows = list(csv.reader(f))
    check(rows[0] == ['time_us', 'motor.current_ref', 'motor.current'] and len(rows) == 1025 and rows[1][0] == '-10200'
          and rows[-1][0] == '92100', f'trace_001.csv: {rows[0]}, {len(rows) - 1} rows from {rows[1][0]} to {rows[-1][0]} us')
    check(int(rows[103][0]) == 0 and int(rows[103][1]) >= 250 > int(rows[102][1]), 'time 0 is the trigger sample, 102 samples (10 %) before it')
    check([x[0] for x in sc.ser.log].count('C') == 3 and s.traces[0]['failed'] is False and not s.traces[0]['invalid'],
          'the trial capture, one capture and the read-back of the safe values; the trace is neither failed nor invalid')
    first = next(i for i, x in enumerate(sc.ser.log) if x[0] == 'W' and x[1] != ALIVE)
    check([x[0] for x in sc.ser.log[:first]].count('R') == 1 and any(x[1] == ALIVE for x in sc.ser.log[:first]),
          'the trial capture ran before the first script write, with the heartbeat on')
    check('capture period 100 us, trigger motor.current_ref rising at 250, pretrigger 10 %' in run_txt(s), 'run.txt has the capture setup')
    check(sc.ser.writes(REF)[-2:] == [('W', REF, 0)] * 2 and 'safe values read back: 1 of 1 correct' in log, 'safe values twice, read back')
    t = s.traces[0]['time_ms']
    check(abs(t[0] + 10.2) < 1e-9 and abs(t[1] - t[0] - 0.1) < 1e-9, 'the event has the time in ms, from the pretrigger on')
    tev = [e for e in s.ev if e['type'] == 'trace']
    cev = [e for e in s.ev if e['type'] == 'channels']
    check(len(tev) == 1 and tev[0]['time_unit'] == 'ms' and len(cev) == 1 and cev[0]['names'] == ['motor.current_ref', 'motor.current']
          and cev[0]['period_us'] == 100, 'trace event: time_unit ms; one channels event with the capture channels and period (the overlay of the GUI needs it)')


def test_capture_timeout():
    s, r, sc = go('''
def main():
    setup()
    capture_start()
    t0 = time.time()
    capture_arm()
    printf("ARM %d\\n", int((time.time() - t0) * 1000))
    t0 = time.time()
    got = capture_wait(300)
    printf("WAIT %d %d\\n", got, int((time.time() - t0) * 1000))
    t0 = time.time()
    wait_ms(100)
    printf("MS %d\\n", int((time.time() - t0) * 1000))
    if got != 0:
        printf("RETURNING\\n")
        return
    printf("NOT REACHED\\n")
''', head=CAPHEAD, ser=ScaledSerial())
    log = logs(s)
    arm, wait, ms = (re.search(rf'{k} (-?\d+)(?: (\d+))?', log) for k in ('ARM', 'WAIT', 'MS'))
    arm_ms = int(arm.group(1)) if arm else -1
    check(r['status'] == 'ok' and 'RETURNING' in log and 'NOT REACHED' not in log, f'the return of the script ends the run: {r["status"]} {r["reason"]}')
    check(wait and wait.group(1) == '-1' and 1150 <= int(wait.group(2)) <= 2600,
          f'no trigger: capture_wait(300) gives -1 after {wait and wait.group(2)} ms wall clock at scale 4')
    check(41 <= arm_ms <= 400, f'capture_arm() waits 102 samples x 100 us at scale 4: {arm_ms} ms (at least 41)')
    check(ms and 380 <= int(ms.group(1)) <= 900, f'wait_ms(100) is the scaled wall clock in capture mode: {ms and ms.group(1)} ms')
    check(sc.ser.writes(REF) == [('W', REF, 0)] * 2 and 'safe values read back: 1 of 1 correct' in log and not sc.running,
          'safe end after the timeout: safe values twice, read back, capture stopped')
    check(not s.traces, 'no trace without a capture')


class SlowDumpSerial(ScaledSerial):
    """The dump of the second capture (the one with the trigger) comes in slices of `chunk` bytes, one per `gap` s of
    wall clock. After `stop_at` bytes nothing more comes (None: it goes on to the end). Everything else passes at once."""
    chunk, gap, stop_at, dump_len = 400, 0.3, None, 5 + 1024 * 4

    def __init__(self):
        super().__init__()
        self.timeout = 3        # read(n) waits for all n bytes, up to 3 s, as MdbSerial does (15 s there)
        self.held, self.rs, self.left, self.sent, self.t_out = bytearray(), 0, 0, 0, 0.0

    def write(self, data):
        c = bytes(data)[:1]
        if c == b'R':
            self.rs += 1
            if self.rs == 2:
                self.left, self.sent, self.t_out = self.dump_len, 0, 0.0
        elif c in (b'S', b'C'):
            self.left = 0
            self.held.clear()
        return super().write(data)

    def _advance(self):
        with self.lock:
            before = len(self.buf)
            super()._advance()
            if not self.left:
                self.buf += self.held       # what follows the counted dump, e.g. a byte before its header
                self.held.clear()
                return
            self.held += self.buf[before:]
            del self.buf[before:]
            now = time.perf_counter()
            if self.held and now - self.t_out >= self.gap:
                n = min(self.chunk, self.left, len(self.held), (self.stop_at if self.stop_at is not None else 1 << 30) - self.sent)
                if n > 0:
                    self.buf += self.held[:n]
                    del self.held[:n]
                    self.left -= n
                    self.sent += n
                    self.t_out = now


class StalledDumpSerial(SlowDumpSerial):
    stop_at = 1200


class HeldDumpSerial(StalledDumpSerial):
    """The dump starts and stops part of the way, the rest never comes: only the stop of the safe end ends it.
    So an abort lands in the transfer in every run, whatever the speed of the machine."""
    gap = 0.01

    def __init__(self):
        super().__init__()
        self.timeout = 0.1


SLOWSCRIPT = '''
def main():
    setup()
    capture_start()
    capture_arm()
    write("motor.current_ref", 500)
    wait_ms(100)
    write("motor.current_ref", 0)
    t0 = time.time()
    got = capture_wait(300)
    printf("WAIT %d %d\\n", got, int((time.time() - t0) * 1000))
    if got == 0:
        printf("LEN %d\\n", capture_length())
'''


def test_capture_slow_transfer():
    # capture_wait(300) is 1200 ms of wall clock at scale 4. The dump takes about 3 s, with gaps of 300 ms: a transfer in progress is not timed out
    s, r, sc = go(SLOWSCRIPT, head=CAPHEAD, ser=SlowDumpSerial())
    log = logs(s)
    m = re.search(r'WAIT (-?\d+) (\d+)', log)
    check(r['status'] == 'ok', f'slow transfer: {r["status"]} {r["reason"]}')
    check(m and m.group(1) == '0' and int(m.group(2)) > 1300, f'slow transfer: capture_wait(300) returns 0 after {m and m.group(2)} ms, longer than its timeout of 1200 ms')
    check('LEN 1024' in log, 'the whole capture arrived')
    check(sc.ser.writes(REF)[-2:] == [('W', REF, 0)] * 2 and not sc.running, 'safe end')


def test_capture_stalled_transfer():
    # the dump stops after 1200 bytes: no byte for 1200 ms of wall clock is the timeout
    s, r, sc = go(SLOWSCRIPT, head=CAPHEAD, ser=StalledDumpSerial())
    log = logs(s)
    m = re.search(r'WAIT (-?\d+) (\d+)', log)
    check(r['status'] == 'ok', f'stalled transfer: {r["status"]} {r["reason"]}')
    check(m and m.group(1) == '-1' and 1150 <= int(m.group(2)) <= 3000, f'stalled transfer: capture_wait(300) gives -1 after {m and m.group(2)} ms')
    check('LEN' not in log and not s.traces, 'no capture was used')
    check(sc.ser.writes(REF)[-2:] == [('W', REF, 0)] * 2 and 'safe values read back: 1 of 1 correct' in log and not sc.running,
          'safe end: safe values twice, read back, capture stopped')


def test_capture_arm_waits():
    # the pretrigger of 10 % of 1024 samples at 1 ms is 102 ms: a step written right after capture_arm() returns must fire the trigger
    s, r, sc = go('''
def main():
    setup()
    capture_period_us(1000)
    capture_start()
    capture_arm()
    write("motor.current_ref", 500)
    wait_ms(100)
    write("motor.current_ref", 0)
    printf("WAIT %d\\n", capture_wait(3000))
    printf("RISE %d\\n", capture_rise_us("motor.current"))
    capture_save("one")
''', head=CAPHEAD, ser=ScaledSerial(), wait=60)
    check(r['status'] == 'ok' and 'WAIT 0' in logs(s), f'step right after capture_arm() fires: {r["status"]} {r["reason"]}')
    with open(os.path.join(s.folder, 'trace_001.csv'), newline='') as f:
        rows = list(csv.reader(f))
    check(rows[0][0] == 'time_ms' and rows[1][0] == '-102' and rows[-1][0] == '921',
          f'a period of 1 ms gives a time_ms column: {rows[0][0]} {rows[1][0]} ... {rows[-1][0]}')


LIMIT_BODY = '''
def main():
    setup()
    limit("motor.current", 200)
    capture_start()
    capture_arm()
    write("motor.current_ref", 500)
    wait_ms(100)
    write("motor.current_ref", 0)
    printf("STEPPED\\n")
    capture_wait(2000)
    printf("NOT REACHED\\n")
'''


def test_capture_limit():
    s, r, sc = go(LIMIT_BODY, head=CAPHEAD)
    check(r['status'] == 'aborted' and r['reason'].startswith('limit exceeded: motor.current = ') and 'in the capture, limit 200' in r['reason'],
          f'limit() on the buffer after the transfer: {r["reason"]}')
    check('STEPPED' in logs(s) and 'NOT REACHED' not in logs(s), 'the step went through while the capture ran, the script stopped at capture_wait()')
    check(s.traces and s.traces[-1]['unfinished'] and s.traces[-1]['values']['motor.current'].max() > 200,
          'the capture that tripped it is kept as unfinished')
    check(sc.ser.writes(REF)[-2:] == [('W', REF, 0)] * 2 and not sc.running and peek(sc, 'motor.current_ref') == [0],
          'safe values written, capture stopped')


def test_mut_capture_limit():
    with mock.patch.object(tune_api.Session, '_cap_limits', lambda self, t, cols: None):
        s, r, sc = go(LIMIT_BODY, head=CAPHEAD)
    check(r['status'] == 'ok', f'MUTATION no limit check on the buffer: the run is not aborted ({r["status"]}), the limit test can fail')


def test_capture_abort():
    # 1. while it waits for a trigger that never comes
    t0 = []

    def during(s):
        wait_log(s, 'WAITING')
        t0.append(time.time())
        tune_api.abort('Abort button')

    s, r, sc = go('''
def main():
    setup()
    capture_start()
    capture_arm()
    printf("WAITING\\n")
    capture_wait(60000)
    printf("NOT REACHED\\n")
''', head=CAPHEAD, during=during)
    check(r['status'] == 'aborted' and 'NOT REACHED' not in logs(s) and time.time() - t0[0] < 3,
          f'abort in capture_wait(): {r["status"]}, over after {time.time() - t0[0]:.1f} s')
    check(sc.ser.writes(REF) == [('W', REF, 0)] * 2 and not sc.running and sc.thread is None and ('S', None, None) in sc.ser.log[-12:],
          'safe values twice, Scope stopped')
    # 2. during the transfer: the step is over, the port has given 1200 bytes of the dump and holds the rest
    t0.clear()
    inside = []

    def during2(s):
        wait_log(s, 'STEPPED')
        end = time.time() + 5
        while s.scope.dump_bytes < 1000 and time.time() < end:
            time.sleep(0.005)
        inside.extend((s.scope.captures, s.scope.dump_bytes))
        t0.append(time.time())
        tune_api.abort('Abort button')

    s, r, sc = go('''
def main():
    setup()
    capture_start()
    capture_arm()
    write("motor.current_ref", 500)
    wait_ms(100)
    write("motor.current_ref", 0)
    printf("STEPPED\\n")
    capture_wait(60000)
    printf("NOT REACHED\\n")
''', head=CAPHEAD, ser=HeldDumpSerial(), during=during2)
    check(inside[0] == 0 and inside[1] >= 1000 and sc.captures == 0,
          f'the abort came during the transfer (captures, dump bytes at the abort: {inside}, of {SlowDumpSerial.dump_len})')
    check(r['status'] == 'aborted' and 'NOT REACHED' not in logs(s) and time.time() - t0[0] < 3,
          f'abort during the transfer: {r["status"]}, over after {time.time() - t0[0]:.1f} s')
    check(sc.ser.writes(REF)[-2:] == [('W', REF, 0)] * 2 and not sc.running and sc.thread is None and peek(sc, 'motor.current_ref') == [0],
          'safe values twice, Scope stopped, the target answers afterwards')
    check(tune_api._active is None and not s.hb_thread.is_alive(), 'no run active, the heartbeat thread has ended')
    # 3. a script that ends after capture_arm() without waiting
    s, r, sc = go('def main():\n    setup()\n    capture_start()\n    capture_arm()\n', head=CAPHEAD)
    check(r['status'] == 'ok' and not sc.running and sc.ser.writes(REF) == [('W', REF, 0)] * 2,
          f'the end while armed: {r["status"]}, capture stopped')


def test_capture_heartbeat():
    # The code writes 200 ms x 4 = 800 ms after the last write returned. On top come one poll step, the write and the
    # time until the thread runs, about 20 ms when nothing holds the interpreter; a wall clock loop promises no more
    # than that. sim.dll runs in real time whatever time_scale says, so tune_block.h times out after 1000 ms of wall
    # clock here. That is the bound for each gap (at the nominal scale 4 it would be 4000 ms). The run takes at least
    # 3 x 400 ms + 4000 ms, so with every gap below 1000 ms there are at least 6 writes.
    most = 1.0
    ser = TimedSerial()
    s, r, sc = go('''
def main():
    setup()
    capture_start()
    write("motor.current_kp", 400)
    for n in range(3):
        step()
    wait_ms(1000)
''', head=CAPHEAD, ser=ser, wait=60)
    check(r['status'] == 'ok', f'3 captures at scale 4: {r["status"]} {r["reason"]}')
    gaps = [b - a for a, b in zip(ser.beats, ser.beats[1:])]
    around = [b - a for a, b in zip(ser.beats, ser.beats[1:]) if any(a < t < b for t in ser.starts)]
    check(len(ser.beats) >= 6 and max(gaps) < most,
          f'heartbeat every 200 ms of target time on the wall clock: {len(ser.beats)} writes, largest gap {1000 * max(gaps):.0f} ms (scale 4: 800 nominal, limit {1000 * most:.0f})')
    check(around and max(around) < most, f'no gap around the captures: {[round(1000 * x) for x in around]} ms')
    m = re.search(r'heartbeat: largest gap between two writes (\d+) ms wall clock, (\d+) ms target time at scale 4; around a capture (\d+) ms', run_txt(s))
    check(m and int(m.group(2)) < 1000 * most / 4 and int(m.group(3)) > 0, f'run.txt has the measured heartbeat gap: {m and m.groups()}')


def test_throttling_line():
    # a port that knows full_speed() reports it on the port line of run.txt; 0 processes is a warning, never an error
    for n in (2, 0):
        ser = LogSerial()
        ser.throttling_off = n
        s, r, sc = go('def main():\n    printf("HI\n")\n', ser=ser)
        txt = run_txt(s)
        port = [ln for ln in txt.splitlines() if ln.startswith('port ')]
        warn = 'power throttling of the simulator is not turned off' in txt
        check(port and f'power throttling off for {n} processes' in port[0], f'run.txt port line with {n} processes: {port}')
        check(warn == (n == 0), f'warning line in run.txt only for 0 processes (n={n}, warning {warn})')
    s, r, sc = go('def main():\n    printf("HI\n")\n')
    port = [ln for ln in run_txt(s).splitlines() if ln.startswith('port ')]
    check(port and 'throttling' not in port[0] and 'throttling' not in run_txt(s), 'a port without it prints nothing about it')


def test_row_status():
    check(tune_api.row_status(False, []) == 'ok' and tune_api.row_status(True, []) == 'invalid', 'row status without traces')
    check(tune_api.row_status(False, [{'failed': True, 'invalid': False, 'unfinished': False}, {}]) == 'failed', 'one failed trace makes the row failed')
    check(tune_api.row_status(True, [{'failed': True}, {'unfinished': True}]) == 'failed, invalid, unfinished', 'all flags, in a fixed order')
    rec = Recorder()
    s, r, sc = go('''
def main():
    setup()
    summary_channel("motor.current")
    table("n")
    capture_start()
    step()
    trace_failed()
    capture_save("bad")
    row(1)
    step()
    capture_save("good")
    row(2)
''', head=CAPHEAD, render_png=rec, wait=30)
    with open(os.path.join(s.folder, 'results.csv'), newline='') as f:
        csv_rows = list(csv.reader(f))[1:]
    meta = [e for e in s.ev if e['type'] == 'trace']
    rows = [{'values': e['values'], 'invalid': e['invalid'], 'traces': e['row_traces']} for e in s.ev if e['type'] == 'row']
    gui = [x['status'] for x in script_tab.table_view(['n'], rows, meta, None, set())[1]]
    check(csv_rows == [['1', 'failed'], ['2', 'ok']], f'results.csv follows the trace marked failed: {csv_rows}')
    check(gui == [x[1] for x in csv_rows], f'the table of the GUI says the same: {gui}')
    check([x[-1] for x in rec.ds[0]['rows']] == gui, f'the summary says the same: {rec.ds[0]["rows"]}')


def test_capture_errors():
    def err(body, want, name, head=CAPHEAD, table=TABLE):
        s, r, sc = go(body, head=head, table=table)
        line = line_of(s, '# LINE')
        check(r['status'] == 'error' and r['reason'].startswith(f't.py:{line}: error: ') and want in r['reason'], f'{name}: {r["reason"]}')
        w = sc.ser.writes(REF)
        check(not w or w[-2:] == [('W', REF, 0)] * 2, f'{name}: the last writes are the safe values')

    err('def main():\n    setup()\n    capture_start()\n    x = read("motor.current")  # LINE\n', 'does not work in capture mode', 'read() in capture mode')
    err('def main():\n    setup()\n    stream_start()  # LINE\n', 'cannot be mixed', 'stream_start() with capture_channels()')
    err('def main():\n    channels("motor.speed")\n    capture_channels("motor.current")  # LINE\n', 'cannot be mixed', 'capture_channels() after channels()')
    err('def main():\n    setup()\n    capture_start()\n    trace_begin()  # LINE\n', 'does not work in capture mode', 'trace_begin() in capture mode')
    err('def main():\n    setup()\n    capture_arm()  # LINE\n', 'before capture_start()', 'capture_arm() before capture_start()')
    err('def main():\n    setup()\n    capture_start()\n    capture_wait(100)  # LINE\n', 'without capture_arm()', 'capture_wait() without capture_arm()')
    err('def main():\n    setup()\n    capture_start()\n    capture_arm()\n    capture_arm()  # LINE\n', 'capture_wait() is missing', 'capture_arm() twice')
    err('def main():\n    setup()\n    capture_start()\n    x = capture_max("motor.current")  # LINE\n', 'no capture', 'capture_max() before a capture')
    err('def main():\n    setup()\n    capture_start()\n    trace_failed()  # LINE\n', 'no capture', 'trace_failed() before a capture')
    err('def main():\n    setup()\n    capture_start()\n    step()\n    x = capture_value("motor.current", 1024)  # LINE\n', 'from 0 to 1023',
        'capture_value() out of range')
    err('def main():\n    setup()\n    capture_start()\n    step()\n    x = capture_max("motor.speed")  # LINE\n', 'not in capture_channels()',
        'capture_max() of another variable')
    err('def main():\n    setup()\n    capture_start()\n    capture_start()  # LINE\n', 'was already called', 'capture_start() twice')
    err('def main():\n    setup()\n    capture_start()\n    capture_period_us(50)  # LINE\n', 'must come before capture_start()', 'declaration after capture_start()')
    err('def main():\n    capture_period_us(0)  # LINE\n', '1 to 65535', 'period 0')
    err('def main():\n    capture_period_us(9)  # LINE\n', 'below 10 us', 'period 9 is under the floor')
    err('def main():\n    capture_channels("motor.current_ref", "motor.current", "motor.kp", "motor.ki", "motor.run")  # LINE\n', 'at most 4', 'five channels')
    err('def main():\n    capture_channels("motor.current_ref")\n    capture_trigger("motor.current_ref", 3, 250, 10)  # LINE\n',
        'RISING or FALLING', 'edge')
    err('def main():\n    capture_channels("motor.current_ref")\n    capture_trigger("motor.current_ref", RISING, 70000, 10)  # LINE\n',
        'inside the range of s16', 'level outside the type')
    err('def main():\n    capture_channels("motor.current_ref")\n    capture_trigger("motor.current_ref", RISING, 250, 120)  # LINE\n',
        '0 to 100 percent', 'pretrigger')
    table = VarTable.from_symbols({**SYMBOLS, 'probef': (KP, 4)}, {**SYMBOL_TYPES, 'probef': 'f32'})
    err('def main():\n    capture_channels("probef")\n    capture_trigger("probef", RISING, 250, 10)  # LINE\n', 'is a float', 'f32 trigger', table=table)
    # refusals at capture_start(): nothing reaches the target
    refused('def main():\n    capture_channels("motor.current")\n    allow("motor.run", 0, 1)\n    safe_value("motor.run", 0)\n    capture_start()\n',
            'no capture_trigger()', head=CAPHEAD)
    refused('def main():\n    setup()\n    capture_trigger("motor.kp", RISING, 5, 10)\n    capture_start()\n', 'not in capture_channels()',
            head=CAPHEAD, name='trigger not in the channels')
    refused('def main():\n    setup()\n    limit("motor.kp", 100)\n    capture_start()\n', 'not in channels()', head=CAPHEAD, name='limit not measured')
    refused('def main():\n    capture_channels("motor.current")\n    capture_trigger("motor.current", RISING, 5, 10)\n    safe_value("motor.run", 0)\n'
            '    capture_start()\n', 'no allow()', head=CAPHEAD)
    refused('def main():\n    setup()\n    capture_start()\n', 'tune_alive', head=CAPHEAD,
            table=VarTable.from_symbols({k: v for k, v in SYMBOLS.items() if k != 'tune_alive'}, SYMBOL_TYPES))
    slow = LogSerial()
    slow.baudrate = 9600
    refused('def main():\n    setup()\n    capture_start()\n', 'takes 4272 ms at this baud rate', head=CAPHEAD, ser=slow, name='transfer at 9600 baud')
    fast = LogSerial()
    fast.baudrate = 57600
    s, r, sc = go('def main():\n    setup()\n    capture_start()\n', head=CAPHEAD, ser=fast)
    check(r['status'] == 'ok', f'57600 baud: 712 ms per capture is accepted: {r["reason"]}')


def test_capture_invalid():
    ser = BadHeaderSerial()
    s, r, sc = go('''
def main():
    setup()
    table("kp")
    capture_start()
    step()
    capture_save("bad")
    row(1)
''', head=CAPHEAD, ser=ser)
    check(ser.header == 1 and r['status'] == 'ok', f'the header of the capture was changed once: {ser.header}, {r["status"]} {r["reason"]}')
    check(s.traces[0]['invalid'] and 'marked invalid' in logs(s) and 'starting at -103' in logs(s),
          'a capture of another shape is marked invalid and says so')
    with open(os.path.join(s.folder, 'results.csv'), newline='') as f:
        rows = list(csv.reader(f))
    check(rows[1] == ['1', 'invalid'] and any(e['type'] == 'row' and e['invalid'] for e in s.ev), f'the row of it is invalid: {rows[1]}')
    # failed marking, a unit column, the summary picture
    rec = Recorder()
    s, r, sc = go('''
def main():
    setup()
    unit("motor.current", 20.0 / 32768, "A")
    summary_channel("motor.current")
    capture_start()
    step()
    trace_failed()
    capture_save("failed one")
    step()
    capture_save("good one")
''', head=CAPHEAD, render_png=rec, wait=60)
    check(r['status'] == 'ok' and [t['failed'] for t in s.traces] == [True, False] and [t['invalid'] for t in s.traces] == [False, False],
          f'trace_failed() marks the capture that capture_save() stores: {[t["failed"] for t in s.traces]}')
    with open(os.path.join(s.folder, 'trace_001.csv'), newline='') as f:
        head_row = next(csv.reader(f))
    check(head_row == ['time_us', 'motor.current_ref', 'motor.current', 'motor.current [A]'], f'CSV with the physical column: {head_row}')
    d = rec.ds[0]
    names = [x[0] for x in d['series']]
    x0 = d['series'][0][1]
    check(names == ['001 failed one (failed)', '002 good one'] and d['series'][0][3]['linestyle'] == '--' and abs(x0[0] + 10.2) < 1e-9
          and abs(x0[1] - x0[0] - 0.1) < 1e-9 and abs(x0[102]) < 1e-9,
          f'summary: dashed failed capture, x in ms with 0 at the trigger: {names}, {x0[0]}, {x0[102]}')
    check(d['ylabel'] == 'motor.current [A]', f'axis: {d["ylabel"]}')
    # a capture that arrived and was not saved is kept when the run ends
    s, r, sc = go('def main():\n    setup()\n    capture_start()\n    step()\n', head=CAPHEAD)
    check(r['status'] == 'ok' and len(s.traces) == 1 and s.traces[0]['unfinished'] and 'trace 001 "unfinished"' in run_txt(s),
          f'a capture that arrived and was not saved is kept as unfinished: {len(s.traces)}')


def hand_session(period=100, pre=102, n=1024):
    """A Session with a capture put in by hand, no target: curves of known numbers."""
    folder = tempfile.mkdtemp(dir=TMP)
    path = os.path.join(folder, 't.py')
    with open(path, 'w') as f:
        f.write('def main():\n    pass\n')
    sess = tune_api.Session(path, Scope(SimSerial()), TABLE, None, folder, 'x.elf', {})
    sess.run_file.close()
    sess.chans = [TABLE.lookup('motor.current')]
    sess.cap = {'t': np.arange(n) - pre, 'cols': [np.zeros(n)], 'pre': pre, 'period': period, 'invalid': False}
    return sess


def test_capture_numbers():
    ramp = np.zeros(1024)
    ramp[110:210] = 10.0 * np.arange(100)
    ramp[210:] = 1000
    ramp[300] = 1100
    sess = hand_session()
    sess.cap['cols'] = [ramp]
    got = (sess.capture_max('motor.current'), sess.capture_final('motor.current'), sess.capture_rise_us('motor.current'),
           sess.capture_length())
    check(got == (1100, 1000, 8000, 1024), f'ramp 0 to 1000 in 100 samples, peak 1100: max, final, rise (10 % at sample 120, 90 % at 200), length: {got}')
    check(all(type(x) is int for x in got), 'raw integers, as in C')
    sess.cap['cols'] = [-0.5 * ramp]
    check(sess.capture_rise_us('motor.current') == 8000 and sess.capture_final('motor.current') == -500, 'a falling step: the same rise time')
    sess.cap['cols'] = [ramp]
    sess.cap['period'] = 250
    check(sess.capture_rise_us('motor.current') == 20000, 'the period scales the rise time: 80 samples x 250 us')
    sess.cap['period'] = 100
    sess.cap['cols'] = [np.zeros(1024)]
    check(sess.capture_rise_us('motor.current') == -1, 'a flat capture has no rise time: -1')
    pulse = np.where((np.arange(1024) > 200) & (np.arange(1024) < 400), 800.0, 0.0)
    sess.cap['cols'] = [pulse]
    check(sess.capture_rise_us('motor.current') == -1, 'a pulse that ends before the buffer does: the step to the final value is 0, -1')
    tail = np.zeros(1024)
    tail[-102:] = np.r_[np.full(51, -500.0), np.full(51, -499.0)]
    sess.cap['cols'] = [tail]
    check(sess.capture_final('motor.current') == -499, f'-499.5 truncates towards 0 like C integer division: {sess.capture_final("motor.current")}')
    sess.cap['t'], sess.cap['pre'] = np.arange(1024), 0
    sess.cap['cols'] = [np.r_[np.zeros(5), np.full(1019, 100.0)]]
    check(sess.capture_rise_us('motor.current') == 0, 'no pretrigger: the first sample is the base, a jump of one sample has rise 0')
    try:
        sess.capture_value('motor.current', 1024)
        check(False, 'capture_value() out of range')
    except tune_api.ScriptError as e:
        check('from 0 to 1023' in str(e), f'capture_value(1024): {e}')


def test_current():
    """scripts/tune_current.py on SIM through tools/run_script.py, as a subprocess: exit code, table, tuning.h, report."""
    import subprocess
    work = tempfile.mkdtemp(dir=TMP)
    tool = os.path.join(HERE, '..', 'tools', 'run_script.py')
    script = os.path.join(HERE, '..', 'scripts', 'tune_current.py')
    t0 = time.time()
    p = subprocess.run([sys.executable, tool, '--port', 'SIM', '--out', 'runs', script], cwd=work, capture_output=True, text=True, timeout=180)
    check(p.returncode == 0, f'tune_current.py on SIM: exit code {p.returncode} after {time.time() - t0:.0f} s')
    folders = os.listdir(os.path.join(work, 'runs')) if os.path.isdir(os.path.join(work, 'runs')) else []
    check(len(folders) == 1, f'one run folder: {folders}')
    folder = os.path.join(work, 'runs', folders[0]) if folders else work
    with open(os.path.join(folder, 'results.csv'), newline='') as f:
        rows = list(csv.reader(f))
    with open(os.path.join(HERE, '..', 'docs', 'evidence', 'm1', 'model_check.txt'), encoding='utf-8') as f:
        text = f.read()
    known = {}
    for line in text.split('== tune_current.py')[1].splitlines()[2:6]:
        kp, rise, over = line.split()[:3]
        known[kp] = (int(rise), int(over))
    check(known == {'100': (2500, 3), '200': (2000, 0), '400': (500, 0), '800': (200, 47)}, f'model_check.txt: {known}')
    table = {r[0]: (int(r[1]), int(r[2]), r[3]) for r in rows[1:]}
    check(rows[0] == ['kp', 'rise_us', 'overshoot_percent', 'status'] and sorted(table) == sorted(known), f'results.csv: {rows}')
    # the model runs on the wall clock, the capture and the model tick differ in phase: one sample of 100 us
    ok = all(abs(table[k][0] - known[k][0]) <= 100 and abs(table[k][1] - known[k][1]) <= 1 and table[k][2] == ('failed' if k == '800' else 'ok') for k in known)
    check(ok, f'rise time and overshoot against model_check.txt (rise within 100 us, overshoot within 1 %): {table}')
    traces = sorted(x for x in os.listdir(folder) if x.startswith('trace_'))
    check(len(traces) == 7, f'4 traces of the gains and 3 confirming traces: {len(traces)}')
    with open(os.path.join(folder, 'tuning.h')) as f:
        h = f.read()
    for want in ('#define CURRENT_KP 400\n', '#define CURRENT_KI 20\n'):
        check(want in h, f'tuning.h has {want.strip()}')
    with open(os.path.join(folder, 'run.txt'), encoding='utf-8') as f:
        txt = f.read()
    for want in ('trace 004 "kp 800": 1024 samples, failed', 'trace 007 "confirm 3": 1024 samples, ok', 'result ok', 'motor.current_ref = 0 (safe)',
                 'capture period 100 us, trigger motor.current_ref rising at 250, pretrigger 10 %', 'heartbeat: largest gap'):
        check(want in txt, f'run.txt has "{want}"')
    seq = re.findall(r'^write \d+ ms motor.current_kp = (\d+)$', txt, re.M)
    check(seq == ['100', '200', '400', '800', '400'], f'run.txt: the gains written by the script: {seq}')
    check(len(re.findall(r'^write \d+ ms motor.current_ref = 500$', txt, re.M)) == 7, 'run.txt: 7 steps')
    with open(os.path.join(work, 'run_report.txt'), encoding='utf-8') as f:
        rep = f.read()
    check('status ok' in rep and 'port SIM' in rep and 'script tune_current.py' in rep and 'W203 x1, W204 x2' in rep, 'run_report.txt')
    bad = [w for w in ('motor', 'kp ', 'CURRENT', TMP, work, os.sep) if w in rep]
    check(not bad, f'run_report.txt holds no path, name or value: {bad}')


TESTS = {'values': test_values, 'refuse_stream': test_refuse_stream, 'refuse_write': test_refuse_write, 'limit': test_limit,
         'abort': test_abort_in_wait, 'stuck': test_stuck, 'exception': test_exception, 'race': test_race, 'silent': test_silent,
         'port': test_port_error, 'readback': test_readback, 'heartbeat': test_heartbeat, 'folder': test_folder,
         'invalid': test_invalid, 'session': test_session, 'receiver': test_stuck_receiver,
         'trial_end': test_trial_end, 'guard': test_guard, 'width': test_limit_width, 'startup': test_startup, 'speed': test_speed,
         'cursor': test_cursor, 'cursor_read': test_cursor_read,
         'summary': test_summary, 'capture': test_capture, 'capture_timeout': test_capture_timeout, 'capture_slow': test_capture_slow_transfer,
         'capture_stalled': test_capture_stalled_transfer,
         'capture_arm': test_capture_arm_waits, 'capture_limit': test_capture_limit, 'mut_capture_limit': test_mut_capture_limit,
         'capture_abort': test_capture_abort, 'capture_heartbeat': test_capture_heartbeat, 'throttling_line': test_throttling_line, 'row_status': test_row_status, 'capture_errors': test_capture_errors,
         'capture_invalid': test_capture_invalid, 'capture_numbers': test_capture_numbers, 'current': test_current}

for name, fn in TESTS.items():
    if len(sys.argv) < 2 or name in sys.argv[1:]:
        print(f'--- {name}')
        fn()
shutil.rmtree(TMP, ignore_errors=True)
print(f'runs left active: {tune_api._active}')
sys.exit(1 if getattr(check, 'failed', False) else 0)
