"""hw_check.py - checks mini-scope on the target and writes a report.

    .venv\\Scripts\\python tools\\hw_check.py --port COM5
    .venv\\Scripts\\python tools\\hw_check.py --port COM5 --loopback     adapter only, TX and RX bridged

Program test.X first (production build) and disconnect the PICkit. The demo
variables of example_main.c (counter, ramp, ramp_step) are taken from the ELF; without
them only the checks that need no variables run. --port SIM runs against the
PC model (tests/sim.dll) to try out this script.

The report goes to the console and to hw_report.txt; each FAIL/WARN names the
section of TROUBLESHOOTING.md to read next.
"""
import argparse
import datetime
import os
import platform
import sys
import time

import numpy as np
import serial

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, os.path.join(ROOT, 'gui'))
import scope_gui as g      # noqa: E402

ELF = os.path.join(ROOT, r'test.X\dist\default\production\test.X.production.elf')
BAUDS = (115200, 57600, 230400, 14400, 28800, 38400, 19200, 76800, 9600)
lines, results = [], []


def out(text=''):
    print(text, flush=True)
    lines.append(text)


def result(status, name, detail, hint=''):
    results.append(status)
    out(f'{status:4}  {name}: {detail}')
    if hint and status != 'OK':
        out(f'      -> {hint}')


def loopback(port, baud):
    data = bytes(range(256)) * 4
    try:
        s = serial.Serial(port, baud, timeout=0.5)
    except serial.SerialException as e:
        result('FAIL', 'port', str(e), 'wrong COM port, or another program has it open')
        return
    with s:
        s.reset_input_buffer()
        s.write(data)
        back = s.read(len(data))
    if back == data:
        result('OK', 'adapter loopback', f'{len(data)} bytes at {baud} baud came back unchanged')
    else:
        result('FAIL', 'adapter loopback', f'{len(back)} of {len(data)} bytes back, '
               f'{sum(a != b for a, b in zip(back, data))} wrong',
               'TX and RX of the adapter bridged? Right COM port? Port busy (GUI, terminal)?')


def connect(port, baud):
    """Scope at the given baud, or after a baud scan (wrong clock). Returns (scope, baud) or (None, None)."""
    garbage = 0
    for b in [baud] + [x for x in BAUDS if x != baud]:
        try:
            ser = g.open_port(port, b)
        except serial.SerialException as e:
            result('FAIL', 'port', str(e), 'wrong COM port, or the GUI / a terminal still has it open: '
                   'TROUBLESHOOTING.md 3, 4.1')
            return None, None
        sc = g.Scope(ser)
        for _ in range(3):
            if sc.ping():
                return sc, b
            garbage += ser.in_waiting
        g.close_port(ser)
        if port in ('SIM', 'MDB'):
            break
    if garbage:
        result('FAIL', 'ping', f'no valid answer at any baud rate, but {garbage} bytes arrived',
               'noise or a wrong clock far off: TROUBLESHOOTING.md 4.3, 4.4')
    else:
        result('FAIL', 'ping', 'no byte at all from the target at any baud rate',
               'wiring, PICkit still connected, debug build, firmware not running: '
               'TROUBLESHOOTING.md 1, 4.1, 4.2 (run with --loopback to check the adapter)')
    return None, None


def stream(sc, chans, period_us, secs):
    """Streams for secs; returns t, cols and (wall time, sample index) pairs for the clock check."""
    err = sc.start(chans, 0, period_us)
    if err:
        return None, None, err
    marks, end = [], time.perf_counter() + secs
    while time.perf_counter() < end:
        time.sleep(0.25)
        t, _ = sc.snapshot()
        if len(t):
            marks.append((time.perf_counter(), int(t[-1])))
    t, cols = sc.snapshot()
    sc.stop()
    return t, cols, marks


def check_clock(sc, chans, counter_col, secs):
    t, cols, marks = stream(sc, chans, 1000, secs)
    if t is None:
        result('FAIL', 'stream', marks, 'TROUBLESHOOTING.md 5')
        return
    if len(t) < 100:
        result('FAIL', 'stream', f'only {len(t)} samples in {secs} s', 'TROUBLESHOOTING.md 4.5')
        return
    result('OK', 'stream', f'{len(t)} samples in {secs} s, {sc.lost} lost')
    m = np.array([x for x in marks if x[0] > marks[0][0] + 1.0])     # skip the start-up second
    rate = np.polyfit(m[:, 0], m[:, 1], 1)[0] if len(m) > 4 else 0
    dev = (rate / 1000 - 1) * 100
    detail = f'{rate:.1f} samples/s at 1000 us, sample clock {dev:+.2f} % against the PC'
    hint = ('FP differs from SCOPE_FP_HZ (FRC tolerance, PLL setup); the baud rate is off by the same amount: '
            'TROUBLESHOOTING.md 4.3')
    result('OK' if abs(dev) < 1.5 else 'WARN' if abs(dev) < 3 else 'FAIL', 'clock', detail, hint)
    if counter_col is None:
        return
    c = cols[counter_col].astype(np.int64)
    d = np.diff(c) / np.diff(t)                 # counter increments per sample, lost frames included
    med = float(np.median(d))
    bad = int(np.sum((d <= 0) | (np.abs(d - med) > 0.5 * med)))
    result('OK' if bad == 0 else 'WARN', 'consistency',
           f'counter +{med:.0f} per sample, {bad} outliers in {len(d)} steps',
           'torn 32-bit reads or a stalled main loop: TROUBLESHOOTING.md 5 ("Single values are wrong")')


def check_rates(sc, chans, baud):
    rec = sum(np.dtype(g.TYPES[ty]).itemsize for _, ty in chans)
    _, _, min_us = g.stream_load(rec, 1000, baud)
    for per in (2 * min_us, min_us):
        t, _, marks = stream(sc, chans, per, 5)
        if t is None:
            result('FAIL', f'stream {per} us', marks)
            continue
        status = 'OK' if sc.lost == 0 and len(t) > 0 else 'FAIL'
        result(status, f'stream {per} us', f'{len(t)} samples, {sc.lost} lost ({rec + 2} bytes/frame)',
               'the UART does not carry what it should: TX interrupt, baud rate, '
               'or other interrupts blocking mini-scope (TROUBLESHOOTING.md 4.5)')


def check_capture(sc, chans, ramp_col):
    for name, kw in (('capture', dict(tch=0, ttype=0, level=0, pre_pct=0)),
                     ('capture trigger', dict(tch=ramp_col, ttype=1, level=30000, pre_pct=20))):
        if kw['tch'] is None:
            continue
        sc.stop()
        err = sc.start(chans, 1, 10, auto=False, **kw)
        end = time.time() + 10
        while not err and sc.captures == 0 and time.time() < end:
            time.sleep(0.1)
        t, cols = sc.snapshot()
        sc.stop()
        if err or sc.captures == 0:
            result('FAIL', name, err or 'no capture within 10 s', 'TROUBLESHOOTING.md 6')
            continue
        depth = sc.capbuf // sum(np.dtype(g.TYPES[ty]).itemsize for _, ty in chans)
        if kw['ttype'] == 0:
            ok = len(t) == depth and t[0] == 0
            result('OK' if ok else 'FAIL', name, f'{len(t)} of {depth} samples at 10 us')
        else:
            r = cols[ramp_col].astype(np.int64)
            i0 = int(np.flatnonzero(t == 0)[0])
            ok = r[i0 - 1] < 30000 <= r[i0]
            result('OK' if ok else 'FAIL', name, f'ramp rising through 30000: {r[i0 - 1]} -> {r[i0]}, '
                   f'pretrigger {i0} samples', 'TROUBLESHOOTING.md 6')


def check_write(sc, ramp, step_addr):
    """Doubles ramp_step while streaming the ramp: the slope must double."""
    def slope(t, r):
        return float(np.median(np.diff(r.astype(np.int64)) % 65536 / np.diff(t))) if len(t) > 10 else 0.0
    if sc.write(step_addr, 'u16', 1) or sc.start([ramp], 0, 1000):
        result('FAIL', 'write', 'could not start')
        return
    time.sleep(2.0)
    t, (r,) = sc.snapshot()
    s1 = slope(t, r)
    sc.write(step_addr, 'u16', 2)
    time.sleep(0.5)
    n = len(sc.snapshot()[0])
    time.sleep(2.0)
    t, (r,) = sc.snapshot()
    s2 = slope(t[n:], r[n:])
    sc.stop()
    sc.write(step_addr, 'u16', 1)
    ok = s1 > 0 and abs(s2 / s1 - 2) < 0.2
    result('OK' if ok else 'FAIL', 'write', f'ramp_step 1 -> 2 while streaming: ramp slope {s1:.0f} -> {s2:.0f} per sample',
           'the W command does not arrive or does not write: firmware older than the GUI?')


def first_counter(sc, ch):
    t, cols, _ = stream(sc, [ch], 1000, 1.0)
    return int(cols[0][0]) if t is not None and len(t) else None


def check_reset(sc, counter, restarts=True):
    # restarts: the PC model only counts the reset, its counter keeps running
    before = first_counter(sc, counter) if counter and restarts else None
    t0 = time.time()
    err = sc.reset()
    after = first_counter(sc, counter) if counter and not err else None
    detail = f'answers again after {time.time() - t0:.1f} s'
    if before is not None and after is not None:
        detail += f', counter {before} -> {after}'
    restarted = err is None and (before is None or after is None or after < before)
    result('OK' if restarted else 'FAIL', 'reset', err or detail,
           'firmware without the X command, or start-up takes longer than 2 s')
    if not counter or err:
        return
    sc.start([counter], 0, 1000)                # 'X' in the middle of a stream, without 'S' first
    time.sleep(1.0)
    sc.running = False
    sc.thread.join()
    sc.thread = None
    sc.ser.write(b'X')
    t0, ok = time.time(), False
    while not ok and time.time() - t0 < 3:
        ok = sc.ping()
    result('OK' if ok else 'FAIL', 'reset while streaming',
           f'answers again after {time.time() - t0:.1f} s' if ok else 'no answer within 3 s')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--port', required=True, help='COM port of the USB-UART adapter, or SIM')
    p.add_argument('--baud', type=int, default=115200)
    p.add_argument('--elf', default=ELF, help='ELF of the programmed firmware (default: test.X production)')
    p.add_argument('--secs', type=int, default=20, help='duration of the clock measurement')
    p.add_argument('--loopback', action='store_true', help='only check the adapter (TX and RX bridged)')
    p.add_argument('--report', default=os.path.join(ROOT, 'hw_report.txt'))
    a = p.parse_args()

    out(f'mini-scope hw_check  {datetime.datetime.now():%Y-%m-%d %H:%M}  port {a.port}, {a.baud} baud')
    out(f'Python {platform.python_version()}, {platform.platform()}')
    if a.loopback:
        loopback(a.port, a.baud)
    else:
        run_checks(a)
    out()
    out(f'Result: {results.count("OK")} OK, {results.count("WARN")} WARN, {results.count("FAIL")} FAIL')
    with open(a.report, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print(f'Report written to {os.path.abspath(a.report)}')
    sys.exit(1 if 'FAIL' in results else 0)


def run_checks(a):
    syms = {}
    if a.port == 'SIM':
        syms = g.sim_symbols()
        out('ELF: none, PC model')
    elif os.path.exists(a.elf):
        syms = g.load_symbols(a.elf)
        out(f'ELF: {a.elf}, built {datetime.datetime.fromtimestamp(os.path.getmtime(a.elf)):%Y-%m-%d %H:%M}')
    else:
        out(f'ELF: {a.elf} not found, checks with the demo variables are skipped')
    counter = (syms['counter'][0], 's32') if 'counter' in syms else None
    ramp = (syms['ramp'][0], 'u16') if 'ramp' in syms else None
    out()

    sc, baud = connect(a.port, a.baud)
    if not sc:
        return
    if baud != a.baud:
        result('FAIL', 'ping', f'answers at {baud} instead of {a.baud} baud: '
               f'the real FP is {baud / a.baud:.3g} x SCOPE_FP_HZ', 'TROUBLESHOOTING.md 4.3')
    else:
        result('OK', 'ping', f'SCP1, capture buffer {sc.capbuf} bytes, max. {sc.max_ch} channels')
    try:
        chans = [c for c in (counter, ramp) if c] or [(0x1000, 'u16')]   # 0x1000: start of data RAM
        check_clock(sc, chans, 0 if counter else None, a.secs)
        check_rates(sc, chans, baud)
        check_capture(sc, chans, chans.index(ramp) if ramp else None)
        if ramp and 'ramp_step' in syms:
            check_write(sc, ramp, syms['ramp_step'][0])
        check_reset(sc, counter, restarts=a.port != 'SIM')
    finally:
        sc.stop()
        g.close_port(sc.ser)


if __name__ == '__main__':
    main()
