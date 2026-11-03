"""m0_stream.py - results 1 to 3 of card M0: second timer, stream stability, slowdown.

    python docs/evidence/m0/m0_stream.py --fast 2 --secs 300

--fast  0 baseline, 1 empty 10 kHz interrupt, 2 realistic body, see m0_load.h
--kick  also sets _U1TXIF in the 10 kHz interrupt (simulator TX interrupt workaround)
--secs  wall clock seconds of streaming

Target time comes from the MDB Stopwatch, which counts instruction cycles (32 MHz
here; the seconds it prints are wrong because they assume 64 MHz per cycle).
"""
import argparse
import re
import statistics
import time

from m0_common import Frames, MdbSerial, build, mdb_lines, start, symbols

FCY = 32e6
# the simulator does not raise the TX interrupt again, the 10 kHz interrupt kicks it
KICK = [('    _CCT1IF = 0;\n', '    _CCT1IF = 0;\n    _U1TXIF = 1;\n')]


def stopwatch(ser):
    ser._cmd('Stopwatch')
    t = time.time()
    while time.time() - t < 8:
        for line in mdb_lines(ser, 0.1):
            m = re.search(r'Stopwatch cycle count = (\d+)', line)
            if m:
                return (t + time.time()) / 2, int(m.group(1))
    raise TimeoutError('no Stopwatch answer')


def printed(ser, name):
    ser._cmd(f'Print {name}')
    t = time.time()
    while time.time() - t < 8:
        lines = mdb_lines(ser, 0.3)
        for i, line in enumerate(lines):
            if line.endswith(name + '='):
                return lines[i + 1] if i + 1 < len(lines) else mdb_lines(ser, 1)[0]
    raise TimeoutError(f'no Print answer for {name}')


def slope(points):
    """Least squares cycles per wall second."""
    xm = statistics.mean(p[0] for p in points)
    ym = statistics.mean(p[1] for p in points)
    return sum((p[0] - xm) * (p[1] - ym) for p in points) / sum((p[0] - xm) ** 2 for p in points)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fast', type=int, default=2)
    ap.add_argument('--kick', action='store_true')
    ap.add_argument('--secs', type=float, default=120)
    a = ap.parse_args()

    elf = build(f'stream_{a.fast}_{int(a.kick)}', a.fast, load_edits=KICK if a.kick else ())
    sym = symbols(elf)
    if a.fast:
        chans = ['fast_ticks', 'CCP2TMRL', 'probe', 'fast_cycles']
    else:
        chans = ['ramp', 'sine', 'probe', 'ramp_step']
    ser = MdbSerial(elf, progress=lambda f, t: None)
    start(ser, [sym[c] for c in chans])
    fr = Frames(ser, 4)
    rows = []
    sw = [stopwatch(ser)]
    t0 = time.time()
    next_sw = t0 + 5
    file_pts = []
    while time.time() - t0 < a.secs:
        new = fr.poll()
        rows += new
        if new:
            file_pts.append((time.time(), new[-1][0]))
        if time.time() >= next_sw:
            sw.append(stopwatch(ser))
            next_sw += 5
        time.sleep(0.2)
    wall = time.time() - t0
    t_end = stopwatch(ser)
    sw.append(t_end)

    ser._cmd('Halt')
    mdb_lines(ser, 3)
    total_cycles = stopwatch(ser)[1]
    fast_total = int(printed(ser, 'fast_total'))
    overflows = int(printed(ser, 'scope_overflows'))
    time.sleep(6)
    rows += fr.poll()
    ser._cmd('Quit')
    ser.p.wait(20)

    rate = slope(sw)
    print(f'fast={a.fast} kick={a.kick} wall={wall:.0f}s channels={chans}')
    print(f'cycles/s {rate:.0f}, slowdown from Stopwatch {FCY / rate:.2f}'
          f' ({len(sw)} readings, {sw[-1][1] - sw[0][1]} cycles)')
    print(f'slowdown from file {1000 * (file_pts[-1][0] - file_pts[3][0]) / (file_pts[-1][1] - file_pts[3][1]):.2f}'
          f' (samples {file_pts[3][1]}..{file_pts[-1][1]})')
    print(f'samples seen {fr.index} received {fr.received} lost {fr.lost}'
          f' ({100 * fr.lost / max(fr.index, 1):.2f} %) bad bytes {fr.bad}'
          f' scope_overflows {overflows} (u16)')
    late = [r for r in rows if r[0] > 3000]
    if late:
        span = late[-1][0] - late[0][0] + 1
        print(f'after sample 3000: span {span} received {len(late)}'
              f' lost {100 * (span - len(late)) / span:.2f} %')
    per = {}
    for a_, b_ in zip(rows, rows[1:]):
        per.setdefault(b_[0] - a_[0], []).append(a_)
    print('gaps (distance: count)', {k: len(v) for k, v in sorted(per.items())})
    if a.fast:
        dt = [(b[1][0] - x[1][0]) & 0xFFFF for x, b in zip(rows, rows[1:]) if b[0] - x[0] == 1]
        dc = [(b[1][1] - x[1][1]) & 0xFFFF for x, b in zip(rows, rows[1:]) if b[0] - x[0] == 1]
        print(f'fast ticks per sample: min {min(dt)} max {max(dt)} mean {statistics.mean(dt):.4f}')
        print(f'SCCP2 ticks per sample (8000 = 1 ms): min {min(dc)} max {max(dc)} mean {statistics.mean(dc):.2f}')
        cyc = [r[1][3] for r in rows]
        print(f'ISR length in FP/4 ticks: min {min(cyc)} max {max(cyc)} = {4 * max(cyc)} cycles,'
              f' {100 * 4 * max(cyc) / 3200:.1f} % of 100 us')
        print(f'fast_total {fast_total} in {total_cycles} cycles since the last halt:'
              f' {fast_total / (total_cycles / FCY):.2f} Hz')
    else:
        print(f'total cycles since start {total_cycles}')


if __name__ == '__main__':
    main()
