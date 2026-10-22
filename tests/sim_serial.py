"""sim_serial.py - serial port replacement that runs the real scope.c (sim.dll).

Time runs with the wall clock: every read() advances the simulated target by
the elapsed time, so the GUI sees realistic data rates.
"""
import ctypes
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SYMBOLS = {'sine': (0x1000, 2), 'ramp': (0x1002, 2), 'counter': (0x1004, 4), 'toggle': (0x1008, 1)}


class SimSerial:
    def __init__(self, dll=os.path.join(HERE, 'sim.dll'), timeout=0.1):
        self.lib = ctypes.CDLL(dll)
        self.lib.sim_period_us.restype = ctypes.c_double
        self.lib.sim_init()
        self.timeout = timeout
        self.buf = bytearray()
        self.t_last = time.perf_counter()
        self.cbuf = ctypes.create_string_buffer(1 << 20)

    def _advance(self):
        now = time.perf_counter()
        per = self.lib.sim_period_us()
        ticks = int((now - self.t_last) * 1e6 / per) if per > 0 else 0
        if per > 0 and ticks == 0:
            return
        self.t_last = self.t_last + ticks * per * 1e-6 if per > 0 else now    # keep the started tick
        n = self.lib.sim_run(min(ticks, 200000), self.cbuf, len(self.cbuf))
        self.buf += self.cbuf.raw[:n]

    def write(self, data):
        self._advance()
        b = bytes(data)
        self.lib.sim_rx(b, len(b))
        return len(b)

    def read(self, n=1):
        end = time.perf_counter() + self.timeout
        while len(self.buf) < n and time.perf_counter() < end:
            time.sleep(0.005)
            self._advance()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    @property
    def in_waiting(self):
        self._advance()
        return len(self.buf)

    def reset_input_buffer(self):
        self._advance()
        self.buf.clear()

    def close(self):
        pass
