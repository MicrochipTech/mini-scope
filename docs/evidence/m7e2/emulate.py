"""emulate.py - tune_speed.py's measuring logic on the bare model (model.dll), with a chosen target-time cost per write.

python docs/evidence/m7e2/emulate.py COST_MS [settle]
COST_MS: target ms every write() spends before its value lands (SIM about 0 to 6, MDB 50 to 150, see run.txt).
settle: wait until the speed holds the base speed before each step (the fix), else the old fixed BACK_MS.
Prints the 20 table rows as kp ki offset overshoot settle. Only the measuring logic is copied, not the API.
"""
import ctypes
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
dll = ctypes.CDLL(os.path.join(HERE, 'model.dll'))
dll.m_motor.restype = ctypes.POINTER(ctypes.c_int16)
dll.m_startup.restype = ctypes.POINTER(ctypes.c_uint16)
mo = dll.m_motor()
F = {'run': 0, 'state': 1, 'speed': 2, 'kp': 6, 'ki': 7, 'ref_offset': 8}
BASE, HOLD = 1500, 100
POLL = int(os.environ.get('POLL', 2))          # POLL_MS of the script
LAND = int(os.environ.get('LAND', 0))          # ms between the start of the measuring and the step in the model
BACK = int(os.environ.get('BACK', 50))         # BACK_MS of the script
START = int(os.environ.get('START', 200))      # START_SETTLE_MS


class Emu:
    def __init__(self, cost, settle):
        dll.m_init()
        self.cost, self.settle, self.t, self.pending = cost, settle, 0, None
        self.v0 = []

    def wait(self, ms):
        for _ in range(ms):
            if self.pending is not None and self.t >= self.pending[0]:
                mo[F['ref_offset']] = self.pending[1]
                self.pending = None
            dll.m_ms()
            self.t += 1

    def write(self, name, v):
        self.wait(self.cost)            # MDB: the wait for the mailbox comes before the value lands
        mo[F[name]] = v

    def read(self, name):
        return mo[F[name]]

    def at_base(self, band, hold, timeout):
        inside, waited = 0, 0
        while inside < hold and waited < timeout:
            inside = inside + POLL if abs(self.read('speed') - BASE) <= band else 0
            self.wait(POLL)
            waited += POLL
        return waited

    def step(self, off):
        size, d = abs(off), 1 if off > 0 else -1
        band = size * 2 // 100
        if self.settle:
            self.at_base(band, BACK, 1000)
        self.v0.append(self.read('speed'))
        peak, last = 0, 0
        self.wait(self.cost)
        self.pending = (self.t + LAND, off)     # the step reaches the model LAND ms after the measuring starts
        for k in range(0, HOLD, POLL):
            b = (self.read('speed') - BASE - off) * d
            peak = max(peak, b)
            if b > band or b < -band:
                last = k
            self.wait(POLL)
        self.write('ref_offset', 0)
        if not self.settle:
            self.wait(BACK)
        return peak * 100 // size, min(last + POLL, HOLD)

    def run(self):
        for n, v in (('kp', 500), ('ki', 30), ('ref_offset', 0), ('run', 1)):
            self.write(n, v)
        while self.read('state') != 3:
            self.wait(POLL)
        self.wait(START)
        out = []
        for kp in (200, 500, 1000, 1500):
            stop = False
            for ki in (60, 150, 300):
                self.write('kp', kp)
                self.write('ki', ki)
                self.wait(BACK)
                r = [self.step(o) for o in (300, -300)]
                out += [(kp, ki, o) + x for o, x in zip((300, -300), r)]
                w = max(x[0] for x in r)
                if w > 20:
                    stop = ki == 60
                    break
            if stop:
                break
        return out


if __name__ == '__main__':
    cost = int(sys.argv[1])
    e = Emu(cost, len(sys.argv) > 2)
    for r in e.run():
        print(*r)
    if os.environ.get('V0'):
        print('speed before each step', e.v0)
