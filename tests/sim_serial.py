"""sim_serial.py - serial port replacement that runs the real scope.c (sim.dll).

Time runs with the wall clock: every read() advances the simulated target by
the elapsed time, so the GUI sees realistic data rates. The DLL keeps the virtual
time: the virtual motor steps every 100 us, also while no sample timer runs.

Two test modes, set by environment variables, off by default (tests/test_gui.py):
  MS_SIM_WRITELOG=file   every write() is appended to the file as "seconds hex"
  MS_SIM_LOST_S=n        n seconds after the first stream configuration ('C') every call raises OSError
"""
import ctypes
import os
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))


class Symbols(dict):
    """The name table of sim.dll. `strings` are the const texts of the firmware, as VarTable.read_string() gives
    them for an ELF: here the text tune_build of sim.dll, which tune_block.h defines."""

    @property
    def strings(self):
        return {'tune_build': build_text()}


def build_text(dll=os.path.join(HERE, 'sim.dll')):
    lib = ctypes.CDLL(dll)
    lib.sim_build.restype = ctypes.c_char_p
    return lib.sim_build().decode('latin-1')


SYMBOLS = Symbols({'sine': (0x1000, 2), 'ramp': (0x1002, 2), 'counter': (0x1004, 4), 'toggle': (0x1008, 1),
           'sine_gain': (0x100A, 2), 'ramp_step': (0x100C, 2),
           # virtual motor, struct layout of example_main.c (all 16 bit)
           'motor.run': (0x1100, 2), 'motor.state': (0x1102, 2), 'motor.speed': (0x1104, 2),
           'motor.speed_ref': (0x1106, 2), 'motor.iq': (0x1108, 2), 'motor.fault': (0x110A, 2),
           'motor.kp': (0x110C, 2), 'motor.ki': (0x110E, 2), 'motor.ref_offset': (0x1110, 2),
           'motor.current_ref': (0x1112, 2), 'motor.current': (0x1114, 2),
           'motor.current_kp': (0x1116, 2), 'motor.current_ki': (0x1118, 2),
           'startup.ramp_rate': (0x1120, 2), 'startup.align_current': (0x1122, 2),
           'startup.handover_speed': (0x1124, 2), 'tune_alive': (0x1128, 2),
           'tune_build_id': (0x112A, 2)})
# C types of SYMBOLS for elf_vars.VarTable.from_symbols (the GUI unpacks SYMBOLS as (address, size) pairs)
SYMBOL_TYPES = {'sine': 's16', 'ramp': 'u16', 'counter': 's32', 'toggle': 'u8', 'sine_gain': 's16', 'ramp_step': 'u16',
                'motor.run': 'u16', 'motor.state': 'u16', 'motor.speed': 's16', 'motor.speed_ref': 's16', 'motor.iq': 's16',
                'motor.fault': 'u16', 'motor.kp': 's16', 'motor.ki': 's16', 'motor.ref_offset': 's16',
                'motor.current_ref': 's16', 'motor.current': 's16', 'motor.current_kp': 's16', 'motor.current_ki': 's16',
                'startup.ramp_rate': 'u16', 'startup.align_current': 'u16', 'startup.handover_speed': 'u16',
                'tune_alive': 'u16', 'tune_build_id': 'u16'}


class SimSerial:
    time_scale = 1              # wall clock seconds per second of target time
    output_lag_ms = 0           # target time until a written byte shows up in the stream

    def __init__(self, dll=os.path.join(HERE, 'sim.dll'), timeout=0.1):
        self.lib = ctypes.CDLL(dll)
        self.lib.sim_period_us.restype = ctypes.c_double
        self.lib.sim_run_us.argtypes = [ctypes.c_double, ctypes.c_char_p, ctypes.c_int]
        self.lib.sim_init()
        self.timeout = timeout
        self.buf = bytearray()
        self.lock = threading.RLock()   # reader and writer threads both enter the DLL and touch buf
        self.t_last = time.perf_counter()
        self.cbuf = ctypes.create_string_buffer(1 << 20)
        self.writelog = os.environ.get('MS_SIM_WRITELOG')
        self.lost_s = float(os.environ.get('MS_SIM_LOST_S') or 0)
        self.lost_at = None

    def _lost(self):
        if self.lost_at is not None and time.monotonic() >= self.lost_at:
            raise OSError('simulated loss of the port')

    def _advance(self):
        with self.lock:
            now = time.perf_counter()
            us = min((now - self.t_last) * 1e6, 2e6)
            self.t_last = now
            n = self.lib.sim_run_us(ctypes.c_double(us), self.cbuf, len(self.cbuf))
            # only n bytes: cbuf.raw copies all 1 MB with the interpreter held, 0.4 ms each time. The reader then
            # always finds a byte, never sleeps, and the other threads stood still for the whole of a dump.
            self.buf += ctypes.string_at(self.cbuf, n)

    def write(self, data):
        b = bytes(data)
        self._lost()
        if self.lost_s and self.lost_at is None and b[:1] == b'C':
            self.lost_at = time.monotonic() + self.lost_s
        if self.writelog:
            with open(self.writelog, 'a') as f:
                f.write(f'{time.monotonic():.3f} {b.hex()}\n')
        with self.lock:
            self._advance()
            self.lib.sim_rx(b, len(b))
        return len(b)

    def read(self, n=1):
        self._lost()
        end = time.perf_counter() + self.timeout
        while len(self.buf) < n and time.perf_counter() < end:
            time.sleep(0.005)
            self._advance()
        with self.lock:
            out = bytes(self.buf[:n])
            del self.buf[:n]
        return out

    @property
    def in_waiting(self):
        self._lost()
        with self.lock:
            self._advance()
            return len(self.buf)

    def reset_input_buffer(self):
        with self.lock:
            self._advance()
            self.buf.clear()

    def close(self):
        pass
