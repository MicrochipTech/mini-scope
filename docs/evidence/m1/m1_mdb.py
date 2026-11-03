"""m1_mdb.py - card 1.1: starts of the virtual motor in the MPLAB X simulator.

    python docs/evidence/m1/m1_mdb.py

Needs the ELF of configuration sim (test.X, built as in start_sim.bat). Streams
motor.state, motor.speed, motor.iq and motor.fault at 1 ms (target time, the
10 kHz interrupt also kicks the TX interrupt, see DECISIONS.md), writes
motor.run through the 'W' command and prints the state changes in ms after the
first change, the numbers to compare with model_check.c.
Then streams motor.current_ref and motor.current_* and writes a current step at
standstill.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'm0'))
from m0_common import Frames, MdbSerial, config_stream, start, symbols, write_cmd   # noqa: E402

ELF = os.path.join(HERE, '..', '..', '..', 'test.X', 'dist', 'sim', 'production', 'test.X.production.elf')

MOTOR = ['run', 'state', 'speed', 'speed_ref', 'iq', 'fault', 'kp', 'ki', 'ref_offset',
         'current_ref', 'current', 'current_kp', 'current_ki']
STARTUP = ['ramp_rate', 'align_current', 'handover_speed']


def s16(v):
    return v - 65536 if v >= 32768 else v


def collect(fr, n_samples, timeout=600):
    """Frames until n_samples more sample numbers have passed."""
    rows = []
    t = time.time()
    first = None
    while time.time() - t < timeout:
        rows += fr.poll()
        if rows and first is None:
            first = rows[0][0]
        if rows and rows[-1][0] - first >= n_samples:
            break
        time.sleep(0.2)
    return rows


def changes(rows, base=None):
    """State changes as (ms after the first one, state, speed, iq, fault)."""
    out, prev = [], None
    for idx, (state, speed, iq, fault) in rows:
        speed, iq = s16(speed), s16(iq)
        if prev is not None and state != prev:
            base = idx if base is None else base
            out.append((idx - base, prev, state, speed, iq, fault))
        prev = state
    return out


def phase(ser, fr, label, addr, writes, run_ms, stop_ms):
    """Writes the settings, run = 1 for run_ms, run = 0 for stop_ms (samples)."""
    for name, val in writes:
        ser.write(write_cmd(addr[name], val))
    fr.poll()
    ser.write(write_cmd(addr['run'], 1))
    up = collect(fr, run_ms)
    ser.write(write_cmd(addr['run'], 0))
    down = collect(fr, stop_ms)
    allrows = up + down
    print(f'--- {label}')
    ch = changes([(i, v) for i, v in allrows])
    for c in ch:
        print('  +%4d ms  state %d -> %d  speed %6d  iq %5d  fault %d' % c)
    last = None
    for (i0, v0), (i1, v1) in zip(allrows, allrows[1:]):
        if v0[0] != 0 and v1[0] == 0:
            last = i1                   # sample where the model stopped
    if last is not None:
        idle = [i for i, v in allrows if i >= last and s16(v[1]) == 0]
        print(f'  speed 0 after {idle[0] - last if idle else -1} ms in state 0')
    maxiq = max(s16(v[2]) for _, v in allrows)
    print(f'  max iq {maxiq}, final fault {allrows[-1][1][3]}, samples {len(allrows)}, lost {fr.lost}, bad {fr.bad}')
    return allrows


def main():
    sym = symbols(ELF)
    m = {n: sym['motor'] + 2 * i for i, n in enumerate(MOTOR)}
    u = {n: sym['startup'] + 2 * i for i, n in enumerate(STARTUP)}
    print('symbols: motor 0x%04x startup 0x%04x tune_alive 0x%04x' % (sym['motor'], sym['startup'], sym['tune_alive']))
    ser = MdbSerial(ELF, progress=lambda f, t: None)
    start(ser, [m['state'], m['speed'], m['iq'], m['fault']])
    fr = Frames(ser, 4)
    collect(fr, 50)

    # the first mailbox write after Run can get lost, run = 0 is harmless
    ser.write(write_cmd(m['run'], 0))

    # restore defaults of the sweep, then a good start (rate 100) and a failing one (rate 400)
    for name, val in (('ramp_rate', 100), ('align_current', 400), ('handover_speed', 600)):
        ser.write(write_cmd(u[name], val))
    phase(ser, fr, 'good start, ramp_rate 100', m, [], 400, 150)
    ser.write(write_cmd(u['ramp_rate'], 400))
    phase(ser, fr, 'failing start, ramp_rate 400', m, [], 250, 100)
    ser.write(write_cmd(u['ramp_rate'], 200))
    phase(ser, fr, 'good start, ramp_rate 200', m, [], 300, 150)
    ser.write(write_cmd(u['ramp_rate'], 50))
    phase(ser, fr, 'good start, ramp_rate 50', m, [], 400, 150)
    ser.write(write_cmd(u['ramp_rate'], 100))
    ser.write(write_cmd(u['handover_speed'], 150))
    phase(ser, fr, 'failing start, handover 150', m, [], 250, 100)
    ser.write(write_cmd(u['handover_speed'], 600))
    ser.write(write_cmd(u['align_current'], 200))
    phase(ser, fr, 'failing start, align 200', m, [], 250, 100)
    ser.write(write_cmd(u['align_current'], 400))
    phase(ser, fr, 'good start again (deterministic?)', m, [], 400, 150)

    # current loop at standstill
    ser.write(b'S')
    time.sleep(8)
    ser.write(config_stream([m['current_ref'], m['current'], m['current_kp'], m['state']], 1000))
    ser.write(b'R')
    t = time.time()
    while ser.in_waiting < 100 and time.time() - t < 60:
        time.sleep(0.2)
    fr2 = Frames(ser, 4)
    collect(fr2, 20)
    ser.write(write_cmd(m['current_ref'], 500))
    rows = collect(fr2, 60)
    ser.write(write_cmd(m['current_ref'], 0))
    rows += collect(fr2, 40)
    print('--- current step 500 at standstill, 1 ms samples (ref, current, kp)')
    print('  ', [(i - rows[0][0], s16(v[0]), s16(v[1])) for i, v in rows][::6][:20])

    ser._cmd('Quit')
    ser.p.wait(20)


if __name__ == '__main__':
    main()
