"""m7b_load.py - card M7b: mini-scope under a realistic control load, in the MPLAB X simulator.

    python docs/evidence/m7b/m7b_load.py --elf <load ELF from make_load.py> --mips 32|100 [--phases ABCDEF] [--secs 20]

One MDB session. Phases (each runs once):
  A  load off, mini-scope idle              idle loop rate without anything
  B  load on, mini-scope idle               share of the control interrupt
  C  load on, stream 1 ms, 4 channels       --secs of target time: frames, lost, resyncs, sample spacing
  a  kick only (control interrupt with a one cycle body), mini-scope idle: baseline for D
  D  kick only, stream 1 ms                 share of mini-scope alone
  E  load on, stream 500 us (more than the line carries): lost frames must be counted, not hidden
  F  load on, capture with trigger from 100 us down to 1 us, one without trigger, 1 us with the load off

Idle counts and target time come from the firmware: the PC writes snap_req, the main loop copies idle and the
32 bit time base SCCP2 (FP ticks) in one pass. The Stopwatch cycles are read too, as a cross check.
Channel 0 of every stream and capture is SCCP3, a free running 16 bit timer at FP/4, so the spacing of the
samples is measured in target time, not assumed. Nothing in tools/ or gui/ is changed: the script uses
Scope (gui/scope_gui.py) and MdbSerial (tools/mdb_serial.py) as they are.
"""
import argparse
import os
import queue
import re
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
sys.path[:0] = [os.path.join(ROOT, 'gui'), os.path.join(ROOT, 'tools')]
from mdb_serial import MdbSerial, symbols       # noqa: E402
from scope_gui import Scope                     # noqa: E402

CH_NAMES = ['CCP3TMRL', 'isr_ticks', 'ramp', 'isr_len']


class Mdb(MdbSerial):
    """MdbSerial with the simulated oscillator set for the MIPS of the build (FOSC = 2 x FP). Only this one
    MDB command differs, tools/mdb_serial.py is used as it is."""
    fosc_mhz = 64

    def _cmd(self, c):
        if c.startswith('Set oscillator.frequency '):
            c = f'Set oscillator.frequency {self.fosc_mhz}'
        super()._cmd(c)


def say(*a):
    print(*a, flush=True)


class Target:
    def __init__(self, elf, mips):
        self.fp = mips * 1e6
        self.sym = symbols(elf)
        Mdb.fosc_mhz = 2 * mips
        t0 = time.time()
        self.ser = Mdb(elf, progress=lambda f, t: None)
        say(f'MDB started in {time.time() - t0:.0f} s, oscillator.frequency {Mdb.fosc_mhz} MHz, '
            f'power throttling off for {self.ser.throttling_off} processes')
        self.sc = Scope(self.ser)

    def drain(self):
        while True:
            try:
                self.ser.lines.get_nowait()
            except queue.Empty:
                return

    def printed(self, name):
        self.drain()
        self.ser._cmd(f'Print {name}')
        end, seen = time.time() + 30, False
        while time.time() < end:
            try:
                s = self.ser.lines.get(timeout=0.2).strip()
            except queue.Empty:
                continue
            if s.endswith(name + '='):
                seen = True
            elif seen and re.fullmatch(r'>?\s*-?\d+', s):
                return int(s.lstrip('>'))
        raise TimeoutError(f'no Print answer for {name}')

    def stopwatch(self):
        self.drain()
        self.ser._cmd('Stopwatch')
        end = time.time() + 30
        while time.time() < end:
            try:
                m = re.search(r'Stopwatch cycle count = (\d+)', self.ser.lines.get(timeout=0.2))
            except queue.Empty:
                continue
            if m:
                return int(m.group(1))
        raise TimeoutError('no Stopwatch answer')

    def put(self, name, value):
        self.ser._cmd(f'write {self.sym[name]} {value}')

    def snap(self):
        """(idle, time base in FP ticks, Stopwatch cycles right after) of one pass of the main loop."""
        self.put('snap_req', 1)
        for _ in range(100):
            if self.printed('snap_req') == 0:
                break
            time.sleep(0.2)
        else:
            raise TimeoutError('main loop does not answer snap_req')
        return self.printed('snap_idle'), self.printed('snap_time'), self.stopwatch()

    def load(self, mode):
        """off: control interrupt stopped. kick: it runs with a body of one cycle, so the simulator workaround
        for the TX interrupt (see load_main.c) still works; needed for any stream. on: the 60 % body."""
        if not hasattr(self, 'burn'):
            self.burn = self.printed('load_burn')
        self.put('load_burn', 1 if mode == 'kick' else self.burn)
        self.put('load_on', 0 if mode == 'off' else 1)
        time.sleep(1.0)
        assert self.printed('load_on') == (0 if mode == 'off' else 1)

    def window(self, secs):
        """Snapshots about every second of wall clock until `secs` of target time (time base) have passed.
        The 32 bit time base wraps after 2^32 / FP; the snapshots are unwrapped."""
        out = [self.snap()]
        base, prev, wrap = out[0][1], out[0][1], 0
        while True:
            time.sleep(1.0)
            s = self.snap()
            if s[1] < prev:
                wrap += 1 << 32
            prev = s[1]
            out.append((s[0], s[1] + wrap, s[2]))
            if (s[1] + wrap - base) / self.fp >= secs:
                return out

    def rate(self, snaps):
        """(idle passes per FP cycle, target seconds, idle passes per target second, Stopwatch cycles)"""
        di = (snaps[-1][0] - snaps[0][0]) & 0xFFFFFFFF
        dt = snaps[-1][1] - snaps[0][1]
        return di / dt, dt / self.fp, di / (dt / self.fp), snaps[-1][2] - snaps[0][2]

    def channels(self):
        return [(self.sym[n], 'u16') for n in CH_NAMES]

    def close(self):
        self.sc.running = False
        self.ser.close()


def stream(t, period_us, secs, label):
    sc = t.sc
    err = sc.start(t.channels(), 0, period_us)
    assert err is None, err
    snaps = t.window(secs)
    time.sleep(0.5)
    overflows = t.printed('scope_overflows')
    idx, cols = sc.snapshot()
    lost, resyncs = sc.lost, sc.resyncs
    sc.stop()
    stamp = cols[0].astype(np.int64)
    d = np.diff(stamp) & 0xFFFF                 # ticks of FP/4 between two received frames
    nominal = period_us * t.fp / 1e6 / 4
    one = d[np.diff(idx) == 1]                  # neighbours only
    return dict(label=label, frames=len(idx), span=int(idx[-1]), lost=lost, resyncs=resyncs, overflows=overflows,
                nominal=nominal, expected=float(d.sum()) / nominal + 1, tmin=int(one.min()), tmax=int(one.max()),
                tmean=float(one.mean()), snaps=snaps, window=(snaps[-1][1] - snaps[0][1]) / t.fp)


def show_stream(r):
    say(f'  {r["label"]}: frames {r["frames"]} (sample index span {r["span"]}), lost {r["lost"]}, '
        f'resyncs {r["resyncs"]}, scope_overflows {r["overflows"]}')
    say(f'    frames expected from the time stamps {r["expected"]:.1f}; spacing of neighbours min {r["tmin"]} / '
        f'mean {r["tmean"]:.2f} / max {r["tmax"]} ticks of FP/4 (nominal {r["nominal"]:.0f}); '
        f'window {r["window"]:.2f} s target time')


def capture(t, period_us, trig, wall_s):
    sc = t.sc
    kw = dict(tch=0, ttype=1, level=30000, pre_pct=20) if trig else dict(tch=0, ttype=0, level=0, pre_pct=0)
    err = sc.start(t.channels(), 1, period_us, auto=False, **kw)
    if err:
        return dict(period=period_us, error=err)
    end = time.time() + wall_s
    while sc.captures == 0 and time.time() < end:
        time.sleep(0.2)
    if sc.captures == 0:
        return dict(period=period_us, error=f'no capture within {wall_s:.0f} s of wall clock')
    ts, cols = sc.snapshot()
    sc.stop()
    stamp = cols[0].astype(np.int64)
    d = np.diff(stamp) & 0xFFFF
    r = dict(period=period_us, samples=len(ts), nominal=period_us * t.fp / 1e6 / 4, dmin=int(d.min()),
             dmax=int(d.max()), dmean=float(d.mean()), trig=trig)
    if trig:
        i0 = int(np.flatnonzero(ts == 0)[0])
        r.update(pre=i0, trig_vals=(int(stamp[i0 - 1]), int(stamp[i0])), trig_ok=bool(stamp[i0 - 1] < 30000 <= stamp[i0]))
    return r


def show_capture(r):
    if 'error' in r:
        say(f'  capture {r["period"]} us: {r["error"]}')
        return
    s = (f'  capture {r["period"]} us{" with trigger" if r["trig"] else ", no trigger"}: {r["samples"]} samples, '
         f'spacing min {r["dmin"]} / mean {r["dmean"]:.2f} / max {r["dmax"]} ticks of FP/4 (nominal {r["nominal"]:.1f})')
    if r['trig']:
        s += (f', pretrigger {r["pre"]} samples, time stamp {r["trig_vals"][0]} -> {r["trig_vals"][1]} '
              f'through 30000: {"OK" if r["trig_ok"] else "WRONG"}')
    say(s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--elf', required=True)
    ap.add_argument('--mips', type=int, required=True)
    ap.add_argument('--phases', default='ABCDEF')
    ap.add_argument('--secs', type=float, default=20, help='target seconds of the stream in phase C')
    ap.add_argument('--win', type=float, default=2, help='target seconds of the windows A, B, D, E')
    ap.add_argument('--periods', default='100t,100,50t,20t,10t,5t,2t,1t,1tn',
                    help='phase F: period in us, t = with trigger, n = control load off')
    a = ap.parse_args()
    t = Target(a.elf, a.mips)
    say(f'FP {t.fp / 1e6:.0f} MHz, control ISR burn {t.printed("load_burn")} cycles per pass of {t.fp / 20000:.0f}')
    res = {}
    try:
        if not t.sc.ping():
            raise SystemExit('no ping')
        say(f'ping ok, capture buffer {t.sc.capbuf} bytes, max {t.sc.max_ch} channels')
        if 'A' in a.phases:
            t.load('off')
            res['A'] = t.rate(t.window(a.win))
            say(f'A load off, mini-scope idle:  idle/cycle {res["A"][0]:.6f}, idle/s {res["A"][2]:.0f}, '
                f'{res["A"][1]:.3f} s target, Stopwatch {res["A"][3]} cycles')
        if 'a' in a.phases:
            t.load('kick')
            res['a'] = t.rate(t.window(a.win))
            say(f'a kick only, mini-scope idle:  idle/cycle {res["a"][0]:.6f}, idle/s {res["a"][2]:.0f}, '
                f'{res["a"][1]:.3f} s target, Stopwatch {res["a"][3]} cycles')
        if 'B' in a.phases:
            t.load('on')
            res['B'] = t.rate(t.window(a.win))
            say(f'B load on, mini-scope idle:   idle/cycle {res["B"][0]:.6f}, idle/s {res["B"][2]:.0f}, '
                f'{res["B"][1]:.3f} s target, Stopwatch {res["B"][3]} cycles')
            say(f'  isr_len, last pass (FP ticks between the first and the last read of the time base): {t.printed("isr_len")}')
            if 'A' in res:
                share = 1 - res['B'][0] / res['A'][0]
                say(f'  control ISR share {100 * share:.2f} % of the cycles = {share * t.fp / 20000:.1f} cycles of {t.fp / 20000:.0f} per pass')
        if 'C' in a.phases:
            t.load('on')
            r = stream(t, 1000, a.secs, 'C load on, stream 1 ms, 4 channels')
            show_stream(r)
            res['C'] = t.rate(r['snaps'])
            say(f'C load on, stream 1 ms:       idle/cycle {res["C"][0]:.6f}, idle/s {res["C"][2]:.0f}, '
                f'{res["C"][1]:.3f} s target, Stopwatch {res["C"][3]} cycles')
            for s0, s1 in zip(r['snaps'], r['snaps'][1:]):
                say(f'    interval {(s1[1] - s0[1]) / t.fp:6.2f} s: idle/cycle '
                    f'{((s1[0] - s0[0]) & 0xFFFFFFFF) / (s1[1] - s0[1]):.6f}')
        if 'D' in a.phases:
            t.load('kick')
            r = stream(t, 1000, 2 * a.win, 'D load off, stream 1 ms, 4 channels')
            show_stream(r)
            res['D'] = t.rate(r['snaps'])
            say(f'D load off, stream 1 ms:      idle/cycle {res["D"][0]:.6f}, idle/s {res["D"][2]:.0f}, '
                f'{res["D"][1]:.3f} s target, Stopwatch {res["D"][3]} cycles')
        if 'E' in a.phases:
            t.load('on')
            show_stream(stream(t, 500, a.win, 'E load on, stream 500 us, 4 channels'))
        if 'F' in a.phases:
            for item in a.periods.split(','):
                per, trig, load = int(item.rstrip('tn')), 't' in item, 'n' not in item
                t.load('on' if load else 'off')
                say(f'  control load {"on" if load else "off"}')
                r = capture(t, per, trig, 100)
                show_capture(r)
                if 'error' in r:
                    say('  recovering with a device reset through MDB')
                    t.sc.running = False
                    t.ser.reset_target()
                    time.sleep(45)
                    t.load('on')
                    t.sc.ping()
        if 'A' in res:
            say('Use of the cycles, idle/cycle lost against A (load off, mini-scope idle):')
            for k in 'aBCD':
                if k in res:
                    say(f'  {k}: {100 * (1 - res[k][0] / res["A"][0]):.2f} %')
            if 'B' in res and 'C' in res:
                say(f'  mini-scope stream with the load on: {100 * (1 - res["C"][0] / res["B"][0]):.2f} % of the cycles left by the load (C against B)')
            if 'D' in res and 'a' in res:
                say(f'  mini-scope stream without the load: {100 * (1 - res["D"][0] / res["a"][0]):.2f} % of the cycles (D against a)')
    finally:
        t.close()


if __name__ == '__main__':
    main()
