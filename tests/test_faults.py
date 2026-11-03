"""test_faults.py - what goes wrong, in SIM (card M7c): faults of the link, the port, the target and the script.

    python tests/test_faults.py                  all cases
    python tests/test_faults.py silent reset     only the cases with these names (see CASES at the end)

Written from SCRIPTING.md ("How a run ends", section 5; the tuning block, section 6), the plan ("Script errors")
and TROUBLESHOOTING.md, not from the code. The faults come from tests/fault_serial.py. Every case checks: the run ends
within a bound, status and reason, the safe values written and read back where the port still exists, run.txt, and
that nothing keeps running. Bounds, all from the documents:
    STALL_S   0.5 s   "no sample for 500 ms" ends a run (SCRIPTING.md section 5)
    END_S     5.0 s   a run is aborted after 5 s at the latest ("Closing the browser aborts a run after 5 s", section 5)
    SLACK_S   1.0 s   scheduling of the test threads
A case whose behaviour is against the documents is a finding: xfail() prints XFAIL with the finding number and does not
fail the run, the day the behaviour is right it prints FAIL so that the mark is removed.

Showing that a case can fail:   MS_FAULT_OFF=<case> python tests/test_faults.py <case>
switches off the fault of that case (the injectors of FaultSerial and the script mutations do nothing), the case must FAIL.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
sys.path[:0] = [HERE, os.path.join(ROOT, 'gui'), os.path.join(ROOT, 'tools')]
import tune_api                                   # noqa: E402
import tune_check                                 # noqa: E402
from elf_vars import VarTable                     # noqa: E402
from fault_serial import FaultSerial, fault_on    # noqa: E402
from scope_gui import Scope                       # noqa: E402
from sim_serial import SYMBOLS, SYMBOL_TYPES      # noqa: E402

TABLE = VarTable.from_symbols(SYMBOLS, SYMBOL_TYPES)
ADDR = {n: a for n, (a, _) in SYMBOLS.items()}
RUN, RAMP, ALIVE, FAULT = ADDR['motor.run'], ADDR['startup.ramp_rate'], ADDR['tune_alive'], ADDR['motor.fault']
SPEED, KP, CREF, BUILD = ADDR['motor.speed'], ADDR['motor.current_kp'], ADDR['motor.current_ref'], ADDR['tune_build_id']
TMP = tempfile.mkdtemp(prefix='fault_test_')
STARTUP = os.path.join(ROOT, 'scripts', 'tune_startup.py')
CURRENT = os.path.join(ROOT, 'scripts', 'tune_current.py')
threading.excepthook = lambda a: None           # Scope's reader dies with the port

STALL_S, END_S, SLACK_S = 0.5, 5.0, 1.0
BOUND = STALL_S + END_S + SLACK_S               # from the fault to the result when the run has to notice the fault
TUNE_THREADS = ('tune-script', 'tune-monitor', 'tune-heartbeat', 'tune-port', 'tune-events')

HEAD = '''from tune_api import *


def setup():
    channels("motor.speed", "motor.state", "motor.fault", "motor.run")
    allow("motor.run", 0, 1)
    allow("startup.ramp_rate", 1, 500)
    allow("startup.align_current", 0, 1000)
    allow("startup.handover_speed", 100, 2000)
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

XFAILS = []


def check(cond, text):
    print(('OK   ' if cond else 'FAIL ') + text)
    if not cond:
        check.failed = True
    return cond


def xfail(finding, cond, text):
    """A known finding: the behaviour is against the documents."""
    if cond:
        print(f'FAIL {finding} is fixed, remove the xfail: {text}')
        check.failed = True
    else:
        print(f'XFAIL {finding}: {text}')
        XFAILS.append(finding)
    return cond


class Run:
    """The outcome of one run."""
    s = r = sc = ser = None
    t_end = None
    refused = None
    ev = ()


def go(src=None, ser=None, case=None, hooks=None, during=None, trial=300, wait=40, probe=False, table=TABLE, path=None, after_ping=None):
    """Runs a script (text src or file path) on a FaultSerial with tune_api.run(). probe=False skips stages 1 to 4 as
    the small scripts of test_script.py do (hooks skip_checks, skip_probe, skip_build_id); probe=True runs the lot."""
    tune_api.TRIAL_MS = trial
    h = {} if probe else {'skip_checks': True, 'skip_probe': True, 'skip_build_id': True}
    h.update(hooks or {})
    folder = tempfile.mkdtemp(dir=TMP)
    dest = os.path.join(folder, 't.py')
    if path:
        shutil.copy(path, dest)
    else:
        with open(dest, 'w') as f:
            f.write(HEAD + src)
    run = Run()
    run.ser = ser or FaultSerial(case)
    run.sc = Scope(run.ser)
    run.sc.ping()
    if after_ping:
        after_ping()
    run.ev = ev = []

    def receive(e):
        ev.append(e)
        if e['type'] == 'result':
            run.t_end = time.monotonic()
    try:
        run.s = tune_api.run(dest, run.sc, table, on_event=receive, out_dir=os.path.join(folder, 'runs'), elf_name='x.elf', hooks=h)
    except tune_api.RunRefused as e:
        run.refused = str(e)
        run.path = dest
        return run
    run.path = dest
    if during:
        during(run)
    run.r = run.s.wait(wait)
    if run.r is None:
        check(False, 'the run did not end in time')
        tune_api.abort('test timeout')
        run.s.wait(10)
        run.r = run.s.result
    return run


def logs(run):
    return '\n'.join(e['text'] for e in list(run.ev) if e['type'] == 'log')


def run_txt(run):
    with open(os.path.join(run.s.folder, 'run.txt'), encoding='utf-8') as f:
        return f.read()


def wait_log(run, text, secs=30):
    end = time.time() + secs
    while time.time() < end:
        if text in logs(run):
            return True
        time.sleep(0.005)
    return False


def line_of(path, text):
    with open(path, encoding='utf-8') as f:
        return next(i for i, x in enumerate(f, 1) if text in x)


def peek(sc, *names):
    if sc.start([(ADDR[n], SYMBOL_TYPES[n]) for n in names], 0, 1000):
        return None
    time.sleep(0.05)
    _, cols = sc.snapshot()
    sc.stop()
    return [int(c[-1]) for c in cols]


def quiet(run, what, port_alive=True):
    """Nothing keeps running: no thread of the session, no active run, the reader of the port ended, the heartbeat
    writes nothing more, the stream of the target is stopped."""
    end = time.time() + 2.5
    while time.time() < end and any(t.name in TUNE_THREADS and t.is_alive() for t in threading.enumerate()):
        time.sleep(0.05)
    alive = [t.name for t in threading.enumerate() if t.name in TUNE_THREADS and t.is_alive()]
    th = run.sc.thread
    check(not alive and tune_api._active is None, f'{what}: no session thread left {alive}, no active run')
    check(th is None or not th.is_alive(), f'{what}: the reader of the port ended')
    if port_alive:
        n = len(run.ser.log)
        time.sleep(0.4)
        check(len(run.ser.log) == n, f'{what}: nothing written after the end (heartbeat stopped)')
        check(not run.ser.in_stream, f'{what}: the stream of the target is stopped (last command {[x[0] for x in run.ser.log if x[0] != "W"][-1]})')


def ended(run, what, status, reason, bound=BOUND, port_alive=True, safe=True, readback=True, addr=RUN, since_fault=True):
    """The common checks of a case. reason: text the reason has to contain. Returns the elapsed time."""
    r, ser = run.r, run.ser
    ok = check(r is not None and r['status'] == status, f'{what}: status {r and r["status"]!r} (want {status!r}), reason {r and r["reason"][:110]!r}')
    check(r is not None and reason in r['reason'], f'{what}: the reason says "{reason}"')
    dt = None
    if since_fault and ser.t_fault is not None and run.t_end:
        dt = run.t_end - ser.t_fault
        check(dt <= bound, f'{what}: result {dt:.1f} s after the fault (bound {bound:.1f} s)')
    elif run.t_end:
        dt = run.t_end - run.s.t_start
    if ok and r is not None:
        txt = run_txt(run)
        check(f'result {status}' in txt and 'reason ' in txt, f'{what}: run.txt has the result and the reason')
    if safe and port_alive:
        sent = ser.writes(addr)
        check(sent[-2:] == [0, 0], f'{what}: safe value written twice ({sent[-3:]})')
        if readback:
            check('safe values read back: 1 of 1 correct' in logs(run), f'{what}: safe values read back correct')
    quiet(run, what, port_alive)
    return dt


def settle(run):
    run.ser.silent = False
    run.ser.gone_at = None
    run.ser.unstick()


# -- link ---------------------------------------------------------------------------------------------------------

SWEEP = '''
def main():
    setup()
    table("ramp")
    stream_start()
    trace_begin()
    printf("UP\\n")
    wait_ms(700)
    trace_end("x")
    row(1)
    define("X", 1)
'''

# the confirming trace after the last row(): a damaged stream there keeps tuning.h from being written (SCRIPTING.md, define())
CONFIRM = '''
def main():
    setup()
    table("ramp")
    stream_start()
    trace_begin()
    wait_ms(100)
    trace_end("sweep")
    row(1)
    trace_begin()
    printf("UP\\n")
    wait_ms(700)
    trace_end("confirm")
    define("X", 1)
'''


def link_case(case, inject, what):
    """A damaged stream during a trace: the run goes on, the trace and the row are marked, no tuning.h."""
    ser = FaultSerial(case)
    run = go(CONFIRM, ser=ser, during=lambda run: (wait_log(run, 'UP'), inject(ser)))
    r = run.r
    check(r['status'] == 'ok', f'{what}: the run itself goes on to its end, status {r["status"]!r} {r["reason"][:80]!r}')
    tr = run.s.traces[1] if len(run.s.traces) > 1 else {}
    check(tr.get('invalid') is True and not run.s.traces[0]['invalid'], f'{what}: the trace with the damage is marked invalid, the one before is not')
    check(r['tuning_h'] is None and 'tuning.h not written' in logs(run), f'{what}: no tuning.h although define() was called')
    check('invalid' in run_txt(run), f'{what}: run.txt says invalid')
    check(len(tr.get('time_ms', [])) >= 600, f'{what}: the stream recovered, {len(tr.get("time_ms", []))} samples in the trace')
    ended(run, what, 'ok', 'script ended', since_fault=False)


def case_row_invalid():
    """A damaged stream inside a trace before row(): the row says invalid in the table and in results.csv."""
    ser = FaultSerial('row_invalid')
    run = go(SWEEP, ser=ser, during=lambda run: (wait_log(run, 'UP'), ser.corrupt(2)))
    rows = [e['invalid'] for e in run.ev if e['type'] == 'row']
    check(rows == [True], f'damaged stream before row(): the table row is marked invalid {rows}')
    with open(os.path.join(run.s.folder, 'results.csv'), encoding='utf-8') as f:
        lines = f.read().split()
    check(lines[1:] == ['1,invalid'], f'damaged stream before row(): results.csv says invalid {lines}')
    check(run.r['status'] == 'ok', 'damaged stream before row(): the run itself goes on')
    ended(run, 'damaged stream before row()', 'ok', 'script ended', since_fault=False)


def case_corrupt():
    link_case('corrupt', lambda ser: ser.corrupt(2), 'corrupt bytes')


def case_lost_bytes():
    link_case('lost_bytes', lambda ser: ser.lose_bytes(2), 'lost bytes')


def case_lost_frames():
    link_case('lost_frames', lambda ser: ser.lose_frames(30), 'lost frames')


def case_trial_damaged():
    for case, inject, what in (('trial_corrupt', lambda ser: ser.corrupt(1), 'corrupt bytes'),
                               ('trial_lost', lambda ser: ser.lose_frames(5), 'lost frames')):
        ser = FaultSerial(case)
        inject(ser)
        run = go('def main():\n    setup()\n    stream_start()\n    write("motor.run", 1)\n', ser=ser, trial=2000)
        r = run.r
        check(r['status'] == 'error' and 'trial stream' in r['reason'] and ('resyncs' in r['reason'] or 'lost' in r['reason']),
              f'trial with {what}: refused, {r["reason"][:110]!r}')
        check([x for x in ser.writes() if x != 0] == [x for x in ser.writes(ALIVE)] and ser.writes(RUN) == [0, 0],
              f'trial with {what}: only the heartbeat and the safe values were written, no write of the script')
        ended(run, f'trial with {what}', 'error', 'trial stream', since_fault=False)


def case_delay():
    # a stall below the 500 ms of the documents is no fault
    ser = FaultSerial('delay_short')
    run = go(SWEEP, ser=ser, during=lambda s: (wait_log(s, 'UP'), ser.delay(0.3)))
    check(run.r['status'] == 'ok' and run.sc.lost == 0 and run.sc.resyncs == 0,
          f'delay of 0.3 s: no abort, nothing lost ({run.r["status"]}, lost {run.sc.lost}, resyncs {run.sc.resyncs})')
    check(run.ser.t_fault is None or ser.max_hold_s >= 0.25, f'delay of 0.3 s: the link did hold the bytes back ({ser.max_hold_s:.2f} s)')
    ended(run, 'delay of 0.3 s', 'ok', 'script ended', since_fault=False)
    # a stall above 500 ms is the stall abort
    ser = FaultSerial('delay_long')
    run = go(SWEEP.replace('700', '8000'), ser=ser, during=lambda s: (wait_log(s, 'UP'), ser.delay(1.2)))
    ended(run, 'delay of 1.2 s', 'aborted', 'no sample for 500 ms', readback=False)
    # the link is still stalled while the safe end reads back: that must be reported, not passed
    check(any('read-back' in e for e in run.r['errors']) or 'safe values read back: 1 of 1 correct' in logs(run),
          f'delay of 1.2 s: the read-back is either correct or reported as an error {run.r["errors"][:2]}')
    time.sleep(1.0)
    check(peek(run.sc, 'motor.run') == [0], 'delay of 1.2 s: the safe values reached the model once the link came back')


def case_silent():
    ser = FaultSerial('silent')
    run = go(SWEEP.replace('700', '8000'), ser=ser, during=lambda s: (wait_log(s, 'UP'), ser.go_silent()))
    ended(run, 'silent target', 'aborted', 'no sample for 500 ms', readback=False)
    check(any('read-back' in e or 'read back' in e for e in run.r['errors']), f'silent target: the failed read-back is reported {run.r["errors"][:2]}')
    settle(run)
    check(peek(run.sc, 'motor.run') == [0], 'silent target: the safe values did reach the model')
    # the two safe writes were sent although nothing came back
    check(ser.writes(RUN)[-2:] == [0, 0], 'silent target: safe values were written to it all the same')


# -- port ---------------------------------------------------------------------------------------------------------

def case_port_gone():
    ser = FaultSerial('port_gone')
    body = SWEEP.replace('wait_ms(700)', 'wait_ms(8000)')
    run = go(body, ser=ser, during=lambda s: (wait_log(s, 'UP'), ser.port_gone(0.0)))
    ended(run, 'port gone in a wait', 'aborted', 'port error', port_alive=False, safe=False)
    check('safe values not read back: the port is gone' in logs(run), 'port gone: the log says that the read-back was skipped')
    check(os.path.exists(os.path.join(run.s.folder, 'trace_001.csv')), 'port gone: the open trace was kept')
    txt = run_txt(run)
    check('port error' in txt and 'result aborted' in txt, 'port gone: run.txt has the reason')
    check(any('safe values' in e for e in run.r['errors']), f'port gone: an error says that the safe values were not delivered {run.r["errors"][:2]}')


def case_port_gone_in_write():
    ser = FaultSerial('port_gone_write')
    hit = []

    def before_send(stage):
        if stage == 'post' and not hit:
            hit.append(1)
            ser.port_gone(0.0)
    run = go(HEAD_RUN_ONLY, ser=ser, hooks={'before_send': before_send})
    check(hit, 'the port went inside the first script write')
    ended(run, 'port gone inside a write', 'aborted', 'port error', port_alive=False, safe=False)


HEAD_RUN_ONLY = '''
def main():
    setup()
    stream_start()
    write("motor.run", 1)
    wait_ms(8000)
'''


def case_port_gone_in_safe_end():
    ser = FaultSerial('gone_at_safe')
    ser.gone_at_safe_write()
    run = go(SWEEP.replace('700', '100'), ser=ser)
    check(run.r is not None and run.r['status'] == 'error', f'port gone at the safe write: status {run.r and run.r["status"]!r}, reason {run.r and run.r["reason"][:80]!r}')
    check(any('safe values' in e for e in run.r['errors']), f'port gone at the safe write: the error says that the safe values failed {run.r["errors"][:2]}')
    check('safe values not read back' in logs(run), 'port gone at the safe write: no read-back, said in the log')
    check(run.t_end - ser.t_fault < END_S + SLACK_S, f'port gone at the safe write: result {run.t_end - ser.t_fault:.1f} s after the fault')
    quiet(run, 'port gone at the safe write', port_alive=False)


def case_port_gone_at_start():
    ser = FaultSerial('port_gone_start')
    t0 = time.monotonic()
    run = go(path=STARTUP, ser=ser, probe=True, hooks={'skip_checks': True}, after_ping=lambda: ser.port_gone(0.0, name='port_gone_start'))
    ended(run, 'port gone before the probe', 'aborted', 'port error', port_alive=False, safe=False, bound=END_S, since_fault=True)
    check(ser.log[:2] == [('P', None, None)] * 1 + ser.log[1:2] and 'W' not in [x[0] for x in ser.log], f'port gone before the probe: nothing was written to the target {[x[0] for x in ser.log]}')
    check(time.monotonic() - t0 < END_S + SLACK_S, 'port gone before the probe: quick end')


def case_silent_start():
    """No answer to the ping: E401 names TROUBLESHOOTING.md, nothing but the ping reaches the target, quick end."""
    ser = FaultSerial('silent_start')
    t0 = time.monotonic()
    run = go(path=STARTUP, ser=ser, probe=True, hooks={'skip_checks': True}, after_ping=lambda: ser.go_silent(name='silent_start'))
    r = run.r
    check(r['status'] == 'error' and r['rule_codes'].get('E401') == 1 and 'TROUBLESHOOTING.md' in r['reason'],
          f'target silent before the probe: E401 naming TROUBLESHOOTING.md: {r["reason"][:90]!r}')
    check('W' not in [x[0] for x in ser.log], f'target silent before the probe: nothing was written {[x[0] for x in ser.log]}')
    check('safe values not written' in logs(run), 'target silent before the probe: the log says no safe values were written, and why')
    check(time.monotonic() - t0 < END_S, f'target silent before the probe: ended after {time.monotonic() - t0:.1f} s')
    quiet(run, 'target silent before the probe', port_alive=False)


def case_port_hang():
    """A write that never returns. Documented: a port call of the safe end takes at most 5 s (PORT_TIMEOUT_S, "a run is
    aborted after 5 s"). The safe end makes up to three such calls (safe write, stop, and the join of the heartbeat),
    so the bound is the rest of the script plus 3 x 5 s."""
    ser = FaultSerial('port_hang')

    def hang(run):
        wait_log(run, 'UP')
        if fault_on('port_hang'):
            ser.hang.set()
            ser._mark()
    try:
        run = go(SWEEP.replace('700', '1500'), ser=ser, during=hang, wait=60)
    finally:
        ser.release.set()
    bound = 1.5 + 3 * 5.0 + SLACK_S
    check(run.r is not None and any('does not return' in e for e in run.r['errors']), f'hung port: the error says the port does not return {run.r and run.r["errors"][:2]}')
    check(run.r['status'] == 'error', f'hung port: status {run.r["status"]!r}')
    check(run.t_end - ser.t_fault < bound, f'hung port: result {run.t_end - ser.t_fault:.1f} s after the hang (bound {bound:.1f} s)')
    quiet(run, 'hung port', port_alive=False)


# -- target -------------------------------------------------------------------------------------------------------

def case_reset_trial():
    ser = FaultSerial('reset_trial')
    ser.when_written(ALIVE, None, ser.reset_target, nth=3)
    run = go('def main():\n    setup()\n    stream_start()\n    write("motor.run", 1)\n    wait_ms(8000)\n', ser=ser, trial=2000)
    check(ser.t_fault is not None, 'the target was reset during the trial stream')
    ended(run, 'reset in the trial', 'aborted', 'no sample for 500 ms')
    check(not ser.writes(KP) and not ser.writes(ADDR['startup.ramp_rate']), 'reset in the trial: the script wrote nothing')


def case_reset_sweep():
    ser = FaultSerial('reset_sweep')
    ser.when_written(RUN, 1, ser.reset_target, nth=2)
    run = go(path=STARTUP, ser=ser, probe=True)
    check(ser.t_fault is not None, 'the target was reset at the second start of the sweep')
    ended(run, 'reset in a sweep', 'aborted', 'no sample for 500 ms')
    check(run.r['tuning_h'] is None, 'reset in a sweep: no tuning.h')
    settle(run)
    check(peek(run.sc, 'motor.run', 'motor.state') == [0, 0], 'reset in a sweep: the model stands (run 0, state 0)')


# -- script level -------------------------------------------------------------------------------------------------

def case_build_id():
    # 1. the target holds another id than the ELF: the probe refuses with E402, only the ping and a read reach it
    ser = FaultSerial('build_id')
    ser.poke(BUILD, 0x1234, name='build_id')
    idx = len(ser.log)
    run = go(path=STARTUP, ser=ser, probe=True, hooks={'skip_checks': True})
    r = run.r
    check(r['status'] == 'error' and r['rule_codes'].get('E402') == 1 and 'tune_build_id is 0x1234' in r['reason'],
          f'wrong id on the target: E402, {r["reason"][:140]!r}')
    after = [x[0] for x in ser.log[idx:]]
    check('W' not in after, f'wrong id: nothing was written to the target after the ping ({after})')
    check('safe values not written' in logs(run), 'wrong id: the log says the safe values were not written')
    quiet(run, 'wrong id')
    # 2. the ELF holds another text than the program of the target
    ser = FaultSerial('build_id_elf')
    other = VarTable.from_symbols(SYMBOLS, SYMBOL_TYPES, strings={'tune_build': 'Jan  1 2000 00:00:00' if fault_on('build_id_elf') else SYMBOLS.strings['tune_build']})
    run = go(path=STARTUP, ser=ser, probe=True, hooks={'skip_checks': True}, table=other)
    r = run.r
    check(r['status'] == 'error' and r['rule_codes'].get('E402') == 1 and 'not the build of the ELF' in r['reason'],
          f'wrong text in the ELF: E402, {r["reason"][:120]!r}')
    check('W' not in [x[0] for x in ser.log], 'wrong text in the ELF: nothing was written')
    quiet(run, 'wrong text in the ELF')
    # 3. without the probe the start refuses (SCRIPTING.md, stream_start: "or when the build is not the ELF (E402)")
    ser = FaultSerial('build_id_start')
    ser.poke(BUILD, 0x4321, name='build_id_start')
    idx = len(ser.log)
    run = go(SWEEP, ser=ser, hooks={'skip_build_id': False})
    r = run.r
    check(r['status'] == 'error' and 'stream_start() refused' in r['reason'] and 'tune_build_id' in r['reason'],
          f'stream_start() with a wrong id: refused, {r["reason"][:120]!r}')
    check('W' not in [x[0] for x in ser.log[idx:]] or ser.writes(RUN) == [], 'stream_start() with a wrong id: no write of the script reached the target')
    quiet(run, 'stream_start with a wrong id')


def case_overwrite():
    ser = FaultSerial('overwrite')
    ser.stick(RAMP, 100, name='overwrite')                  # the firmware keeps startup.ramp_rate at 100
    run = go(path=STARTUP, ser=ser, probe=True)
    r = run.r
    line = line_of(run.path, 'allow(VAR_RAMP_RATE')
    check(r['status'] == 'error' and r['rule_codes'].get('E420') == 1 and r['reason'].startswith(f't.py:{line}: error: startup.ramp_rate'),
          f'overwritten variable: E420 at its allow() line {line}: {r["reason"][:110]!r}')
    check('overwrites it' in r['reason'] and 'offset' in r['reason'], 'overwritten variable: the text names the cause and the cure')
    check(not ser.writes(RUN)[:-2] and ser.writes(RUN) == [0, 0], f'overwritten variable: the motor was never started ({ser.writes(RUN)})')
    ended(run, 'overwritten variable', 'error', f't.py:{line}', since_fault=False)


def case_fault_idle():
    ser = FaultSerial('fault_idle')
    ser.stick(FAULT, 1, name='fault_idle')
    run = go(path=STARTUP, ser=ser, probe=True)
    r = run.r
    line = line_of(run.path, 'expect(not has_fault()')
    check(r['status'] == 'error' and r['rule_codes'].get('E410') == 1 and r['reason'].startswith(f't.py:{line}: error: '),
          f'fault stuck at 1 before the run: E410 at the expect() line {line}: {r["reason"][:100]!r}')
    check('no fault expected' in r['reason'], 'fault stuck at 1 before the run: the text of expect() is shown')
    check(ser.writes(RUN) == [0, 0] and not ser.writes(RAMP)[2:], f'fault stuck at 1 before the run: the motor was not started ({ser.writes(RUN)})')
    ended(run, 'fault stuck at 1 (idle)', 'error', f't.py:{line}', since_fault=False)


def case_fault_midrun():
    ser = FaultSerial('fault_mid')
    ser.when_written(RUN, 1, lambda: ser.stick(FAULT, 1, name='fault_mid'))
    run = go(path=STARTUP, ser=ser, probe=True)
    r = run.r
    txt = logs(run)
    check(r['status'] == 'ok' and 'No ramp rate started reliably' in txt, f'fault stuck at 1 during the run: no start counts, {r["status"]!r}')
    check(r['tuning_h'] is None, 'fault stuck at 1 during the run: no tuning.h')
    rows = [e['values'] for e in run.ev if e['type'] == 'row']
    check(rows and all(v[1] == 0 for v in rows), f'fault stuck at 1 during the run: every row has 0 good starts {rows}')
    check(all(t['failed'] for t in run.s.traces[:len(rows)]), 'fault stuck at 1 during the run: every start trace is marked failed')
    ended(run, 'fault stuck at 1 (run)', 'ok', 'script ended', since_fault=False)


def case_no_stop():
    # the speed stays after the stop command: the script gives up, safe values follow
    ser = FaultSerial('no_stop')
    ser.when_written(RUN, 0, lambda: ser.stick(SPEED, 500, name='no_stop'))
    run = go(path=STARTUP, ser=ser, probe=True)
    r = run.r
    check('Motor does not stop, aborting' in logs(run), 'motor does not stop: the script says so')
    check(r['status'] == 'ok' and r['tuning_h'] is None, f'motor does not stop: the script returned, no tuning.h ({r["status"]!r})')
    check(not [e for e in run.ev if e['type'] == 'row'], 'motor does not stop: no row was written')
    ended(run, 'motor does not stop', 'ok', 'script ended', since_fault=False)
    settle(run)
    check(peek(run.sc, 'motor.run') == [0], 'motor does not stop: motor.run is 0 at the end')


def case_ignores_stop():
    # the firmware keeps motor.run at 1: the safe value cannot be reached, the run must not end ok
    ser = FaultSerial('ignores_stop')
    ser.when_written(RUN, 1, lambda: ser.stick(RUN, 1, name='ignores_stop'))
    run = go(path=STARTUP, ser=ser, probe=True)
    r = run.r
    check('Motor does not stop, aborting' in logs(run), 'firmware ignores the stop: the script notices')
    check(r['status'] == 'error' and any('safe value not reached: motor.run is 1' in e for e in r['errors']),
          f'firmware ignores the stop: safe value not reached is an error: {r["status"]!r} {r["errors"][:2]}')
    check('safe value not reached' in run_txt(run), 'firmware ignores the stop: run.txt says it')
    check(ser.writes(RUN)[-3:] == [0, 0, 0] or len(ser.writes(RUN)) >= 3, f'firmware ignores the stop: the safe value was written twice and once more ({ser.writes(RUN)[-4:]})')
    quiet(run, 'firmware ignores the stop')
    ser.unstick()


def case_trigger_never():
    ser = FaultSerial('trigger_never')
    ser.stick(CREF, 0, name='trigger_never')                # the firmware overwrites the reference: no step, no trigger
    run = go(path=CURRENT, ser=ser, probe=True)
    r = run.r
    check('no trigger, does the firmware use motor.current_ref?' in logs(run), 'trigger never comes: the script says what to check')
    check(r['status'] == 'ok' and r['tuning_h'] is None and not [e for e in run.ev if e['type'] == 'row'],
          f'trigger never comes: the script returned, no row, no tuning.h ({r["status"]!r})')
    # script times: settle 200 ms, step 100 ms, capture timeout 500 ms; the probe and the trial come before
    check(any(e['type'] == 'state' and e['state'] == 'running' for e in run.ev), 'trigger never comes: the run reached the sweep')
    ended(run, 'trigger never comes', 'ok', 'script ended', addr=CREF, since_fault=False)
    ser.unstick()


def case_capture_lost_bytes():
    # one byte lost in the dump of a capture: the transfer stops, capture_wait() times out, no wrong data is used
    ser = FaultSerial('capture_lost')
    ser.only = 'capture'
    ser.when_written(CREF, 500, lambda: ser.lose_bytes(1, name='capture_lost'))
    run = go(path=CURRENT, ser=ser, probe=True)
    r = run.r
    check('no trigger' in logs(run), 'capture dump short by a byte: capture_wait() returned -1, the script ended')
    check(r['status'] == 'ok' and r['tuning_h'] is None, f'capture dump short by a byte: no tuning.h ({r["status"]!r})')
    ended(run, 'capture dump short', 'ok', 'script ended', addr=CREF, since_fault=False)
    # and in the trial capture of capture_start()
    ser = FaultSerial('capture_trial')
    ser.only = 'capture'
    ser.lose_bytes(1, name='capture_trial')
    run = go(path=CURRENT, ser=ser, probe=True, hooks={'skip_probe': True, 'skip_build_id': True})
    r = run.r
    check(r['status'] == 'error' and 'capture_start() refused' in r['reason'],
          f'trial capture short by a byte: capture_start() refuses {r["reason"][:110]!r}')
    ended(run, 'trial capture short', 'error', 'capture_start() refused', addr=CREF, since_fault=False)


def case_heartbeat_lost():
    """SCRIPTING.md section 6: the target stops after TUNE_HEARTBEAT_MS = 1000 ms without a change of tune_alive, with
    no help of the PC. The heartbeat is held back with the hook hold_heartbeat while the motor runs."""
    hold = threading.Event()
    ser = FaultSerial('heartbeat')
    src = '''
def main():
    channels("motor.state", "motor.fault", "motor.run", "tune_alive")
    allow("motor.run", 0, 1)
    safe_value("motor.run", 0)
    stream_start()
    trace_begin()
    write("motor.run", 1)
    wait_ms(600)
    printf("UP\\n")
    wait_ms(2200)
    trace_end("hb")
'''

    def during(run):
        wait_log(run, 'UP')
        if fault_on('heartbeat'):
            hold.set()
            ser._mark()
    run = go(src, ser=ser, hooks={'hold_heartbeat': hold}, during=during)
    tr = run.s.traces[0]
    t = tr['time_ms']
    state, fault, runv, alive = (np.asarray(tr['values'][n]) for n in ('motor.state', 'motor.fault', 'motor.run', 'tune_alive'))
    ch = np.flatnonzero(np.diff(alive) != 0)
    first = np.flatnonzero(fault == 5)
    gap = int(t[first[0]] - t[ch[-1] + 1]) if len(first) and len(ch) else None
    print(f'       last change of tune_alive at sample {int(t[ch[-1] + 1]) if len(ch) else None}, first fault 5 at sample '
          f'{int(t[first[0]]) if len(first) else None}, gap {gap} ms')
    check(gap is not None and 1000 <= gap <= 1003, f'heartbeat lost: fault 5 {gap} ms after the last change (documented 1000 ms, sample granularity 1 ms)')
    check(len(first) and runv[first[0]:first[0] + 200].max() == 0 and state[first[0]:first[0] + 200].max() == 0,
          'heartbeat lost: the target stopped the motor itself (run 0, state 0) without the PC')
    check(len(first) and runv[:first[0]].max() == 1, 'heartbeat lost: the motor was running before')
    hold.clear()
    ended(run, 'heartbeat lost', 'ok', 'script ended', since_fault=False)


# -- script errors ------------------------------------------------------------------------------------------------

def mutate(path, lineno, case='script_error'):
    """The script with `x = 1 / 0` inserted before line lineno, at its indentation."""
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')
    ind = re.match(r'\s*', lines[lineno - 1]).group(0)
    if fault_on(case):
        lines.insert(lineno - 1, ind + 'x = 1 / 0')
    return '\n'.join(lines)


def executable_lines(path):
    """Lines of the functions (indented, not a comment, not a continuation or else) in file order."""
    with open(path, encoding='utf-8') as f:
        src = f.read().split('\n')
    start = next(i for i, x in enumerate(src) if x.startswith('def ') and ('2. how your' in ' '.join(src[max(0, i - 3):i]) or True))
    out = []
    for i, x in enumerate(src):
        st = x.strip()
        if i < start or not st or st.startswith('#') or not x.startswith('    '):
            continue
        if st.startswith(('else', 'elif', 'except')) or src[i - 1].rstrip().endswith((',', '(')):
            continue
        out.append(i + 1)
    return out


def case_script_error_lines():
    """An error at any line of both example scripts: the message names that line of the script, never tune_api.py, and
    the run ends with the safe values. Stage 3 (the dry run) finds what it can reach; for the rest the run does."""
    detected = {}
    runtime = {}
    for path in (STARTUP, CURRENT):
        name = os.path.basename(path)
        for n in executable_lines(path):
            folder = tempfile.mkdtemp(dir=TMP)
            dest = os.path.join(folder, 't.py')
            with open(dest, 'w', encoding='utf-8') as f:
                f.write(mutate(path, n))
            problems = [p for p in tune_check.check_script(dest, TABLE) if p.severity == 'error']
            if problems:
                detected[(name, n)] = (problems, dest)
            else:
                runtime[(name, n)] = dest
    if not fault_on('script_error'):
        runtime = {}
    wrong = [(k, [(p.code, p.line) for p in v[0]]) for k, v in detected.items()
             if not (len(v[0]) == 1 and v[0][0].code == 'E390' and v[0][0].line == k[1] and 'ZeroDivisionError' in v[0][0].text)]
    check(not wrong and detected, f'stage 3 finds {len(detected)} inserted errors, each as E390 at exactly its line, with the type of the error: {wrong[:3]}')
    # the others: the run must report them at their line, or finish normally if the line is never executed
    bad, hits, quiet_ok = [], 0, 0
    for (name, n), dest in runtime.items():
        ser = FaultSerial('script_error')
        tune_api.TRIAL_MS = 300
        folder = os.path.dirname(dest)
        sc = Scope(ser)
        sc.ping()
        ev = []
        s = tune_api.run(dest, sc, TABLE, on_event=ev.append, out_dir=os.path.join(folder, 'runs'), elf_name='x.elf',
                         hooks={'skip_probe': True, 'skip_build_id': True})
        r = s.wait(60)
        if r is None:
            bad.append((name, n, 'no end'))
            tune_api.abort('test timeout')
            s.wait(10)
            continue
        if r['status'] == 'error':
            hits += 1
            if not r['reason'].startswith(f't.py:{n}: error: ZeroDivisionError') or 'tune_api' in r['reason']:
                bad.append((name, n, r['reason'][:80]))
            safe_addr = RUN if name == 'tune_startup.py' else CREF
            if s.started and ser.writes(safe_addr)[-2:] != [0, 0]:
                bad.append((name, n, 'no safe values', ser.writes(safe_addr)[-3:]))
        elif r['status'] == 'ok':
            quiet_ok += 1
        else:
            bad.append((name, n, r['status'], r['reason'][:80]))
        th = [t.name for t in threading.enumerate() if t.name in TUNE_THREADS and t.is_alive()]
        if th and not bad:
            time.sleep(1.5)
        if [t.name for t in threading.enumerate() if t.name in TUNE_THREADS and t.is_alive()]:
            bad.append((name, n, 'thread left'))
    print(f'       {len(runtime)} lines not reached by the dry run, run for real: {hits} reported at their line, {quiet_ok} never executed')
    check(not bad and not (runtime and fault_on('script_error') and hits == 0),
          f'a runtime error at any other line: reported as t.py:LINE: error: ZeroDivisionError, safe values written: {bad[:3]}')
    check(hits >= 10, f'at least 10 of the lines were really reached ({hits})')


def case_script_error_types():
    on = fault_on('script_types')
    # a syntax error and an undefined name: refused before the target is touched
    for text, code, want in (('def main():\n    setup(\n', 'E101', None), ('def main():\n    setup()\n    stream_strat()\n', 'E102', 'stream_strat')):
        folder = tempfile.mkdtemp(dir=TMP)
        dest = os.path.join(folder, 't.py')
        with open(dest, 'w') as f:
            f.write(HEAD + (text if on else 'def main():\n    setup()\n') + '\ndef check_idle():\n    expect(True, "x")\n')
        ser = FaultSerial()
        sc = Scope(ser)
        sc.ping()
        try:
            s = tune_api.run(dest, sc, TABLE, out_dir=os.path.join(folder, 'runs'))
            s.wait(20)
            check(False, f'{code}: the run was refused')
        except tune_api.RunRefused as e:
            check(f'[{code}]' in str(e) and 't.py:' in str(e) and (want is None or want in str(e)), f'{code}: refused with file:line: {str(e).splitlines()[1][:80]!r}')
        check(ser.log == [('P', None, None)], f'{code}: only the ping reached the target')
    # an exception of any kind at run time: KeyError, SystemExit, ValueError
    for what, stmt in (('KeyError', 'd = {}\n    x = d["a"]'), ('SystemExit', 'raise SystemExit(3)'), ('ValueError', 'int("x")')):
        ser = FaultSerial('script_types')
        run = go(f'def main():\n    setup()\n    stream_start()\n    write("motor.run", 1)\n    wait_ms(50)\n    {stmt if on else "pass"}\n', ser=ser)
        line = line_of(run.path, stmt.split('\n')[-1].strip() if on else 'pass')
        ended(run, what, 'error', f't.py:{line}: error: ', since_fault=False)
    # a loop that never calls wait_ms(): the abort still stops it
    ser = FaultSerial('script_types')
    stop = []
    loop = 'while True:\n        pass' if on else 'return'
    run = go('def main():\n    setup()\n    stream_start()\n    write("motor.run", 1)\n    printf("UP\\n")\n    ' + loop + '\n', ser=ser,
             during=lambda s: (wait_log(s, 'UP'), time.sleep(0.2), stop.append(time.monotonic()), tune_api.abort('test')))
    check(run.t_end - stop[0] < END_S + SLACK_S, f'endless loop: result {run.t_end - stop[0]:.1f} s after the abort')
    ended(run, 'endless loop', 'aborted', 'aborted: test', since_fault=False)
    check(not any(t.name == 'tune-script' and t.is_alive() for t in threading.enumerate()), 'endless loop: the script thread was stopped')


def case_script_error_hook():
    """A module level error, and an error in a function that was called after the first trace."""
    folder = tempfile.mkdtemp(dir=TMP)
    dest = os.path.join(folder, 't.py')
    with open(STARTUP, encoding='utf-8') as f:
        text = f.read()
    n = line_of(STARTUP, 'VAR_RUN            =')
    lines = text.split('\n')
    if fault_on('script_module'):
        lines[n - 1] = lines[n - 1] + ' + 1 / 0' if False else 'VAR_RUN = 1 / 0'
    with open(dest, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    ser = FaultSerial()
    sc = Scope(ser)
    sc.ping()
    try:
        tune_api.run(dest, sc, TABLE, out_dir=os.path.join(folder, 'runs'))
        check(False, 'module level error: refused')
    except tune_api.RunRefused as e:
        check(f't.py:{n}: error' in str(e), f'module level error: refused at line {n}: {str(e).splitlines()[1][:90]!r}')
    check(ser.log == [('P', None, None)], 'module level error: only the ping reached the target')


# -- tools/run_script.py -------------------------------------------------------------------------------------------

def run_tool(args, env=None, timeout=90):
    work = tempfile.mkdtemp(dir=TMP)
    e = dict(os.environ)
    e.update(env or {})
    t0 = time.time()
    p = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'run_script.py')] + args, cwd=work, capture_output=True, text=True,
                       timeout=timeout, env=e)
    return p, work, time.time() - t0


def case_tool_port_lost():
    lost = '5' if fault_on('tool_port_lost') else '0'
    try:
        p, work, secs = run_tool(['--port', 'SIM', '--out', 'runs', STARTUP], env={'MS_SIM_LOST_S': lost})
    except subprocess.TimeoutExpired:
        check(False, 'run_script.py with a lost port: it hung (90 s)')
        return
    out = p.stdout
    check(p.returncode == 1, f'run_script.py, port lost 5 s after the first stream: exit code {p.returncode} after {secs:.0f} s')
    check('Status aborted: port error' in out, f'run_script.py, port lost: the last lines say so {[x for x in out.splitlines() if x.startswith("Status")]}')
    check('safe values not read back: the port is gone' in out, 'run_script.py, port lost: the progress says the read-back was skipped')
    runs = os.path.join(work, 'runs')
    folder = os.path.join(runs, os.listdir(runs)[0]) if os.path.isdir(runs) and os.listdir(runs) else None
    check(folder and 'result aborted' in open(os.path.join(folder, 'run.txt'), encoding='utf-8').read(), 'run_script.py, port lost: run.txt has the result')
    with open(os.path.join(work, 'run_report.txt'), encoding='utf-8') as f:
        rep = f.read()
    check('status aborted' in rep and 'port SIM' in rep, 'run_script.py, port lost: run_report.txt says aborted')
    check(secs < 60, f'run_script.py, port lost: ended after {secs:.0f} s')


def case_tool_script_error():
    folder = tempfile.mkdtemp(dir=TMP)
    dest = os.path.join(folder, 't.py')
    with open(dest, 'w', encoding='utf-8') as f:
        f.write(mutate(STARTUP, line_of(STARTUP, 'write(VAR_ALIGN_CURRENT, ALIGN_CURRENT)'), 'tool_script_error'))
    n = line_of(dest, 'x = 1 / 0') if fault_on('tool_script_error') else 0
    p, work, secs = run_tool(['--port', 'SIM', '--out', 'runs', dest])
    check(p.returncode == 1 and f't.py:{n}: error' in p.stdout and 'ZeroDivisionError' in p.stdout and not os.path.isdir(os.path.join(work, 'runs')),
          f'run_script.py, error at line {n}: exit code {p.returncode}, message with file:line, nothing run ({secs:.0f} s)')
    # a runtime error the dry run cannot see: the run goes to the end with the safe values
    with open(STARTUP, encoding='utf-8') as f:
        text = f.read()
    text = text.replace('            return time_ms\n', '            return time_ms // (time_ms - time_ms)\n', 1) if fault_on('tool_script_error') else text
    dest2 = os.path.join(tempfile.mkdtemp(dir=TMP), 't.py')
    with open(dest2, 'w', encoding='utf-8') as f:
        f.write(text)
    n2 = line_of(dest2, 'time_ms // (time_ms - time_ms)') if fault_on('tool_script_error') else 0
    p, work, secs = run_tool(['--port', 'SIM', '--out', 'runs', dest2])
    check(p.returncode == 1 and f't.py:{n2}: error: ZeroDivisionError' in p.stdout and 'safe values read back: 1 of 1 correct' in p.stdout,
          f'run_script.py, runtime error at line {n2}: exit code {p.returncode}, status line, safe values read back ({secs:.0f} s)')


CASES = {'row_invalid': case_row_invalid, 'corrupt': case_corrupt, 'lost_bytes': case_lost_bytes, 'lost_frames': case_lost_frames,
         'trial_damaged': case_trial_damaged, 'delay': case_delay, 'silent': case_silent,
         'port_gone': case_port_gone, 'port_gone_write': case_port_gone_in_write, 'port_gone_safe': case_port_gone_in_safe_end,
         'port_gone_start': case_port_gone_at_start, 'silent_start': case_silent_start, 'port_hang': case_port_hang,
         'reset_trial': case_reset_trial, 'reset_sweep': case_reset_sweep,
         'build_id': case_build_id, 'overwrite': case_overwrite, 'fault_idle': case_fault_idle, 'fault_mid': case_fault_midrun,
         'no_stop': case_no_stop, 'ignores_stop': case_ignores_stop, 'trigger_never': case_trigger_never,
         'capture_lost': case_capture_lost_bytes, 'heartbeat': case_heartbeat_lost,
         'script_error': case_script_error_lines, 'script_types': case_script_error_types, 'script_module': case_script_error_hook,
         'tool_port_lost': case_tool_port_lost, 'tool_script_error': case_tool_script_error}

# the names that MS_FAULT_OFF needs to switch off the fault of a case (--break)
BREAK = {'row_invalid': 'row_invalid', 'corrupt': 'corrupt', 'lost_bytes': 'lost_bytes', 'lost_frames': 'lost_frames',
         'trial_damaged': 'trial_corrupt,trial_lost', 'delay': 'delay_short,delay_long', 'silent': 'silent',
         'port_gone': 'port_gone', 'port_gone_write': 'port_gone_write', 'port_gone_safe': 'gone_at_safe',
         'port_gone_start': 'port_gone_start', 'silent_start': 'silent_start', 'port_hang': 'port_hang',
         'reset_trial': 'reset_trial', 'reset_sweep': 'reset_sweep', 'build_id': 'build_id,build_id_elf,build_id_start',
         'overwrite': 'overwrite', 'fault_idle': 'fault_idle', 'fault_mid': 'fault_mid', 'no_stop': 'no_stop',
         'ignores_stop': 'ignores_stop', 'trigger_never': 'trigger_never', 'capture_lost': 'capture_lost,capture_trial',
         'heartbeat': 'heartbeat', 'script_error': 'script_error', 'script_types': 'script_types',
         'script_module': 'script_module', 'tool_port_lost': 'tool_port_lost', 'tool_script_error': 'tool_script_error'}

if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if a != '--break']
    brk = '--break' in sys.argv
    if brk:
        print('--break: the fault of each case is switched off, every case below must report FAIL')
    for name, fn in CASES.items():
        if not args or name in args:
            print(f'--- {name}')
            if brk:
                os.environ['MS_FAULT_OFF'] = BREAK[name]
            t0 = time.time()
            before = getattr(check, 'failed', False)
            check.failed = False
            try:
                fn()
            except Exception as e:      # noqa: BLE001 - with --break an exception is a way to fail too
                check(False, f'{name} raised {type(e).__name__}: {e}')
            case_failed = check.failed
            check.failed = before or case_failed
            if brk:
                print(f'    BREAK {name}: {"FAILS as it must" if case_failed else "DID NOT FAIL: the case cannot detect its fault"}')
                if not case_failed:
                    check.not_failed = getattr(check, 'not_failed', []) + [name]
            print(f'    ({time.time() - t0:.1f} s)')
    shutil.rmtree(TMP, ignore_errors=True)
    print(f'runs left active: {tune_api._active}; known findings: {sorted(set(XFAILS))}')
    if brk:
        print(f'cases that did not fail with their fault off: {getattr(check, "not_failed", [])}')
        sys.exit(1 if getattr(check, 'not_failed', []) else 0)
    sys.exit(1 if getattr(check, 'failed', False) else 0)
