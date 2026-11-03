"""sim_start.py - card 1.2: the virtual motor on the port SIM (PC model).

    gcc -O2 -shared -Itests/mock -Ifirmware -o tests/sim.dll tests/mock/sim.c firmware/scope.c
    python docs/evidence/m1/sim_start.py

Uses SimSerial and the Scope class, no browser. Checks:
  1. a good start (ramp 100) and a failing start (ramp 400), streamed at 1 ms, state change times
     against docs/evidence/m1/model_check.txt (within 2 ms, relative to the first state change);
  2. the model runs while no sample timer runs (current_ref written, stream started afterwards);
  3. captures at 10 us and 100 us period of a current step, rise time against model_check.txt;
  4. the names the GUI offers for port SIM.
Virtual time follows the wall clock (SimSerial), a run takes about 10 s.
"""
import os
import re
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
sys.path[:0] = [os.path.join(REPO, 'tests'), os.path.join(REPO, 'gui')]
import scope_gui as g                       # noqa: E402
from scope_gui import Scope                 # noqa: E402
from sim_serial import SimSerial, SYMBOLS   # noqa: E402

FAILED = []


def check(cond, text):
    print(('OK   ' if cond else 'FAIL ') + text)
    if not cond:
        FAILED.append(text)


def addr(name):
    return SYMBOLS[name][0]


def wr(sc, name, value, typ='u16'):
    err = sc.write(addr(name), typ, value)
    assert err is None, err


def model_rows():
    """t_align, t_ramp, t_closed, fault, fault_ms per ramp rate from model_check.txt."""
    rows, on = {}, False
    for line in open(os.path.join(HERE, 'model_check.txt')):
        if line.startswith('=='):
            on = line.startswith('== tune_startup.py')
        m = re.match(r'\s*(\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(\d+)@(-?\d+)', line)
        if on and m:
            rows[int(m.group(1))] = tuple(int(x) for x in m.groups()[1:])
    return rows


def model_current_rise(kp):
    """rise_us column of the current step table in model_check.txt."""
    on = False
    for line in open(os.path.join(HERE, 'model_check.txt')):
        if line.startswith('=='):
            on = line.startswith('== tune_current.py')
        m = re.match(r'\s*(\d+)\s+(\d+)\s+(-?\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$', line)
        if on and m and int(m.group(1)) == kp:
            return int(m.group(2))
    raise SystemExit('kp not found in model_check.txt')


def first(a, pred):
    i = np.flatnonzero(pred(a))
    return int(i[0]) if len(i) else None


def start_run(sc, rate):
    """Streams speed, iq, state, fault at 1 ms and starts the motor. Returns the sample columns."""
    chans = [(addr('motor.speed'), 's16'), (addr('motor.iq'), 's16'),
             (addr('motor.state'), 'u16'), (addr('motor.fault'), 'u16')]
    wr(sc, 'motor.run', 0)
    time.sleep(0.3)                         # fault cleared, standstill
    wr(sc, 'startup.ramp_rate', rate)
    check(sc.start(chans, 0, 1000) is None, f'ramp {rate}: stream config ACK')
    time.sleep(0.3)
    n_write = len(sc.snapshot()[0])         # samples received when run is written (about, thread latency)
    wr(sc, 'motor.run', 1)
    time.sleep(0.6)
    t, cols = sc.snapshot()
    lost = sc.lost
    sc.stop()
    wr(sc, 'motor.run', 0)
    return t, cols, lost, n_write


def compare_run(sc, rate, model):
    t, (speed, iq, state, fault), lost, n_write = start_run(sc, rate)
    check(lost == 0 and np.all(np.diff(t) == 1), f'ramp {rate}: {len(t)} samples, {lost} lost')
    s1 = first(state, lambda a: a == 1)
    s2 = first(state, lambda a: a == 2)
    s3 = first(state, lambda a: a == 3)
    f = first(fault, lambda a: a != 0)
    m_align, m_ramp, m_closed, m_fault, m_fault_ms = model
    got = {'ramp': None if s2 is None else s2 - s1,
           'closed': None if s3 is None else s3 - s1,
           'fault': None if f is None else f - s1}
    want = {'ramp': m_ramp - m_align, 'closed': None if m_closed < 0 else m_closed - m_align,
            'fault': None if m_fault == 0 else m_fault_ms - m_align}
    print(f'     ramp {rate}: state 0->1 at sample {s1} (run written at about sample {n_write}), '
          f'max iq {int(iq.max())}, last speed {int(speed[-1])}, last fault {int(fault[-1])}')
    for k in ('ramp', 'closed', 'fault'):
        ok = (got[k] is None and want[k] is None) or (
            got[k] is not None and want[k] is not None and abs(got[k] - want[k]) <= 2)
        check(ok, f'ramp {rate}: {k} change {got[k]} ms after the first state change, model_check.txt {want[k]}')
    if m_fault:
        check(int(fault[-1]) == m_fault and int(state[-1]) == 0, f'ramp {rate}: fault {int(fault[-1])} latched, state 0')
    else:
        check(int(fault[-1]) == 0 and int(state[-1]) == 3, f'ramp {rate}: closed loop, no fault')


def run_standstill_current(sc):
    """current_ref written with no stream and no capture: the model must have settled at the next stream."""
    wr(sc, 'motor.current_ref', 3000, 's16')
    time.sleep(0.5)                         # no sample timer runs
    check(sc.ser.lib.sim_period_us() == 0.0, 'no sample timer runs while current_ref settles')
    chans = [(addr('motor.current_ref'), 's16'), (addr('motor.current'), 's16')]
    check(sc.start(chans, 0, 1000) is None, 'stream config ACK after the idle time')
    time.sleep(0.2)
    t, (ref, cur) = sc.snapshot()
    sc.stop()
    print(f'     first samples: current_ref {int(ref[0])}, current {int(cur[0])} .. {int(cur[4])}; last {int(cur[-1])}')
    check(int(ref[0]) == 3000 and abs(int(cur[0]) - 3000) <= 30,
          f'first sample after the idle time: current {int(cur[0])} for current_ref 3000 (settled, 1 %)')
    wr(sc, 'motor.current_ref', 0, 's16')
    time.sleep(0.2)


def rise_us(cur, period_us):
    """t10 to t90 of the step in us, like model_check.c, plus the plateau lengths in samples."""
    fin = 500                                           # the step; 10.24 ms at 10 us is not settled yet
    i10 = first(cur, lambda a: a >= fin / 10)
    i90 = first(cur, lambda a: a >= fin * 9 / 10)
    ch = np.flatnonzero(np.diff(cur) != 0) + 1          # indices where the value changes
    runs = np.diff(ch[(ch >= i10 - 1) & (ch <= i90 + 12)])
    return fin, (i90 - i10) * period_us, runs


def capture_step(sc, period_us, model_rise):
    chans = [(addr('motor.current_ref'), 's16'), (addr('motor.current'), 's16')]
    wr(sc, 'motor.current_kp', 400, 's16')
    wr(sc, 'motor.current_ki', 20, 's16')
    wr(sc, 'motor.current_ref', 0, 's16')
    time.sleep(0.5)
    # rising edge of current_ref through 250, 10 % pretrigger
    check(sc.start(chans, 1, period_us, 0, 1, 250, 10, auto=False) is None, f'capture {period_us} us config ACK')
    time.sleep(0.05)
    wr(sc, 'motor.current_ref', 500, 's16')
    end = time.time() + 5
    while sc.captures < 1 and time.time() < end:
        time.sleep(0.05)
    t, (ref, cur) = sc.snapshot()
    n_cap = sc.captures
    sc.stop()
    wr(sc, 'motor.current_ref', 0, 's16')
    check(n_cap == 1 and len(t) == 1024, f'capture {period_us} us: {n_cap} capture, {len(t)} samples')
    i0 = int(np.flatnonzero(t == 0)[0])
    _, rise, runs = rise_us(cur, period_us)
    print(f'     capture {period_us} us: trigger index {i0}, last {int(cur[-1])}, span {len(t) * period_us} us, '
          f'rise t10-t90 {rise} us, plateau lengths {runs.tolist()[:8]}')
    last = int(cur[-1])
    check(last >= 450, f'capture {period_us} us: last sample {last} for a step of 500')
    check(abs(rise - model_rise) <= 100, f'capture {period_us} us: rise {rise} us, model_check.txt {model_rise} us (+-100 us)')
    med = int(np.median(runs)) if len(runs) else None
    want = max(1, 100 // period_us)
    check(len(runs) >= 3 and med == want,
          f'capture {period_us} us: the value changes every {med} samples, model tick 100 us = {want} samples')


def gui_names():
    """The dropdown of the GUI (channel and write rows in gui/scope_gui.py) is built from sim_symbols()."""
    syms = g.sim_symbols()
    options = {n: f'{n}  (0x{a:04X}, {s} B)' for n, (a, s) in syms.items()}      # same expression as the GUI
    want = ['motor.run', 'motor.state', 'motor.speed', 'motor.speed_ref', 'motor.iq', 'motor.fault', 'motor.kp',
            'motor.ki', 'motor.ref_offset', 'motor.current_ref', 'motor.current', 'motor.current_kp',
            'motor.current_ki', 'startup.ramp_rate', 'startup.align_current', 'startup.handover_speed',
            'tune_alive', 'sine', 'ramp', 'counter', 'toggle', 'sine_gain', 'ramp_step']
    missing = [n for n in want if n not in options]
    check(not missing, f'GUI list for port SIM has {len(options)} names, missing: {missing}')
    print('     ' + options['motor.current_ref'] + ' | ' + options['startup.ramp_rate'])
    spans = sorted((a, a + s) for a, s in syms.values())
    check(all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1)), 'no two names overlap')


def main():
    sc = Scope(SimSerial(os.environ.get('SIM_DLL', os.path.join(REPO, 'tests', 'sim.dll'))))
    check(sc.ping(), 'ping')
    rows = model_rows()
    print('model_check.txt (t_align t_ramp t_closed fault@ms):', {k: rows[k] for k in (100, 400)})
    compare_run(sc, 100, rows[100])
    compare_run(sc, 400, rows[400])
    run_standstill_current(sc)
    rise = model_current_rise(400)
    print('model_check.txt: current loop kp 400, rise', rise, 'us')
    capture_step(sc, 10, rise)
    capture_step(sc, 100, rise)
    gui_names()
    print('RESULT:', f'{len(FAILED)} FAIL' if FAILED else 'all OK')
    sys.exit(1 if FAILED else 0)


main()
