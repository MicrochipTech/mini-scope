"""m0_lag.py - result 4 of card M0: output lag from a write to the first sample showing it.

    python docs/evidence/m0/m0_lag.py --fast 2 -n 10

Streams 4 channels at 1 ms, one of them `probe`. For each round:
  s0 = newest sample in the UART output file just before the write goes in,
  s1 = first sample in the file with the new value of `probe`.
s1 - s0 is the lag in target time (1 sample = 1 ms). It says how many samples a
script must still expect from before its write. The wall clock lag is printed as well.
"""
import argparse
import statistics
import time

from m0_common import Frames, MdbSerial, build, start, symbols, write_cmd
from m0_stream import KICK


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fast', type=int, default=2)
    ap.add_argument('--kick', action='store_true', help='_U1TXIF in the 10 kHz interrupt, see m0_stream.py')
    ap.add_argument('-n', type=int, default=10)
    ap.add_argument('--warm', type=float, default=30, help='seconds before the first round')
    a = ap.parse_args()

    elf = build(f'lag_{a.fast}_{int(a.kick)}', a.fast, load_edits=KICK if a.kick else ())
    sym = symbols(elf)
    chans = ['fast_ticks', 'CCP2TMRL', 'probe', 'fast_cycles'] if a.fast else ['ramp', 'sine', 'probe', 'ramp_step']
    ser = MdbSerial(elf, progress=lambda f, t: None)
    start(ser, [sym[c] for c in chans])
    fr = Frames(ser, 4)
    t0 = time.time()
    while time.time() - t0 < a.warm:
        fr.poll()
        time.sleep(0.2)

    target, wall, send = [], [], []
    for k in range(1, a.n + 1):
        fr.poll()
        s0 = fr.index
        t_in = time.time()
        ser.write(write_cmd(sym['probe'], k))
        t_sent = time.time()
        while time.time() - t_in < 60:
            hit = [r for r in fr.poll() if r[1][2] == k]
            if hit:
                t_out = time.time()
                target.append(hit[0][0] - s0)
                wall.append(t_out - t_sent)
                send.append(t_sent - t_in)
                print(f'{k}: s0 {s0} s1 {hit[0][0]} target {hit[0][0] - s0} ms,'
                      f' wall {t_out - t_sent:.1f} s after {t_sent - t_in:.1f} s for sending', flush=True)
                break
            time.sleep(0.05)
        else:
            print(f'{k}: no sample with the value after 60 s', flush=True)
        time.sleep(2)
    ser._cmd('Halt')
    ser._cmd('Quit')
    ser.p.wait(20)
    if target:
        print(f'target lag ms: min {min(target)} median {statistics.median(target)} max {max(target)} (n={len(target)})')
        print(f'wall lag s: min {min(wall):.1f} median {statistics.median(wall):.1f} max {max(wall):.1f}')
    print(f'lost {fr.lost} of {fr.index}')


if __name__ == '__main__':
    main()
