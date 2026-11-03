"""replay.py - replays the writes of a tune_speed.py run into tune_model.h on the PC and compares with its traces.

python docs/evidence/m7e2/replay.py RUN_FOLDER [delay_ms]   (needs model.dll, see model_dll.c)
The model (model.dll, built from firmware/tune_model.h alone) steps in whole ms. Every write of run.txt is applied
at its target time plus delay_ms; the ref_offset writes, which the traces show, at the sample where iq jumps.
Prints, per trace, the largest |speed| difference between replay and trace. 0 everywhere means: this run is
tune_model.h with this write schedule, nothing else.
"""
import ctypes
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compare_speed as c      # noqa: E402

FIELDS = {'motor.run': 0, 'motor.kp': 6, 'motor.ki': 7, 'motor.ref_offset': 8}
STARTUP = {'startup.ramp_rate': 0, 'startup.align_current': 1, 'startup.handover_speed': 2}


def schedule(folder, delay):
    ev = []
    rows = c.rows(folder)
    lines = [l.strip() for l in open(os.path.join(folder, 'run.txt'), encoding='utf-8')]
    t0s = []
    for line in lines:
        m = re.match(r'write (\d+) ms (\S+) = (-?\d+)$', line)
        if m:
            ev.append([int(m[1]) + delay, m[2], int(m[3])])
    # ref_offset writes: landing time from the trace. trace n starts at the cursor of the write before the step + 50
    steps = [i for i, e in enumerate(ev) if e[1] == 'motor.ref_offset' and e[2] != 0]
    lag = 3 if 'MdbSerial' in ''.join(lines[:4]) else 0
    for n, i in enumerate(steps[:len(rows)], 1):
        kp = int(rows[n - 1]['kp'])
        t, s, q = c.trace(folder, n)
        t0 = ev[i - 1][0] - delay + lag + 50
        j = c.step_index(q, kp)
        ev[i][0] = t0 + t[j]
        b = next(x for x in range(j + 90, len(q)) if abs(q[x] - q[x - 1]) >= max(10, kp * 300 // 1024 // 2))
        ev[i + 1][0] = t0 + t[b]
        t0s.append(t0)
    return ev, t0s


def replay(folder, delay):
    dll = ctypes.CDLL(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'model.dll'))
    dll.m_motor.restype = ctypes.POINTER(ctypes.c_int16)
    dll.m_startup.restype = ctypes.POINTER(ctypes.c_uint16)
    dll.m_init()
    mo, su = dll.m_motor(), dll.m_startup()
    ev, t0s = schedule(folder, delay)
    ev.sort(key=lambda e: e[0])
    end = max(e[0] for e in ev) + 400
    speed, k = [0] * (end + 1), 0
    for ms in range(1, end + 1):
        while k < len(ev) and ev[k][0] <= ms:
            _, name, v = ev[k]
            if name in FIELDS:
                mo[FIELDS[name]] = v
            elif name in STARTUP:
                su[STARTUP[name]] = v
            k += 1
        dll.m_ms()
        speed[ms] = mo[2]
    out = []
    for n, t0 in enumerate(t0s, 1):
        t, s, q = c.trace(folder, n)
        d = [abs(speed[t0 + x] - y) for x, y in zip(t, s)]
        out.append(max(d))
    return out


if __name__ == '__main__':
    f = sys.argv[1]
    for delay in ([int(sys.argv[2])] if len(sys.argv) > 2 else range(0, 8)):
        r = replay(f, delay)
        print(f'delay {delay}: max |dv| per trace {r}')
