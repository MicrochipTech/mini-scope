"""fault_serial.py - SimSerial with faults: what goes wrong between the PC and the target (card M7c).

FaultSerial is a SimSerial (the real scope.c and the virtual motor of sim.dll) that logs what the target
receives and can be made to misbehave while a run goes on. Every injector is a method, the test calls it at the
moment the fault should begin. All are off by default. They act on the link or on the model, never on the PC code.

Link faults (the bytes the target sends, only while a stream or a capture dump runs, and only after the first
`after` = 300 bytes of it, so that the start of a stream is not hit; an injection waits until it can act):
    corrupt(n)              n bursts of 3 zero bytes inserted
    lose_bytes(n)           n single bytes removed
    lose_frames(n, frame)   n whole, frame aligned frames removed (the sequence number jumps)
    only = 'capture'        attribute: damage only the dump of a capture (or only 'stream')
    when_written(addr, value, fn, nth)   fn() runs once, after the target has received the nth write of this value
                            (value None: any value)
    poke(addr, value)       the target gets a write at once (a changed variable that the firmware keeps)
    delay(seconds)          nothing arrives for this long, then everything at once (a stalled USB link)
Port faults:
    go_silent()             nothing comes back any more (silent = True), writes still reach the target
    port_gone(after_s)      from after_s seconds on every call raises OSError (cable pulled)
    gone_at_safe_write()    the port dies with the first write of motor.run = 0 (during the safe end)
    drop_run0(n)            the port swallows the next n messages that write motor.run = 0
    hang / release          Events: while `hang` is set a write blocks until `release` is set
Target faults:
    reset_target()          the target restarts: RAM back to the defaults (sim_init) and idle (command S to the DLL), no
                            stream, as after a power cycle. The DLL has no real reset: this is the nearest model of it.
    stick(addr, value)      the firmware writes value into the 16 bit variable at addr before every 100 us
                            step of the model, so the sample timer always sees it: a stuck fault flag, a
                            variable the firmware overwrites, a motor that does not stop, a reference that
                            never changes. unstick() ends it.

Switching a fault off, to show that a test can fail: MS_FAULT_OFF=name1,name2 (or all) turns into no-ops the
injectors of the cases with these names, or the injectors with these names. Test code that builds a fault into a
script or a table itself asks fault_on(case).
"""
import ctypes
import os
import struct
import threading
import time

from scope_gui import Scope
from sim_serial import SimSerial, SYMBOLS

RUN_ADDR = SYMBOLS['motor.run'][0]
GRID_US = 100.0                 # the model of sim.dll steps every 100 us


def fault_on(name):
    off = os.environ.get('MS_FAULT_OFF', '').split(',')
    return not ('all' in off or name in off)


class FaultSerial(SimSerial):
    def __init__(self, case=None):
        super().__init__()
        self.case = case                # name of the test case: MS_FAULT_OFF=case turns all its injectors off
        self.fault_idx = 0              # len(log) when the first fault was injected
        self.log = []                   # (kind, address, value): kind W for a write, else the command byte
        self.times = []                 # monotonic time of each entry of log
        self.in_stream = False          # between 'R' and the next 'S', 'P' or 'X': only then is the stream damaged
        self.pos = 0                    # stream bytes the target has sent since 'R'
        self.corrupt_n = self.lose_n = self.frames_n = 0
        self.frame = 10
        self.only = None                # 'stream' or 'capture': damage only that kind of transfer (None: both)
        self.kind = None                # kind of the transfer under way
        self.after = 300                # damage only after this many stream bytes: the start of a stream is not a loss
        self.on_write = []              # [address, value, fn, count still to see]
        self.hold_until = 0.0
        self.held = bytearray()
        self.silent = False
        self.gone_at = None
        self.safe_gone = False
        self.stuck = {}
        self.hang = threading.Event()
        self.release = threading.Event()
        self.drop_n = 0
        self.credit = 0.0               # elapsed time not yet given to the model
        self.vt = 0.0                   # virtual time of the model, to keep the steps on its 100 us grid
        self.t_fault = None             # monotonic time of the first injection
        self.max_hold_s = 0.0           # longest time the link held bytes back
        self.hold_since = None

    # -- injectors ---------------------------------------------------------------------------------------

    def _on(self, name):
        return fault_on(name) and (self.case is None or fault_on(self.case))

    def _mark(self):
        if self.t_fault is None:
            self.fault_idx = len(self.log)
            self.t_fault = time.monotonic()

    def _inject(self, what, n, name):
        if self._on(name):
            setattr(self, what, n)
            self._mark()

    def corrupt(self, n, name='corrupt'):
        self._inject('corrupt_n', n, name)

    def lose_bytes(self, n, name='lose_bytes'):
        self._inject('lose_n', n, name)

    def lose_frames(self, n, frame=10, name='lose_frames'):
        self.frame = frame
        self._inject('frames_n', n, name)

    def when_written(self, addr, value, fn, nth=1):
        self.on_write.append([addr, value, fn, nth])

    def delay(self, seconds, name='delay'):
        if self._on(name):
            self.hold_until = time.monotonic() + seconds
            self._mark()

    def port_gone(self, after_s=0.0, name='port_gone'):
        if self._on(name):
            self.gone_at = time.monotonic() + after_s
            self._mark()

    def gone_at_safe_write(self, name='gone_at_safe'):
        if self._on(name):
            self.safe_gone = True

    def poke(self, addr, value, name='poke'):
        if self._on(name):
            cmd, err = Scope._w_cmd(addr, 'u16', value)
            assert err is None, err
            self.write(cmd)
            self._mark()

    def go_silent(self, name='silent'):
        if self._on(name):
            self.silent = True
            self._mark()

    def drop_run0(self, n, name='drop_run0'):
        if self._on(name):
            self.drop_n = n

    def reset_target(self, name='reset'):
        if self._on(name):
            with self.lock:
                self._advance()
                self.lib.sim_init()
                self.lib.sim_rx(b'S', 1)        # sim_init() leaves the static state of scope.c: a reset target is idle
                self.vt = 0.0
                self.buf.clear()
                self.held.clear()
                self.in_stream = False
            self._mark()

    def stick(self, addr, value, name='stick'):
        """16 bit variable."""
        if self._on(name):
            cmd, err = Scope._w_cmd(addr, 'u16' if value >= 0 else 's16', value)
            assert err is None, err
            self.stuck[addr] = cmd
            self._mark()

    def unstick(self):
        self.stuck.clear()

    # -- the port ------------------------------------------------------------------------------------------

    def _bad(self):
        if self.gone_at is not None and time.monotonic() >= self.gone_at:
            raise OSError('simulated loss of the port')

    @staticmethod
    def _messages(b):
        for i in range(0, len(b), 8):
            addr, width = struct.unpack('<HB', b[i + 1:i + 4])
            yield addr, int.from_bytes(b[i + 4:i + 4 + width], 'little', signed=False)

    def _run_zero(self, b):
        return b[:1] == b'W' and any(a == RUN_ADDR and v == 0 for a, v in self._messages(b))

    def write(self, data):
        self._bad()
        b = bytes(data)
        if self.hang.is_set():
            self.release.wait(30)
        if self.safe_gone and self._run_zero(b):
            self.gone_at = time.monotonic()
            self._mark()
            self._bad()
        if self.drop_n and self._run_zero(b):
            self.drop_n -= 1
            return len(b)
        c = b[:1]
        if c == b'C':
            self.kind = 'capture' if b[2] == 1 else 'stream'
        if c == b'R':
            self.in_stream, self.pos = True, 0
        elif c in (b'S', b'P', b'X'):
            self.in_stream = False
        if c == b'W':
            got = list(self._messages(b))
            self.log += [('W', a, v) for a, v in got]
            self.times += [time.monotonic()] * len(got)
        else:
            got = []
            self.log.append((chr(b[0]), None, None))
            self.times.append(time.monotonic())
        n = super().write(b)
        for item in list(self.on_write):
            for a, v in got:
                if a == item[0] and item[1] in (None, v):
                    item[3] -= 1
                    if item[3] == 0:
                        self.on_write.remove(item)
                        item[2]()
        return n

    def read(self, n=1):
        self._bad()
        if self.silent:
            time.sleep(self.timeout)
            return b''
        return super().read(n)

    @property
    def in_waiting(self):
        self._bad()
        return 0 if self.silent else SimSerial.in_waiting.fget(self)

    def reset_input_buffer(self):
        self._bad()
        super().reset_input_buffer()

    def writes(self, addr=None):
        return [x[2] for x in self.log if x[0] == 'W' and (addr is None or x[1] == addr)]

    # -- the link and the model ----------------------------------------------------------------------------

    def _advance(self):
        with self.lock:
            before = len(self.buf)
            self._model_steps()
            new = bytes(self.buf[before:])
            del self.buf[before:]
            if new and self.in_stream:
                new = self._damage(new)
            now = time.monotonic()
            self.held += new
            if self.held and now < self.hold_until and self.hold_since is None:
                self.hold_since = now
            if now >= self.hold_until:
                if self.hold_since is not None:
                    self.max_hold_s = max(self.max_hold_s, now - self.hold_since)
                    self.hold_since = None
                self.buf += self.held
                self.held.clear()

    def _model_steps(self):
        """The model in steps that end on its 100 us grid. The stuck values are written at the start of each step,
        so the sample timer, which runs inside the step, reads them before the model can change them again."""
        now = time.perf_counter()
        self.credit += min((now - self.t_last) * 1e6, 2e6)
        self.t_last = now
        while True:
            step = GRID_US - self.vt % GRID_US
            if not self.stuck:                  # nothing to write between the steps: all at once, like SimSerial
                step = self.credit
            if self.credit < step or step <= 0:
                break
            for cmd in list(self.stuck.values()):
                self.lib.sim_rx(cmd, len(cmd))
            n = self.lib.sim_run_us(ctypes.c_double(step), self.cbuf, len(self.cbuf))
            self.buf += ctypes.string_at(self.cbuf, n)
            self.vt = round(self.vt + step, 6)
            self.credit -= step

    def _damage(self, new):
        start, self.pos = self.pos, self.pos + len(new)         # position of this chunk in the stream of the target
        if self.only not in (None, self.kind):
            return new
        cut = max(0, self.after - start)                        # the first `after` bytes of a stream stay whole
        new, tail, start = new[:cut], new[cut:], start + cut
        if self.corrupt_n and len(tail) > 4:
            m = len(tail) // 2
            tail = tail[:m] + bytes(3) + tail[m:]
            self.corrupt_n -= 1
        if self.lose_n and len(tail) > 4:
            m = len(tail) // 2
            tail = tail[:m] + tail[m + 1:]
            self.lose_n -= 1
        if self.frames_n:
            first = (-start) % self.frame                       # first frame boundary in the chunk
            k = min(self.frames_n, (len(tail) - first) // self.frame)
            if k > 0:
                tail = tail[:first] + tail[first + k * self.frame:]
                self.frames_n -= k
        return new + tail
