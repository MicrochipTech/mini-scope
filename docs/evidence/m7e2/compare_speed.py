"""compare_speed.py - tune_speed.py on two ports, trace by trace, aligned at the step.

python docs/evidence/m7e2/compare_speed.py RUN_A RUN_B
RUN_A, RUN_B: run folders of tune_speed.py (results.csv, run.txt, trace_NNN.csv).
Per row: the gap between the write before the trace and the step write (target ms from run.txt),
the sample where the step reaches the model (first jump of iq), the speed there, and the table row.
"""
import csv
import os
import re
import sys

BASE = 1500


def rows(folder):
    with open(os.path.join(folder, 'results.csv'), newline='') as f:
        return list(csv.DictReader(f))


def trace(folder, n):
    with open(os.path.join(folder, f'trace_{n:03d}.csv'), newline='') as f:
        r = list(csv.DictReader(f))
    return [int(x['time_ms']) for x in r], [int(x['motor.speed']) for x in r], [int(x['motor.iq']) for x in r]


def param(folder, name):
    for line in open(os.path.join(folder, 'run.txt'), encoding='utf-8'):
        m = re.match(r'parameter %s = (\d+)' % name, line)
        if m:
            return int(m[1])
    raise KeyError(name)


def back(folder):
    """BACK_MS of the run: the trace starts that long after the write before the step"""
    return param(folder, 'BACK_MS')


def writes(folder):
    """target ms of every ref_offset write that starts a step, and of the write before it"""
    out, prev = [], None
    for line in open(os.path.join(folder, 'run.txt'), encoding='utf-8'):
        m = re.match(r'write (\d+) ms (\S+) = (-?\d+)$', line.strip())
        if not m:
            continue
        t, name, v = int(m[1]), m[2], int(m[3])
        if name == 'motor.ref_offset' and v != 0:
            out.append((prev, t))
        prev = t
    return out


def step_index(iq, kp):
    jump = max(10, kp * 300 // 1024 // 2)
    for i in range(1, len(iq)):
        if abs(iq[i] - iq[i - 1]) >= jump:
            return i
    return None


def measure(time_ms, speed, i0, offset, hold, poll=2):
    """the script's own rule, applied to the trace from sample i0 on"""
    target, size, d = BASE + offset, abs(offset), 1 if offset > 0 else -1
    band = size * 2 // 100
    peak, last = 0, 0
    for k in range(0, hold, poll):
        if i0 + k >= len(speed):
            break
        b = (speed[i0 + k] - target) * d
        peak = max(peak, b)
        if b > band or b < -band:
            last = k
    return peak * 100 // size, min(last + poll, hold)


def main(a, b):
    ra, rb = rows(a), rows(b)
    wa, wb = writes(a), writes(b)
    for f in (a, b):
        print(os.path.basename(os.path.normpath(f)), 'BACK_MS', back(f), 'POLL_MS', param(f, 'POLL_MS'))
    print('row kp ki off | gap A gap B | step A step B | v0 A v0 B | os A os B | st A st B')
    for n, (x, y) in enumerate(zip(ra, rb), 1):
        kp, off = int(x['kp']), int(x['offset'])
        ta, sa, qa = trace(a, n)
        tb, sb, qb = trace(b, n)
        ia, ib = step_index(qa, kp), step_index(qb, kp)
        ga = wa[n - 1][1] - wa[n - 1][0]
        gb = wb[n - 1][1] - wb[n - 1][0]
        print(f"{n:3} {kp:4} {x['ki']:>3} {off:4} | {ga:5} {gb:5} | {ta[ia]:6} {tb[ib]:6} | "
              f"{sa[ia - 1]:4} {sb[ib - 1]:4} | {x['overshoot_pct']:>4} {y['overshoot_pct']:>4} | "
              f"{x['settle_ms']:>4} {y['settle_ms']:>4}")




def aligned(a, b, n_rows=20, span=100):
    """per row: step delay after the script's measuring start, and the largest speed difference
    of the two traces aligned at the step, before (-20..-1) and after (0..span-1)"""
    wa, wb = writes(a), writes(b)
    print('row | delay A delay B | pre max|dv| | post max|dv| first k with dv != 0')
    for n in range(1, n_rows + 1):
        kp = int(rows(a)[n - 1]['kp'])
        ta, sa, qa = trace(a, n)
        tb, sb, qb = trace(b, n)
        ia, ib = step_index(qa, kp), step_index(qb, kp)
        da = ta[ia] - (wa[n - 1][1] - wa[n - 1][0] - back(a))
        db = tb[ib] - (wb[n - 1][1] - wb[n - 1][0] - back(b))
        pre = max(abs(sa[ia - k] - sb[ib - k]) for k in range(1, 21) if ia - k >= 0 and ib - k >= 0)
        dv = [sa[ia + k] - sb[ib + k] for k in range(span) if ia + k < len(sa) and ib + k < len(sb)]
        first = next((k for k, x in enumerate(dv) if x), None)
        print(f'{n:3} | {da:7} {db:7} | {pre:11} | {max(map(abs, dv)):12} {first}')


def remeasure(a, b, n_rows=20, hold=100):
    """the script's rule on the trace: from the measuring start (must give the table) and from the step"""
    print('row | table A  from start A  from step A | table B  from start B  from step B | peak at A B')
    for n in range(1, n_rows + 1):
        out = []
        for f in (a, b):
            r = rows(f)[n - 1]
            kp, off = int(r['kp']), int(r['offset'])
            t, s, q = trace(f, n)
            w = writes(f)[n - 1]
            i_start = next(i for i, x in enumerate(t) if x >= w[1] - w[0] - back(f))
            i_step = step_index(q, kp)
            d = 1 if off > 0 else -1
            peak_k = max(range(hold), key=lambda k: (s[i_step + k] - BASE - off) * d if i_step + k < len(s) else -9e9)
            out.append((f"{r['overshoot_pct']}/{r['settle_ms']}", measure(t, s, i_start, off, hold, param(f, 'POLL_MS')),
                        measure(t, s, i_step, off, hold, param(f, 'POLL_MS')), peak_k))
        (ta, sa, pa, ka), (tb, sb, pb, kb) = out
        print(f'{n:3} | {ta:>7} {str(sa):>13} {str(pa):>12} | {tb:>7} {str(sb):>13} {str(pb):>12} | {ka:3} {kb:3}')


if __name__ == '__main__':
    main(*sys.argv[1:3])
    print()
    aligned(*sys.argv[1:3])
    print()
    remeasure(*sys.argv[1:3])
