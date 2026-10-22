"""test_protocol.py - GUI protocol layer against the real scope.c (sim.dll).

    python tests/test_protocol.py
"""
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, '..', 'gui')]
from scope_gui import Scope              # noqa: E402
from sim_serial import SimSerial, SYMBOLS  # noqa: E402

CH = [(SYMBOLS['sine'][0], 's16'), (SYMBOLS['ramp'][0], 'u16'),
      (SYMBOLS['counter'][0], 's32'), (SYMBOLS['toggle'][0], 'u8')]


def check(cond, text):
    print(('OK   ' if cond else 'FAIL ') + text)
    if not cond:
        check.failed = True


def run(sc, secs):
    time.sleep(secs)
    t, cols = sc.snapshot()
    sc.stop()
    return t, cols


sc = Scope(SimSerial(os.environ.get('SIM_DLL', os.path.join(HERE, 'sim.dll'))))
CAPBUF = int(os.environ.get('SCOPE_CAPBUF', 4096))   # value the DLL was built with
check(sc.ping(), 'ping -> SCP1')
check(sc.capbuf == CAPBUF and sc.max_ch == 16, f'ping reports SCOPE_CAPBUF {sc.capbuf}, SCOPE_MAX_CH {sc.max_ch}')

# stream, slow enough for the UART: nothing may get lost
check(sc.start(CH, 0, 2000) is None, 'stream config ACK')
t, (sine, ramp, cnt, tog) = run(sc, 1.0)
check(len(t) > 250, f'stream 500 Hz: {len(t)} samples in 1 s')
check(sc.lost == 0, f'no lost frames ({sc.lost})')
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

sys.exit(1 if getattr(check, 'failed', False) else 0)
