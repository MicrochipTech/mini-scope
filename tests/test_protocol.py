"""test_protocol.py - GUI protocol layer against the real scope.c (sim.dll).

    python tests/test_protocol.py
"""
import os
import sys
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, '..', 'gui'), os.path.join(HERE, '..', 'tools')]
from scope_gui import Scope, measure     # noqa: E402
from sim_serial import SimSerial, SYMBOLS  # noqa: E402
from mdb_serial import MdbSerial          # noqa: E402 (class attributes only, MDB is not started)

CH = [(SYMBOLS['sine'][0], 's16'), (SYMBOLS['ramp'][0], 'u16'),
      (SYMBOLS['counter'][0], 's32'), (SYMBOLS['toggle'][0], 'u8')]


class NoisySerial(SimSerial):
    """Inserts junk bytes into the target's output on request (a glitch on the line)."""
    pending = 0

    def _advance(self):
        with self.lock:
            before = len(self.buf)
            super()._advance()
            if self.pending and len(self.buf) > before + 4:
                mid = (before + len(self.buf)) // 2
                self.buf[mid:mid] = bytes(3)
                self.pending -= 1


class SlowSerial(SimSerial):
    """Like MdbSerial.write: the write itself takes a long time (outside the port lock)."""
    delay = 0.0

    def write(self, data):
        time.sleep(self.delay)
        return super().write(data)


def check(cond, text):
    print(('OK   ' if cond else 'FAIL ') + text)
    if not cond:
        check.failed = True


def run(sc, secs):
    time.sleep(secs)
    t, cols = sc.snapshot()
    sc.stop()
    return t, cols


sc = Scope(NoisySerial(os.environ.get('SIM_DLL', os.path.join(HERE, 'sim.dll'))))
CAPBUF = int(os.environ.get('SCOPE_CAPBUF', 4096))   # value the DLL was built with
check(sc.ping(), 'ping -> SCP1')
check(sc.capbuf == CAPBUF and sc.max_ch == 16, f'ping reports SCOPE_CAPBUF {sc.capbuf}, SCOPE_MAX_CH {sc.max_ch}')

# stream, slow enough for the UART: nothing may get lost
check(sc.start(CH, 0, 2000) is None, 'stream config ACK')
t, (sine, ramp, cnt, tog) = run(sc, 1.0)
check(len(t) > 250, f'stream 500 Hz: {len(t)} samples in 1 s')
check(sc.lost == 0 and sc.resyncs == 0, f'no lost frames ({sc.lost}), no resyncs ({sc.resyncs})')
check(np.all(np.diff(cnt) == np.diff(t)), 'counter steps == sample index steps')
check(np.all((np.diff(ramp.astype(int)) - 7) % 65536 == 0), 'ramp +7 per sample')
check(sine.min() < -15000 and sine.max() > 15000, f'sine range {sine.min()}..{sine.max()}')

# stream, far too fast: frames get dropped, but indices must stay consistent
check(sc.start(CH, 0, 20) is None, 'fast stream config ACK')
t, (sine, ramp, cnt, tog) = run(sc, 1.0)
check(sc.lost > 0, f'fast stream drops frames ({sc.lost} lost, {len(t)} received)')
d = np.diff(t)
check(np.all(np.diff(cnt)[d < 256] == d[d < 256]), 'counter steps == index steps despite gaps')

# capture with rising trigger on sine at 0, 20 % pretrigger, 10 us sampling
depth = sc.capbuf // 9
pre = int(depth * 20 / 100)          # GUI sends 20 %
check(sc.start(CH, 1, 10, 0, 1, 0, 20, auto=False) is None, 'capture config ACK')
time.sleep(0.5)
t, (sine, ramp, cnt, tog) = sc.snapshot()
check(sc.captures == 1 and len(t) == depth, f'one capture of {len(t)} samples (expected {depth})')
i0 = int(np.flatnonzero(t == 0)[0]) if len(t) else 0
check(i0 == pre, f'trigger at index {i0} (pre {pre})')
check(len(t) and sine[i0 - 1] < 0 <= sine[i0], f'rising edge at trigger: {sine[i0 - 1]} -> {sine[i0]}')
check(len(t) and np.all(np.diff(cnt) == 1), 'capture is gapless (counter +1)')
sc.stop()

# capture in auto mode keeps re-arming
check(sc.start(CH, 1, 10, 0, 2, 5000, 20, auto=True) is None, 'auto capture config ACK')
time.sleep(2.0)                         # one capture takes about 0.4 s at 115200 baud
t, (sine, ramp, cnt, tog) = sc.snapshot()
check(sc.captures >= 3, f'auto re-arm: {sc.captures} captures in 2 s')
i0 = int(np.flatnonzero(t == 0)[0])
check(sine[i0 - 1] > 5000 >= sine[i0], f'falling edge through 5000: {sine[i0 - 1]} -> {sine[i0]}')
sc.stop()

# odd address on a 16 bit channel must be rejected (address error trap)
check(sc.start([(0x1001, 's16')], 0, 1000) is not None, 'odd address -> NAK')
check(sc.ping(), 'target still alive after NAK')

# 'X' resets the target, in every state (here while streaming)
check(sc.start(CH, 0, 2000) is None, 'stream config ACK before reset')
time.sleep(0.2)
n = sc.ser.lib.sim_resets()
check(sc.reset() is None and sc.ser.lib.sim_resets() == n + 1, "'X' -> reset, target answers again")

# 'W' writes variables, also while streaming: sine_gain scales the sine, ramp_step sets the ramp slope
GAIN, STEP = SYMBOLS['sine_gain'][0], SYMBOLS['ramp_step'][0]
check(sc.start(CH[:2], 0, 2000) is None, 'stream for the write test')
time.sleep(1.0)
t, (sine, ramp) = sc.snapshot()
amp0, slope0 = np.abs(sine).max(), np.median(np.diff(ramp.astype(np.int64)) % 65536)
check(sc.write(GAIN, 's16', 16384) is None and sc.write(STEP, 'u16', 14) is None, 'write while streaming')
time.sleep(0.3)
n = len(sc.snapshot()[0])
time.sleep(1.0)
t, (sine, ramp) = sc.snapshot()
amp1, slope1 = np.abs(sine[n:]).max(), np.median(np.diff(ramp[n:].astype(np.int64)) % 65536)
sc.stop()
check(abs(amp1 / amp0 - 0.5) < 0.02, f'sine_gain 16384 halves the sine: {amp0} -> {amp1}')
check(slope1 == 2 * slope0, f'ramp_step 14 doubles the ramp slope: {slope0} -> {slope1}')
check(sc.write(STEP + 1, 'u16', 1) is not None, 'write to an odd address rejected by the GUI')
check(sc.write(STEP, 'u16', 70000) is not None and sc.write(STEP, 'u16', 1.5) is not None,
      'value out of range / not whole rejected by the GUI')
sc.ser.write(b'W' + (STEP + 1).to_bytes(2, 'little') + bytes([2]) + bytes(4))
check(sc.ping(), "target ignores 'W' with an odd address and still answers")

# write_many: N writes leave N values in the target, in one ser.write, nothing is sent if one is invalid
NAMES = ['sine_gain', 'ramp_step', 'motor.kp', 'motor.ki', 'motor.ref_offset', 'motor.current_kp',
         'motor.current_ki', 'startup.ramp_rate', 'startup.align_current', 'startup.handover_speed']
want = {n: 100 + 7 * i for i, n in enumerate(NAMES)}
check(sc.start([(SYMBOLS[n][0], 'u16') for n in NAMES], 0, 4000) is None, 'stream of the written variables')
calls, ser_write = [], sc.ser.write
sc.ser.write = lambda d: (calls.append(len(d)), ser_write(d))[1]
check(sc.write_many([(SYMBOLS[n][0], 'u16', v) for n, v in want.items()]) is None, 'write_many of 10 variables')
check(calls == [80], f'one ser.write of 80 bytes for 10 writes ({calls})')
del calls[:]
time.sleep(0.6)
t, cols = sc.snapshot()
got = {n: int(c[-1]) for n, c in zip(NAMES, cols)}
check(got == want, f'all 10 values arrived in the target ({got == want})')
err = sc.write_many([(SYMBOLS['ramp_step'][0], 'u16', 5), (SYMBOLS['ramp_step'][0] + 1, 'u16', 5)])
check(err is not None and calls == [], f'one invalid write: error "{err}", nothing sent')
check(sc.write_many([]) is None and calls == [], 'empty write_many sends nothing')
sc.ser.write = ser_write
sc.stop()

# concurrent writers during a stream: nothing garbled, order kept (each batch writes k..k+3 to one variable)
GAINS = SYMBOLS['sine_gain'][0]


def hammer(addr, typ, base, rounds, errs):
    for r in range(rounds):
        e = sc.write_many([(addr, typ, base + 4 * r + j) for j in range(4)])
        if e:
            errs.append(e)
        time.sleep(0.004)


errs = []
sc.write_many([(STEP, 'u16', 0), (GAINS, 's16', 0)])
check(sc.start([(STEP, 'u16'), (GAINS, 's16')], 0, 2000) is None, 'stream for the concurrency test')
th = [threading.Thread(target=hammer, args=(STEP, 'u16', 1, 100, errs)),
      threading.Thread(target=hammer, args=(GAINS, 's16', 1001, 100, errs))]
[x.start() for x in th]
[x.join() for x in th]
time.sleep(0.4)
t, (stp, gn) = sc.snapshot()
lost, resyncs = sc.lost, sc.resyncs
sc.stop()
check(not errs and len(t) > 300, f'2 threads x 100 batches of 4 writes during the stream, {len(t)} samples')
check(lost == 0 and resyncs == 0, f'no lost ({lost}), no garbled frames ({resyncs} resyncs)')
check(np.all(np.diff(stp.astype(int)) >= 0) and np.all(np.diff(gn.astype(int)) >= 0),
      'observed values only rise: every write takes effect in order')
check(stp[-1] == 400 and gn[-1] == 1400, f'last values arrived: {stp[-1]} (400), {gn[-1]} (1400)')
check(len(np.unique(stp)) > 20, f'{len(np.unique(stp))} different values seen while writing')
sc.write_many([(STEP, 'u16', 7), (GAINS, 's16', 32767)])

# a writer that sleeps must not starve the reader, and stop() must not deadlock
sl = Scope(SlowSerial(os.environ.get('SIM_DLL', os.path.join(HERE, 'sim.dll'))))
check(sl.ping() and sl.start(CH[:2], 0, 2000) is None, 'stream on a port with a slow write')
sl.ser.delay = 1.0
w = threading.Thread(target=sl.write_many, args=([(STEP, 'u16', 7)],))
n0 = len(sl.snapshot()[0])
w.start()
time.sleep(0.5)
n1 = len(sl.snapshot()[0])
check(w.is_alive() and n1 - n0 > 100, f'reader keeps running while a write sleeps: {n1 - n0} samples in 0.5 s')
t0 = time.time()
sl.stop()
w.join()
check(time.time() - t0 < 2.5 and not w.is_alive(), f'stop() while a write sleeps: done after {time.time() - t0:.2f} s')
sl.ser.delay = 0.0

# port attributes with defaults (read with getattr, like quiet)
plain = Scope(object())
check(plain.time_scale == 1 and plain.output_lag_ms == 0, 'plain port: time_scale 1, output_lag_ms 0')
check(sc.time_scale == 1 and sc.output_lag_ms == 0, 'SimSerial: time_scale 1, output_lag_ms 0')
check(MdbSerial.time_scale == 25 and MdbSerial.output_lag_ms == 3, 'MdbSerial: time_scale 25, output_lag_ms 3')

# line noise: junk bytes inside a stream are counted as resyncs and the stream recovers
check(sc.start(CH[:2], 0, 2000) is None, 'stream config ACK for the noise test')
time.sleep(0.3)
for _ in range(5):
    sc.ser.pending = 1
    time.sleep(0.25)
time.sleep(0.5)
t, (sine, ramp) = sc.snapshot()
n_before = len(t)
time.sleep(0.5)
t, (sine, ramp) = sc.snapshot()
resyncs = sc.resyncs
sc.stop()
check(resyncs >= 4, f'5 injected junk bursts -> resyncs {resyncs}')
check(len(t) > n_before + 100, f'stream goes on after the noise: {len(t) - n_before} samples in 0.5 s')
tail = ramp[-200:].astype(np.int64)
check(np.all((np.diff(tail) - 7) % 65536 == 0), 'the last 200 samples are clean again (ramp +7)')

# measurements under the chart (GUI only)
tm = np.arange(0, 200, 1.0)
m = measure(tm, 1000 * np.sin(2 * np.pi * 50 * tm / 1000 + 0.3))
check(abs(m['freq'] - 50) < 0.1 and abs(m['rms'] - 707.1) < 1 and abs(m['duty'] - 50) < 2,
      f"measure sine 50 Hz: {m['freq']:.2f} Hz, RMS {m['rms']:.1f}, duty {m['duty']:.0f} %")
m = measure(tm, np.where((tm % 10) < 2, 5.0, 0.0))
check(abs(m['freq'] - 100) < 0.1 and abs(m['duty'] - 20) < 0.5 and m['pkpk'] == 5,
      f"measure square 100 Hz, 20 %: {m['freq']:.2f} Hz, duty {m['duty']:.0f} %")
noisy = 1000 * np.sin(2 * np.pi * 20 * tm / 1000) + np.random.default_rng(1).normal(0, 30, len(tm))
check(abs(measure(tm, noisy)['freq'] - 20) < 0.2, 'measure noisy sine: the hysteresis ignores the noise')
check(measure(tm, np.full(len(tm), 3.0))['freq'] is None, 'measure flat line: no frequency')

sys.exit(1 if getattr(check, 'failed', False) else 0)
