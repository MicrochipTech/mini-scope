"""tune_api.py - functions for tuning scripts, and the session that runs a script safely.

A script starts with ``from tune_api import *`` and has a ``main()``. The caller (GUI, run_script)
hands in the connected Scope and the VarTable, this module does not import scope_gui:

    s = tune_api.run('tune_startup.py', scope, table, on_event=fn, out_dir='runs', elf_name='x.elf')
    s.wait()                    # or poll s.done; s.result is the same dict as the 'result' event
    tune_api.abort('Abort button')      # from any thread

run() returns at once, the script runs on its own thread. run() raises RunRefused, before anything
reaches the target, if a run is active, the script cannot be read or the run folder cannot be written.

    result = tune_api.probe('tune_startup.py', scope, table, on_event=fn)       # blocks, abort() works
    result['rows']      # the same rows as the 'variables' event

probe() is stage 4 of "Script errors" alone: the same steps open every run, on the script thread before main().
They are described at Session._probe(). The script is executed once for this, the module level once per session.

on_event(ev) gets dicts, in order, on a thread of its own (a stuck receiver cannot hold up an abort):
    {'type': 'state', 'state': 'starting' | 'probe' | 'trial' | 'running' | 'safe_end' | 'done'}
    {'type': 'variables', 'rows': [{'name', 'role', 'address', 'type', 'value'}]}   the probe: role is measured,
                                written, safe, limit, idle (read in check_idle()), joined by ', '; value is raw
    {'type': 'log', 'text': line}
    {'type': 'unit', 'name': name, 'factor': x, 'text': unit}
    {'type': 'channels', 'names': [names], 'period_us': n, 'factors': [x], 'units': [unit]}   the stream of the script has
                                begun (once, after the start was accepted): what a display needs to show it. In capture mode
                                once after the trial capture: the capture channels and the sample period of a capture
    {'type': 'table', 'columns': [names]}
    {'type': 'row', 'values': [...], 'invalid': bool, 'row_traces': [trace indices since the previous row]}
    {'type': 'trace', 'index': n, 'title': text, 'file': path, 'failed': bool, 'invalid': bool,
     'unfinished': bool, 'time_ms': array, 'time_unit': 'ms', 'values': {name: array}}
    {'type': 'result', ...}     status ('ok', 'aborted', 'error'), reason, abort_ms, abort_ms_target,
                                errors, folder, tuning_h, summary_png (path or None), rule_codes, rows, problems,
                                summary_channel, summary_text, summary_mark

Who does what: the script thread only asks. The monitor thread of the session owns the end of a
run: every abort cause sets the abort flag and the monitor runs the safe end (safe values twice, keep
the stream if there is one, stop it, save the open trace, read the safe values back). The script
thread cannot delay this. The gate lock makes a script write and the abort flag exclude each other: a
write checks the flag inside the gate, and the safe end passes through the gate once after setting
the flag.

Nothing goes to the target before the ELF is verified (build_id_check, in the probe or in stream_start()),
except the ping: a run that ends earlier writes no safe values. From that moment every end of the run writes
them, also during the probe and the trial stream. The script itself can only write after the trial.

The script cursor: samples arrive in chunks, so the newest sample can be well ahead of what a script has waited for.
The script therefore sees the stream at its own cursor, counted in sample indices (target ms): wait_ms(n) moves it
by exactly n and returns once that sample has arrived, read() gives the value at the cursor (the nearest sample at or
before it, if one was lost), write() sets it to the newest sample after the write has returned plus the output lag, and
trace_begin()/trace_end() take the cursor as their window. So the same script gets the same steps on every port; a time is
right to one poll step (10 ms) or one chunk of the port, whichever is larger, because the PC knows the time of a write only to one chunk. The monitor, limit(), the heartbeat and the stall check work on the newest sample, not on
the cursor. A script that works for a long time without wait_ms() reads old values until its next write().

Times: during a stream the sample index is the time in target ms, otherwise the wall clock divided
by scope.time_scale. Values are raw integers
everywhere, unit() is display only.
"""
import csv
import ctypes
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time

import numpy as np

from elf_vars import VarError

__all__ = ['channels', 'allow', 'safe_value', 'limit', 'stream_start', 'write', 'read', 'wait_ms',
           'trace_begin', 'trace_end', 'trace_failed', 'table', 'row', 'summary_channel', 'summary_text',
           'summary_mark', 'unit', 'define', 'printf', 'Q15', 'expect',
           'RISING', 'FALLING', 'capture_channels', 'capture_period_us', 'capture_trigger', 'capture_start',
           'capture_arm', 'capture_wait', 'capture_max', 'capture_final', 'capture_rise_us', 'capture_length',
           'capture_value', 'capture_save']

KEEP = 200_000          # stream samples Scope keeps, same as scope_gui.KEEP
MAX_CHANNELS = 4
PERIOD_US = 1000        # one sample per ms
BAUD = 115200           # for a port without a baudrate attribute (SIM, MDB)
MAX_LOAD = 0.90         # of the UART, above this stream_start() refuses
TRIAL_MS = 2000         # trial stream of stream_start()
STALL_MS = 500          # no sample for this long while the stream should run: abort
HEARTBEAT_MS = 200      # tune_alive interval: 1 s timeout in the target, a write can take 70 ms in MDB
SAFE_KEEP_MS = 200      # stream kept after the safe values, the stop of the motor is part of the data
READBACK_GROUP = 4      # safe variables read back per short stream
SEEN_MS = 50            # probe: a written value must show up in the stream within this (plus the output lag)
SETTLE_MS = 50          # probe: target time between the first heartbeat and the first read
HOLD_MS = 100           # probe: a changed variable must hold this long
MAX_IDLE_CALLS = 10_000  # read() calls of one check_idle(): the values do not change, a loop on them never ends
SUMMARY_MAX = 12        # traces and table rows in summary.png
RENDER_S = 30.0         # summary.png is given up after this
SUMMARY_COLORS = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22',
                  '#17becf', '#393b79', '#8c6d31']
PORT_TIMEOUT_S = 5.0    # a port call of the safe end takes at most this (or 2 s of target time)
POLL_S = 0.005          # monitor period
ALIVE = 'tune_alive'
BUILD_ID = 'tune_build_id'      # 16 bit variable of the firmware, CRC of the text below
BUILD_TEXT = 'tune_build'       # const char array in flash: __DATE__ " " __TIME__
CAPTURE_PERIOD_US = 100     # default sample period of a capture
# Floor of the capture period. Below 10 us mini-scope did not hold its period even without load at 32 MIPS,
# and at 1 to 2 us the target hung (needs a reset). The measured limits under load are higher: see SCRIPTING.md.
CAPTURE_MIN_US = 10
CAPTURE_WARN_US = 50        # below this a capture may not hold its period under interrupt load (measured at 32 MIPS)
MAX_CAPTURE_CHANNELS = 4
CAPBUF = 4096               # capture buffer of firmware/scope.h, for the checks that run without a target
TRANSFER_MAX_MS = 1000      # a capture transfer longer than this refuses: nothing is supervised meanwhile
TRIAL_MARGIN_MS = 500       # trial capture: time allowed beyond the window and the transfer
HEARTBEAT_LIMIT_MS = 1000   # the target stops after this long without a change of tune_alive
RISING, FALLING = 1, 2      # edge of capture_trigger(), as ttype of the 'C' command
TYPE_RANGE = {'s8': (-128, 127), 'u8': (0, 255), 's16': (-32768, 32767), 'u16': (0, 65535),
              's32': (-2 ** 31, 2 ** 31 - 1), 'u32': (0, 2 ** 32 - 1)}


class ScriptError(Exception):
    """A refused call or a failed expect(), reported at the line of the script."""


class ExpectFailed(ScriptError):
    """expect() with a false condition."""


class IdleRefused(ScriptError):
    """check_idle() asked for something that touches the target."""


class ProbeFail(Exception):
    """A step of the probe failed: rule code and text."""

    def __init__(self, code, text, line=1):
        super().__init__(text)
        self.code, self.text, self.line = code, text, line


class RunRefused(Exception):
    """run() cannot start. Nothing reached the target."""


class RunAborted(BaseException):
    """Stops the script thread. A BaseException, so that `except Exception` in a script cannot hold it."""


_local = threading.local()      # .session on a script thread; a stale script thread can only reach its own session
_active = None
_active_lock = threading.Lock()


def build_id(text):
    """CRC-16/CCITT-FALSE of a text: start 0xFFFF, polynomial 0x1021, no reflection, as tune_crc16() in
    firmware/tune_block.h. build_id('123456789') is 0x29B1."""
    crc = 0xFFFF
    for ch in text.encode('latin-1'):
        crc ^= ch << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def build_id_check(session):
    """Card 5.1: the firmware on the target is the build the ELF describes. Returns None if tune_build_id
    of the target equals the CRC of the text tune_build in the ELF, else the text of the refusal (rule E402).
    A missing variable, a missing or unreadable text, a plain address table without the text: all refuse, there
    is no run without this check. Reads tune_build_id through a short stream, nothing is written. Called in the
    probe (step 3) and, only when the probe is skipped, from _refusal()."""
    if session.hooks.get('skip_build_id'):          # tests of small scripts only
        return None
    fix = f'program the matching build, or select the ELF that belongs to the programmed one (add firmware/tune_block.h if {BUILD_ID} is missing)'
    try:
        text = session.vars.read_string(BUILD_TEXT)
    except VarError as e:
        return f'the ELF cannot prove its build: {e.message}; {fix}'
    try:
        v = session.var(BUILD_ID)
    except ScriptError as e:
        return f'{e}; {fix}'
    if v.width != 2:
        return f'{BUILD_ID} is {v.width} bytes wide, it must be a 16 bit variable; {fix}'
    try:
        session._s_start([v])
        try:
            _, cols = session._s_wait(3)
        finally:
            session._s_stop()
    except ProbeFail as e:
        return e.text
    got, want = int(cols[0][-1]) & 0xFFFF, build_id(text)
    if got != want:
        return (f'the firmware of the target is not the build of the ELF: {BUILD_ID} is 0x{got:04X} on the target, '
                f'0x{want:04X} from "{text}" in the ELF; {fix}')
    return None


def row_status(invalid, traces):
    """The status of a table row, one rule for results.csv, the summary and the table of the GUI: 'ok', or each of
    failed, invalid, unfinished that holds for one of its traces (dicts with these keys), plus invalid of the row."""
    flags = [k for k in ('failed', 'invalid', 'unfinished') if any(t.get(k) for t in traces) or (k == 'invalid' and invalid)]
    return ', '.join(flags) or 'ok'


def _same(a, b):
    """Two values are the same number, or the same text."""
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _cell(v):
    return f'{v:g}' if isinstance(v, float) else str(v)


class Session:
    """One run of one script. All state of a run lives here, so nothing carries over to the next."""

    def __init__(self, script_path, scope, var_table, on_event, out_dir, elf_name, hooks, probe_only=False, render_png=None):
        self.render_png = render_png        # d -> PNG bytes, handed in by the caller (this module does not import scope_gui)
        self.probe_only = probe_only        # stage 4 alone: the script thread ends after the probe
        self.script_path = os.path.abspath(script_path)
        self.script = os.path.basename(self.script_path)
        self.scope = scope
        self.vars = var_table
        self.on_event = on_event
        self.elf_name = elf_name
        self.hooks = hooks or {}
        self.time_scale = scope.time_scale
        self.port_timeout = max(PORT_TIMEOUT_S, 2.0 * self.time_scale)
        self.t_start = time.monotonic()
        self.ns = {'__name__': '__tune_script__', '__file__': self.script_path}

        # declarations of the script
        self.chans = []             # Var
        self.allowed = {}           # name -> (min, max)
        self.safe = {}              # name -> (Var, value)
        self.limits = {}            # name -> max
        self.units = {}             # name -> (factor, text)
        self.columns = []           # of table()
        self.defines = {}
        self.summary = {'summary_channel': None, 'summary_text': None, 'summary_mark': None}

        # threads and flags
        self.stop = threading.Event()       # the abort flag: no script write after it
        self.gate = threading.Lock()        # held by a script write or stream start while it goes to the port
        self.cause = None                   # (kind, text, monotonic time); the first cause stays
        self.cause_lock = threading.Lock()
        self.cv = threading.Condition()     # new samples or abort
        self.hb_stop = threading.Event()
        self.hb_thread = None
        self.done = threading.Event()
        self.events = queue.Queue()
        self.deliver_thread = None

        # stream
        self.started = False        # stream_start() got past its checks
        self.verified = False       # the probe verified the target: with `started`, every end writes the safe values
        self.short = False          # a short stream of the probe is running
        self.phase = None           # 'idle' while check_idle() runs
        self.armed = False          # the trial stream passed, the script may write
        self.streaming = False
        self.port_ok = True
        self.last_t = 0             # latest sample index
        self.cursor = 0             # the script's position in sample indices, see the module docstring
        self.last_rx = 0.0          # monotonic time of the latest sample
        self.latest = {}            # name -> latest raw value
        self.limit_cols = []        # (column index, name, max)

        # capture mode
        self.mode = None            # 'stream' or 'capture', set by the first channels() or capture_channels()
        self.cap_period = CAPTURE_PERIOD_US
        self.cap_trigger = None     # (Var, edge, level, pre_percent)
        self.capturing = False      # Scope.start was called in capture mode: every end stops it
        self.cap_busy = False       # between the start of a capture and its arrival
        self.cap_state = None       # None, 'armed' (capture_arm() was called) or 'have' (capture_wait() returned 0)
        self.cap = None             # the last capture that arrived: t (sample index, 0 = trigger), cols, pre, period, invalid
        self.cap_saved = True       # self.cap is in a trace file
        self.cap_failed = False     # trace_failed() for the capture
        self.cap_bad = 0            # captures that arrived with another shape than asked for
        self.hb_prev = None         # heartbeat: monotonic time the last write returned, and whether a capture was busy then
        self.hb_prev_busy = False
        self.hb_gap = self.hb_gap_cap = 0.0     # largest gap between two heartbeat writes, in all and around a capture

        # the probe
        self.hb_first = threading.Event()   # the first heartbeat write is out
        self.live = {}              # name -> raw value read by the probe, answers read() in check_idle()
        self.idle_calls = 0
        self.pending = {}           # name -> (Var, original, changed): written by the write test, not yet restored
        self.rows = []              # the 'variables' event
        self.problems = []          # stage 4 problems found
        self.lines = {}             # variable name -> line of its declaration in the script
        self.s_period = PERIOD_US   # of the short stream
        self.s_group = []

        # results
        self.trace = None           # the open trace
        self.trace_lock = threading.Lock()
        self.traces = []
        self.table_rows = []        # (values, status) of row()
        self.last_row_trace = 0     # len(traces) at the last row()
        self.row_quality = (0, 0, 0)
        self.errors = []
        self.rule_codes = {}        # code -> count of the problems of tune_check found before this run
        self.abort_ms = None        # wall clock from the abort cause to the safe values sent
        self.result = None

        self.temp_root = tempfile.mkdtemp(prefix='probe_') if out_dir is None else None     # probe(): no run folder wanted
        try:
            self.folder = self._make_folder(self.temp_root or out_dir)
        except RunRefused:
            if self.temp_root:
                shutil.rmtree(self.temp_root, ignore_errors=True)
            raise

    def _make_folder(self, out_dir):
        if not os.path.isfile(self.script_path):
            raise RunRefused(f'{self.script_path} does not exist')
        stem = os.path.splitext(self.script)[0]
        stamp = time.strftime('%Y%m%d_%H%M%S')
        try:
            for n in range(1, 1000):
                folder = os.path.join(out_dir, f'{stem}_{stamp}' + (f'_{n}' if n > 1 else ''))
                try:
                    os.makedirs(folder)
                    break
                except FileExistsError:
                    pass
            else:
                raise OSError('too many runs in the same second')
            shutil.copy(self.script_path, os.path.join(folder, self.script))
            self.run_file = open(os.path.join(folder, 'run.txt'), 'w', encoding='utf-8', buffering=1)
        except OSError as e:
            raise RunRefused(f'run folder cannot be written: {e}') from None
        return folder

    @staticmethod
    def _throttling_text(ser):
        n = getattr(ser, 'throttling_off', None)
        return '' if n is None else f', power throttling off for {n} processes'

    def start(self):
        ser = self.scope.ser
        self._file(f'script {self.script}')
        self._file(f'time {time.strftime("%Y-%m-%d %H:%M:%S")}')
        self._file(f'port {getattr(ser, "port", None) or type(ser).__name__}, time scale {self.time_scale}, '
                   f'output lag {self.scope.output_lag_ms} ms' + self._throttling_text(ser))
        if getattr(ser, 'throttling_off', None) == 0:
            self._file('warning: power throttling of the simulator is not turned off, it may run up to 4 times slower')
        self._file(f'ELF {self.elf_name}')
        if self.on_event:
            self.deliver_thread = threading.Thread(target=self._deliver, name='tune-events', daemon=True)
            self.deliver_thread.start()
        self.emit('state', state='starting')
        self.script_thread = threading.Thread(target=self._script_main, name='tune-script', daemon=True)
        self.script_thread.start()
        threading.Thread(target=self._monitor, name='tune-monitor', daemon=True).start()

    def wait(self, timeout=None):
        """Waits for the end of the run, returns result (None on a timeout)."""
        self.done.wait(timeout)
        return self.result

    # -- output ---------------------------------------------------------------------------------

    def emit(self, kind, **fields):
        if self.on_event:
            self.events.put({'type': kind, **fields})

    def _deliver(self):
        while True:
            ev = self.events.get()
            if ev is None:
                return
            try:
                self.on_event(ev)
            except Exception as e:      # noqa: BLE001 - a bad receiver must not end the run
                self._file(f'warning: on_event: {e}')

    def _file(self, text):
        try:
            self.run_file.write(text + '\n')
        except (OSError, ValueError):   # the run goes on: the safe end does not depend on the disk
            pass

    def log(self, text):
        self._file(text)
        self.emit('log', text=text)

    def fail(self, text):
        self.errors.append(text)
        self.log('error: ' + text)

    def target_ms(self):
        if self.streaming:
            return self.last_t
        return int((time.monotonic() - self.t_start) * 1000 / self.time_scale)

    # -- script thread --------------------------------------------------------------------------

    def _script_main(self):
        _local.session = self
        try:
            try:
                with open(self.script_path, 'rb') as f:
                    code = compile(f.read(), self.script_path, 'exec')
                exec(code, self.ns)     # noqa: S102 - the script is the user's own program
                if self.probe_only or not self.hooks.get('skip_probe'):
                    self._probe()
                if not self.probe_only:
                    main = self.ns.get('main')
                    if not callable(main):
                        raise ScriptError('no main() in the script')
                    main()
            except RunAborted:
                pass
            except SyntaxError as e:
                self.request('error', f'{self.script}:{e.lineno}: error: {e.msg}')
            except BaseException as e:  # noqa: BLE001 - also SystemExit of a script
                self.request('error', self._error_text(e))
            else:
                self.request('end', 'probe ended' if self.probe_only else 'script ended')
        except RunAborted:              # the asynchronous stop came late
            pass

    def _script_line(self, e):
        """The deepest line of the traceback in the script file, 1 if there is none."""
        line, tb = 1, e.__traceback__
        while tb:
            if tb.tb_frame.f_code.co_filename == self.script_path:
                line = tb.tb_lineno
            tb = tb.tb_next
        return line

    def _error_text(self, e):
        """file:line: error: text, with the deepest line of the traceback in the script file."""
        line = self._script_line(e)
        text = str(e) if isinstance(e, ScriptError) else f'{type(e).__name__}: {e}'
        return f'{self.script}:{line}: error: {text}'

    def var(self, name):
        try:
            v = self.vars.lookup(name)
        except VarError as e:
            raise ScriptError(str(e)) from None
        return v if v.type else v._replace(width=2, type='s16')     # a plain address

    def declaring(self, call):
        if self.started:
            raise ScriptError(f'{call}() must come before {"capture_start" if self.mode == "capture" else "stream_start"}()')

    def _mode(self, want, call):
        if self.mode not in (None, want):
            raise ScriptError(f'{call}(): stream calls and capture calls cannot be mixed in one script')
        self.mode = want

    def port_failed(self, e):
        self.port_ok = False
        self.request('port', f'port error: {e}')

    def wait_samples(self, n):
        end = self.last_t + n
        with self.cv:
            while self.last_t < end and not self.stop.is_set():
                self.cv.wait(0.05)      # the monitor ends a stalled stream, the abort flag ends this
        if self.stop.is_set():
            raise RunAborted()

    def wait_cursor(self):
        with self.cv:
            while self.last_t < self.cursor and not self.stop.is_set():
                self.cv.wait(0.05)
        if self.stop.is_set():
            raise RunAborted()

    def wait_target(self, ms):
        if self.phase == 'idle':
            return                      # the values do not change in check_idle()
        if self.armed and self.mode != 'capture':
            self.cursor += max(0, int(ms))
            self.wait_cursor()
        elif self.streaming:
            self.wait_samples(ms)
        else:
            if self.stop.wait(max(0, ms) / 1000 * self.time_scale):
                raise RunAborted()

    def hook(self, stage):
        fn = self.hooks.get('before_send')
        if fn:
            fn(stage)

    def quality(self):
        with self.scope.lock:
            return self.scope.lost, self.scope.resyncs, self.cap_bad

    def _bytes_per_s(self):
        return (getattr(self.scope.ser, 'baudrate', None) or BAUD) / 10     # 8N1: 10 bits per byte

    def _load(self):
        frame = sum(v.width for v in self.chans) + 2    # as scope_gui.stream_load: sync + seq + channels
        return frame * 1e6 / PERIOD_US / self._bytes_per_s()

    def _refusal(self):
        """Why the stream must not start, or None. Nothing has reached the target at this point."""
        if not self.allowed:
            return 'no allow(): a script must declare what it may write'
        if not self.safe:
            return 'no safe_value(): a script must declare what stops the motor'
        if self.mode == 'capture':
            if not self.chans:
                return 'no capture_channels()'
            if len(self.chans) > MAX_CAPTURE_CHANNELS:
                return f'{len(self.chans)} capture channels, at most {MAX_CAPTURE_CHANNELS}'
            if self.cap_trigger is None:
                return 'no capture_trigger()'
            if self.cap_trigger[0].name not in [v.name for v in self.chans]:
                return f'capture_trigger("{self.cap_trigger[0].name}") on a variable that is not in capture_channels()'
        elif not self.chans:
            return 'no channels()'
        elif len(self.chans) > MAX_CHANNELS:
            return f'{len(self.chans)} channels, at most {MAX_CHANNELS}'
        for name, (v, value) in self.safe.items():
            if name not in self.allowed:
                return f'safe_value("{name}") on a variable without allow()'
            lo, hi = self.allowed[name]
            if not lo <= value <= hi:
                return f'safe_value("{name}", {value}) is outside allow() {lo} ... {hi}'
            if v.type != 'f32' and value != int(value):
                return f'safe_value("{name}", {value}) is not a whole number'
        names = [v.name for v in self.chans]
        for name in self.limits:
            if name not in names:
                return f'limit("{name}") on a variable that is not in channels()'
        if self.mode == 'capture':
            ms = self._transfer_ms()
            if ms > TRANSFER_MAX_MS:
                return f'the transfer of a capture takes {ms:.0f} ms at this baud rate, at most {TRANSFER_MAX_MS} ms'
        else:
            load = self._load()
            if load > MAX_LOAD:
                return f'the stream needs {load:.0%} of the UART, at most {MAX_LOAD:.0%}'
        try:
            self.alive = self.var(ALIVE)
        except ScriptError:
            return f'{ALIVE} is not in the ELF, the firmware needs the heartbeat variable'
        if self.verified:               # the probe has done it
            return None
        why = build_id_check(self)      # the probe was skipped
        if why:
            self._problem('E402', 1, why)
        return why

    def stream_start(self):
        if self.phase == 'idle':
            raise IdleRefused('stream_start() in check_idle(): it must only look at the target, not start anything')
        self._mode('stream', 'stream_start')
        if self.started:
            raise ScriptError('stream_start() was already called')
        why = self._refusal()
        if why:
            raise ScriptError('stream_start() refused: ' + why)
        self.started = True
        names = [v.name for v in self.chans]
        self.limit_cols = [(names.index(n), n, m) for n, m in self.limits.items()]
        for k, v in self.ns.items():
            if k.isupper() and isinstance(v, (int, float, str, list, tuple)):
                self._file(f'parameter {k} = {v!r}')
        self._file('channels ' + ', '.join(names))
        self._file(f'allow {self.allowed}, safe { {n: x[1] for n, x in self.safe.items()} }, limit {self.limits}')
        self.emit('state', state='trial')
        with self.gate:
            if self.stop.is_set():
                raise RunAborted()
            try:
                err = self.scope.start([(v.address, v.type) for v in self.chans], 0, PERIOD_US)
            except Exception as e:      # noqa: BLE001 - serial errors have no common base with a mock port
                self.port_failed(e)
                raise RunAborted() from None
            if err:
                raise ScriptError('stream_start(): ' + err)
            self.last_t, self.last_rx = 0, time.monotonic()
            self.streaming = True
        self.emit('channels', names=names, period_us=PERIOD_US,
                  factors=[self.units.get(n, (1, ''))[0] for n in names], units=[self.units.get(n, (1, ''))[1] for n in names])
        # the heartbeat is the only write until the trial has passed; it runs on from the probe, without a gap
        self._start_heartbeat()
        self.wait_samples(TRIAL_MS)
        lost, resyncs, _ = self.quality()
        if lost or resyncs:
            raise ScriptError(f'stream_start() refused: trial stream of {TRIAL_MS} samples has lost {lost}, '
                              f'resyncs {resyncs}')
        with self.gate:
            if self.stop.is_set():
                raise RunAborted()
            self.armed = True
            self.cursor = self.last_t
        self.emit('state', state='running')

    def write(self, name, value):
        if self.phase == 'idle':
            raise IdleRefused(f'write("{name}") in check_idle(): it must only look at the target, not change it')
        v = self.var(name)
        if v.name not in self.allowed:
            raise ScriptError(f'write("{name}"): not allowed, it needs allow("{name}", min, max)')
        lo, hi = self.allowed[v.name]
        if not lo <= value <= hi:
            raise ScriptError(f'write("{name}", {value}): outside allow() {lo} ... {hi}')
        if not self.armed:
            raise ScriptError(f'write() before {"capture_start" if self.mode == "capture" else "stream_start"}()')
        self.hook('pre')
        with self.gate:
            if self.stop.is_set():      # the recheck: the flag may have come while waiting for the gate
                raise RunAborted()
            self.hook('post')
            try:
                err = self.scope.write(v.address, v.type, value)
            except Exception as e:      # noqa: BLE001
                self.port_failed(e)
                raise RunAborted() from None
        if err:
            raise ScriptError(f'write("{name}", {value}): {err}')
        self._drain()                   # after the return: a slow port (mailbox, heartbeat ahead) spends target time
        t = self.target_ms()            # the newest received sample, the time of the write after it returned
        self._file(f'write {t} ms {v.name} = {value}')
        if self.mode != 'capture':      # a capture has no cursor: wait_ms() is the wall clock
            self.cursor = t + self.scope.output_lag_ms
            self.wait_cursor()

    def read(self, name):
        v = self.var(name)
        if self.phase == 'idle':        # answers from the values of the probe, nothing reaches the target
            self.idle_calls += 1
            if self.idle_calls > MAX_IDLE_CALLS:
                raise ScriptError(f'check_idle() called read() more than {MAX_IDLE_CALLS} times: the values do not '
                                  'change here, a loop waiting for one never ends')
            if v.name not in self.live:
                raise ScriptError(f'read("{name}") in check_idle(): the probe has not read this variable '
                                  '(the name must be known before the script runs)')
            return self.live[v.name]
        if self.mode == 'capture':
            raise ScriptError(f'read("{name}"): read() does not work in capture mode, the target sends the buffer '
                              'after the capture. Use capture_value() or capture_final()')
        if v.name not in [c.name for c in self.chans]:
            raise ScriptError(f'read("{name}"): not in channels()')
        if v.name not in self.latest:
            raise ScriptError(f'read("{name}"): no sample yet, read() needs stream_start()')
        t, cols = self.scope.snapshot()
        if not len(t):
            return self.latest[v.name]
        i = max(0, int(np.searchsorted(t, self.cursor, side='right')) - 1)
        k = [c.name for c in self.chans].index(v.name)
        return self._raw(v, cols[k][i])

    # -- traces and tables ----------------------------------------------------------------------

    def trace_begin(self):
        if self.mode == 'capture':
            raise ScriptError('trace_begin() does not work in capture mode, use capture_save()')
        if not self.armed:
            raise ScriptError('trace_begin() before stream_start()')
        if self.trace:
            raise ScriptError('trace_begin() without trace_end() of the last trace')
        self.trace = {'t0': self.cursor, 'quality': self.quality(), 'failed': False}

    def trace_end(self, title):
        if self.mode == 'capture':
            raise ScriptError('trace_end() does not work in capture mode, use capture_save()')
        with self.trace_lock:
            tr, self.trace = self.trace, None
            if tr is None:
                raise ScriptError('trace_end() without trace_begin()')
            t, cols = self.scope.snapshot()
            n = self.cursor - tr['t0']
            end = int(np.searchsorted(t, self.cursor, side='right'))
            if n > KEEP:
                raise ScriptError(f'trace of {n} samples is longer than KEEP = {KEEP}')
            self._store_trace(tr, title, t, cols, False, end)

    def _store_trace(self, tr, title, t, cols, unfinished, end=None):
        i = int(np.searchsorted(t, tr['t0'], side='right'))
        end = len(t) if end is None else end
        time_ms = t[i:end] - tr['t0']
        values = {v.name: c[i:end] for v, c in zip(self.chans, cols)}
        self._add_trace(tr, title, time_ms, 'time_ms', time_ms.astype(np.int64).tolist(), values, unfinished,
                        self.quality() != tr['quality'])

    def _add_trace(self, tr, title, time_ms, time_name, time_column, values, unfinished, invalid):
        """Writes trace_NNN.csv and enters the trace. time_ms is what the events and summary.png show."""
        index = len(self.traces) + 1
        path = os.path.join(self.folder, f'trace_{index:03d}.csv')
        phys = [v.name for v in self.chans if v.name in self.units]
        columns = [time_column]
        for v in self.chans:
            columns.append(values[v.name].tolist() if v.type == 'f32' else values[v.name].astype(np.int64).tolist())
        for n in phys:
            columns.append([f'{x:.6g}' for x in (values[n] * self.units[n][0]).tolist()])
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow([time_name] + list(values) + [f'{n} [{self.units[n][1]}]' for n in phys])
            w.writerows(zip(*columns))
        tr = {'index': index, 'title': title, 'file': path, 'failed': tr['failed'],
              'invalid': invalid, 'unfinished': unfinished}
        self.traces.append({**tr, 'time_ms': time_ms, 'values': values})
        status = [k for k in ('failed', 'invalid', 'unfinished') if tr[k]]
        self._file(f'trace {index:03d} "{title}": {len(time_ms)} samples, {" ".join(status) or "ok"}')
        self.emit('trace', **tr, time_ms=time_ms, time_unit='ms', values=values)

    @staticmethod
    def _raw(v, x):
        return float(x) if v.type == 'f32' else int(x)

    def table(self, names):
        if self.columns:
            raise ScriptError('table() was already called, a run has one table')
        if not names:
            raise ScriptError('table() needs column names')
        self.columns = list(names)
        with open(os.path.join(self.folder, 'results.csv'), 'w', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(self.columns + ['status'])
        self.emit('table', columns=self.columns)

    def row(self, values):
        if not self.columns:
            raise ScriptError('row() without table()')
        if len(values) != len(self.columns):
            raise ScriptError(f'row() has {len(values)} values, table() has {len(self.columns)} columns')
        q = self.quality()
        invalid = q != self.row_quality
        self.row_quality = q
        mine = list(range(self.last_row_trace + 1, len(self.traces) + 1))
        status = row_status(invalid, self.traces[self.last_row_trace:])
        self.last_row_trace = len(self.traces)
        self.table_rows.append((list(values), status))
        with open(os.path.join(self.folder, 'results.csv'), 'a', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(list(values) + [status])
        self.emit('row', values=list(values), invalid=invalid, row_traces=mine)

    # -- capture mode ---------------------------------------------------------------------------
    #
    # One capture is one Scope.start() with auto off: the target fills its buffer, sends it, and is idle again.
    # There is no stream, so no cursor, no read(), and no stall check: capture_wait() has a timeout on the wall
    # clock (scaled), the heartbeat runs on the wall clock, limit() is checked on the buffer after the transfer.

    def capture_channels(self, names):
        self.declaring('capture_channels')
        self._mode('capture', 'capture_channels')
        if len(names) > MAX_CAPTURE_CHANNELS:
            raise ScriptError(f'capture_channels(): {len(names)} channels, at most {MAX_CAPTURE_CHANNELS}')
        self.chans = [self.var(n) for n in names]

    def capture_period_us(self, us):
        self.declaring('capture_period_us')
        self._mode('capture', 'capture_period_us')
        if us != int(us) or not 1 <= us <= 65535:
            raise ScriptError(f'capture_period_us({us}): a whole number of microseconds from 1 to 65535')
        if us < CAPTURE_MIN_US:
            raise ScriptError(f'capture_period_us({us}): below {CAPTURE_MIN_US} us mini-scope does not hold its period '
                              f'(at 1 to 2 us the target hangs). Use {CAPTURE_MIN_US} us or more, see SCRIPTING.md section 8')
        self.cap_period = int(us)

    def capture_trigger(self, name, edge, level, pre_percent):
        self.declaring('capture_trigger')
        self._mode('capture', 'capture_trigger')
        v = self.var(name)
        if v.type == 'f32':
            raise ScriptError(f'capture_trigger("{name}"): {v.name} is a float, the target compares integers')
        if edge not in (RISING, FALLING):
            raise ScriptError(f'capture_trigger("{name}"): the edge must be RISING or FALLING')
        if level != int(level) or (v.type in TYPE_RANGE and not TYPE_RANGE[v.type][0] <= level <= TYPE_RANGE[v.type][1]):
            raise ScriptError(f'capture_trigger("{name}", {level}): the level must be a whole number inside the range of {v.type}')
        if not 0 <= pre_percent <= 100:
            raise ScriptError(f'capture_trigger("{name}"): the pretrigger is 0 to 100 percent, not {pre_percent}')
        self.cap_trigger = (v, edge, int(level), pre_percent)

    def _record_bytes(self):
        return sum(v.width for v in self.chans)

    def _depth(self):
        return (self.scope.capbuf or CAPBUF) // self._record_bytes()

    def _pre(self):
        """Pretrigger samples as the target holds them: the PC sends the count, the firmware limits it to depth - 1."""
        pct = self.cap_trigger[3]
        return min(int(self._depth() * pct / 100), self._depth() - 1)

    def _transfer_ms(self):
        return ((self.scope.capbuf or CAPBUF) + 5) / self._bytes_per_s() * 1000     # buffer, sync and two 16 bit counts

    def _cap_timeout_s(self):
        """Wall clock seconds that a capture may take from its start to its arrival."""
        ms = self._depth() * self.cap_period / 1000 + self._transfer_ms() + TRIAL_MARGIN_MS + self.scope.output_lag_ms
        return ms / 1000 * self.time_scale

    def capture_start(self):
        if self.phase == 'idle':
            raise IdleRefused('capture_start() in check_idle(): it must only look at the target, not start anything')
        self._mode('capture', 'capture_start')
        if self.started:
            raise ScriptError('capture_start() was already called')
        why = self._refusal()
        if why:
            raise ScriptError('capture_start() refused: ' + why)
        self.started = True
        names = [v.name for v in self.chans]
        self.limit_cols = [(names.index(n), n, m) for n, m in self.limits.items()]
        for k, v in self.ns.items():
            if k.isupper() and isinstance(v, (int, float, str, list, tuple)):
                self._file(f'parameter {k} = {v!r}')
        trig = self.cap_trigger
        self._file('capture channels ' + ', '.join(names))
        self._file(f'capture period {self.cap_period} us, trigger {trig[0].name} {"rising" if trig[1] == RISING else "falling"} '
                   f'at {trig[2]}, pretrigger {trig[3]} %')
        self._file(f'allow {self.allowed}, safe { {n: x[1] for n, x in self.safe.items()} }, limit {self.limits}')
        self.emit('state', state='trial')
        self._start_heartbeat()
        # the trial: one complete capture without a trigger, nothing written but the heartbeat
        self._cap_begin(True)
        if not self._cap_wait():
            raise ScriptError(f'capture_start() refused: the trial capture did not arrive within '
                              f'{self._cap_timeout_s() / self.time_scale * 1000:.0f} ms')
        t, cols = self.scope.snapshot()
        if len(t) != self._depth() or int(t[0]) != 0:
            raise ScriptError(f'capture_start() refused: the trial capture has {len(t)} samples starting at {int(t[0]) if len(t) else 0}, '
                              f'expected {self._depth()} from 0')
        self.cap_busy = False
        self._cap_limits(t, cols)
        with self.gate:
            if self.stop.is_set():
                raise RunAborted()
            self.armed = True
        # a display names the channels of the traces with it (the overlay of the results), nothing streams in capture mode
        self.emit('channels', names=names, period_us=self.cap_period,
                  factors=[self.units.get(n, (1, ''))[0] for n in names], units=[self.units.get(n, (1, ''))[1] for n in names])
        self.emit('state', state='running')

    def _cap_begin(self, trial):
        """Scope.start in capture mode, one capture. The trial has no trigger and no pretrigger."""
        names = [v.name for v in self.chans]
        chans = [(v.address, v.type) for v in self.chans]
        if trial:
            args = (0, 0, 0, 0)
        else:
            v, edge, level, pct = self.cap_trigger
            args = (names.index(v.name), edge, level, pct)
        with self.gate:
            if self.stop.is_set():
                raise RunAborted()
            self.capturing = self.cap_busy = True
            try:
                err = self.scope.start(chans, 1, self.cap_period, *args, auto=False)
            except Exception as e:      # noqa: BLE001
                self.port_failed(e)
                raise RunAborted() from None
        if err:
            raise ScriptError(('capture_start()' if trial else 'capture_arm()') + ': ' + err)

    def _cap_wait(self, timeout_s=None):
        """True when the capture has arrived, False after timeout_s of wall clock (default: the time of a whole
        capture). Until the first byte of the dump the timeout counts from the call. Once the dump is under way
        (Scope.dump_bytes), it counts only while no new byte comes: a slow transfer is never timed out, one that
        stops is. The abort flag ends it, so does a port that failed."""
        span = self._cap_timeout_s() if timeout_s is None else timeout_s
        end = time.monotonic() + span
        seen = 0
        while True:
            if self.stop.is_set():
                raise RunAborted()
            if self.scope.captures:
                return True
            n = self.scope.dump_bytes
            if n != seen:
                seen, end = n, time.monotonic() + span
            th = self.scope.thread
            if th is None or not th.is_alive():
                if self.scope.captures:     # it arrived just before the reader ended
                    return True
                self.port_ok = False
                self.request('port', 'port error: the capture reader ended')
                raise RunAborted()
            if time.monotonic() >= end:
                return False
            self.stop.wait(POLL_S)

    def _cap_limits(self, t, cols):
        """limit() on the whole buffer, after the transfer. Aborts the run, the capture is kept as unfinished."""
        for k, name, most in self.limit_cols:
            c = cols[k]
            bad = np.flatnonzero(~(np.abs(c) <= most))          # also catches NaN of a float
            if len(bad):
                us = int(t[bad[0]]) * self.cap_period
                self.request('limit', f'limit exceeded: {name} = {c[bad[0]]:.0f} at {us} us from the trigger '
                                      f'in the capture, limit {most}')
                raise RunAborted()

    def capture_arm(self):
        if self.phase == 'idle':
            raise IdleRefused('capture_arm() in check_idle(): it must only look at the target, not start anything')
        if not self.armed or self.mode != 'capture':
            raise ScriptError('capture_arm() before capture_start()')
        if self.cap_state == 'armed':
            raise ScriptError('capture_arm() twice: capture_wait() is missing')
        self.cap, self.cap_saved, self.cap_failed, self.cap_state = None, True, False, 'armed'
        self._cap_begin(False)
        # Scope cannot tell when the pretrigger buffer is full: pretrigger samples x period, plus the output lag
        self.wait_target(self._pre() * self.cap_period / 1000 + self.scope.output_lag_ms)

    def capture_wait(self, timeout_ms):
        if self.cap_state != 'armed':
            raise ScriptError('capture_wait() without capture_arm()')
        if not self._cap_wait(max(0, timeout_ms) / 1000 * self.time_scale):
            self.cap_state, self.cap_busy = None, False
            return -1
        t, cols = self.scope.snapshot()
        self.cap_busy = False
        pre, depth = self._pre(), self._depth()
        bad = len(t) != depth or int(t[0]) != -pre
        if bad:
            self.cap_bad += 1
            self.log(f'warning: the capture has {len(t)} samples starting at {int(t[0]) if len(t) else 0}, '
                     f'expected {depth} starting at {-pre}: marked invalid')
        self.cap = {'t': t, 'cols': cols, 'pre': pre, 'period': self.cap_period, 'invalid': bad}
        self.cap_saved, self.cap_state = False, 'have'
        self._cap_limits(t, cols)
        return 0

    def _cap_col(self, call, name):
        if self.cap is None:
            raise ScriptError(f'{call}(): no capture, capture_wait() has to return 0 first')
        v = self.var(name)
        names = [c.name for c in self.chans]
        if v.name not in names:
            raise ScriptError(f'{call}("{name}"): not in capture_channels()')
        return v, self.cap['cols'][names.index(v.name)]

    def capture_max(self, name):
        v, c = self._cap_col('capture_max', name)
        return self._raw(v, c.max())

    def capture_final(self, name):
        """Mean of the last 10 % of the buffer, truncated towards 0 like C integer division."""
        v, c = self._cap_col('capture_final', name)
        tail = c[-max(1, len(c) // 10):]
        if v.type == 'f32':
            return float(tail.mean())
        return int(int(tail.astype(np.int64).sum()) / len(tail))

    def capture_rise_us(self, name):
        """Time from 10 % to 90 % of the step, to the sample. The step goes from the mean before the trigger (the
        first sample without a pretrigger) to capture_final(). -1 if there is no step or a level is not reached."""
        _, c = self._cap_col('capture_rise_us', name)
        c = c.astype(np.float64)
        pre = self.cap['pre']
        base = c[:pre].mean() if pre else c[0]
        step = c[-max(1, len(c) // 10):].mean() - base
        if step == 0:
            return -1
        up = 1.0 if step > 0 else -1.0
        lo, hi = base + 0.1 * step, base + 0.9 * step
        at10 = np.flatnonzero(up * (c[pre:] - lo) >= 0)
        if not len(at10):
            return -1
        i10 = pre + int(at10[0])
        at90 = np.flatnonzero(up * (c[i10:] - hi) >= 0)
        if not len(at90):
            return -1
        return int(at90[0]) * self.cap['period']

    def capture_length(self):
        if self.cap is None:
            raise ScriptError('capture_length(): no capture, capture_wait() has to return 0 first')
        return len(self.cap['t'])

    def capture_value(self, name, i):
        v, c = self._cap_col('capture_value', name)
        if i != int(i) or not 0 <= i < len(c):
            raise ScriptError(f'capture_value("{name}", {i}): the index must be a whole number from 0 to {len(c) - 1}')
        return self._raw(v, c[int(i)])

    def capture_save(self, title):
        if self.cap is None:
            raise ScriptError('capture_save(): no capture, capture_wait() has to return 0 first')
        with self.trace_lock:
            self._store_capture(title, False)

    def _store_capture(self, title, unfinished):
        """The capture as a trace. Time zero is the trigger. The CSV has microseconds below a period of 1 ms, else
        milliseconds; the event and summary.png always milliseconds. Caller holds trace_lock."""
        cap = self.cap
        us = cap['t'].astype(np.int64) * cap['period']
        values = {v.name: c for v, c in zip(self.chans, cap['cols'])}
        if cap['period'] < 1000:
            name, column = 'time_us', us.tolist()
        else:
            name, column = 'time_ms', [f'{x / 1000:g}' for x in us.tolist()]
        self.cap_saved = True
        tr = {'failed': self.cap_failed}
        self._add_trace(tr, title, us / 1000.0, name, column, values, unfinished, cap['invalid'])

    # -- the target probe: stage 4 of "Script errors" -----------------------------------------------

    def _problem(self, code, line, text):
        """A problem of stage 4: counted by code, in the log and run.txt as file:line: severity: text [code]."""
        import tune_check       # here, not at the top: tune_check imports this module
        sev, stage, _ = tune_check.RULES[code]
        p = tune_check.Problem(code, sev, self.script, line, text, stage)
        self.problems.append(p)
        self.rule_codes[code] = self.rule_codes.get(code, 0) + 1
        if sev == 'error':
            self.errors.append(str(p))
        self.log(f'{p} [{code}]')
        return p

    def _line(self, name):
        return self.lines.get(name, 1)

    def _probe(self):
        """Stage 4, on the script thread, before main(). The script body has run once (module level), the
        declarations come from tune_check.record() because main() has not run. Steps:
          1 stages 1 to 3 have no errors
          2 ping
          3 build_id_check: from here on the target is verified, every end writes the safe values
          4 heartbeat on (it runs on into the stream of the run), 50 ms target time of rest
          5 live values of all variables the script names, short streams of up to 4, the 'variables' event
          6 check_idle() on those values, nothing else touches the target
          7 write test, only if check_idle() passed
        limit() is only checked on what is streamed: the short streams of steps 5 and 7 carry up to 4 variables,
        in step 7 the written one and up to 3 with limit(). The rest is not supervised during the probe."""
        import tune_check
        self.emit('state', state='probe')
        rec = tune_check.record(self.script_path, self.vars, baud=getattr(self.scope.ser, 'baudrate', None))
        if rec is None or any(p.severity == 'error' for p in rec.problems):
            raise ScriptError('the probe needs a script without errors in stages 1 to 3: press Check')
        if not callable(self.ns.get('check_idle')):
            raise ScriptError('no check_idle() in the script')
        self._take_declarations(rec)
        try:
            self._probe_steps(rec)
        except ProbeFail as e:
            p = self._problem(e.code, e.line, e.text)
            self.request('error', str(p))
            raise RunAborted() from None
        first = next((p for p in self.problems if p.severity == 'error'), None)
        if first:                       # the write test goes on after an error, to find all of them
            self.request('error', str(first))
            raise RunAborted()
        skipped = sum(1 for x in self.problems if x.code in ('W401', 'W402'))
        self.log(f'probe passed: {len(self.rows)} variables read, {skipped} write test(s) skipped')

    def _take_declarations(self, rec):
        d = rec.declared
        if d['chans']:
            self.chans = list(d['chans'])
        self.allowed.update({n: (lo, hi) for n, (lo, hi, _) in d['allowed'].items()})
        self.safe.update({n: (v, value) for n, (v, value, _) in d['safe'].items()})
        self.limits.update({n: most for n, (most, _) in d['limits'].items()})
        for v in self.chans:
            self.lines[v.name] = d['chans_line']
        for n, (_, line) in d['limits'].items():
            self.lines[n] = line
        for n, (_, _, line) in d['safe'].items():
            self.lines[n] = line
        for n, (_, _, line) in d['allowed'].items():
            self.lines[n] = line
        for line, name, args in rec.calls:
            if name == 'read' and args and isinstance(args[0], str):
                try:
                    self.lines.setdefault(self.var(args[0]).name, line)
                except ScriptError:
                    pass

    def _gated(self, fn, *args):
        """A port call of the script thread, inside the gate: not after the abort flag, a port error is an abort."""
        with self.gate:
            if self.stop.is_set():
                raise RunAborted()
            try:
                return fn(*args)
            except Exception as e:      # noqa: BLE001
                self.port_failed(e)
                raise RunAborted() from None

    def _probe_steps(self, rec):
        self.alive = self.var(ALIVE)    # a script error if the ELF has no heartbeat variable
        if not self._gated(self.scope.ping):
            raise ProbeFail('E401', 'the target does not answer the ping: check the connection and the firmware, '
                            'see TROUBLESHOOTING.md section 4')
        why = build_id_check(self)
        if why:
            raise ProbeFail('E402', why)
        self.verified = True
        self.log('target verified: every end of this run writes the safe values')
        self._start_heartbeat()
        end = time.monotonic() + self.port_timeout
        while not self.hb_first.wait(0.02):
            if self.stop.is_set():
                raise RunAborted()
            if time.monotonic() > end:
                raise ProbeFail('E404', f'the heartbeat {ALIVE} was not written within {self.port_timeout:g} s')
        self.wait_target(SETTLE_MS)     # the target sees the first heartbeat and clears a fault of the last run
        self._probe_values(rec)
        self._probe_idle()
        self._write_test()

    # -- short streams of the probe: the script thread only, one at a time

    def _group_size(self):
        return max(1, min(READBACK_GROUP, self.scope.max_ch or READBACK_GROUP))

    def _s_n(self, ms):
        """Samples of the short stream in ms of target time."""
        return int(np.ceil(ms * 1000 / self.s_period)) if ms > 0 else 0

    def _s_start(self, group):
        frame = sum(v.width for v in group) + 2
        self.s_period = max(PERIOD_US, int(np.ceil(frame * 1e6 / (MAX_LOAD * self._bytes_per_s()))))
        self.s_group = group
        with self.gate:
            if self.stop.is_set():
                raise RunAborted()
            try:
                err = self.scope.start([(v.address, v.type) for v in group], 0, self.s_period)
            except Exception as e:      # noqa: BLE001
                self.port_failed(e)
                raise RunAborted() from None
            self.short = not err
        if err:
            raise ProbeFail('E403', f'{", ".join(v.name for v in group)} cannot be streamed: {err}', self._line(group[0].name))

    def _s_stop(self):
        with self.gate:
            if self.stop.is_set():
                raise RunAborted()      # the safe end stops it
            try:
                self.scope.stop()
            except Exception as e:      # noqa: BLE001
                self.port_failed(e)
                raise RunAborted() from None
            self.short = False

    def _s_wait(self, upto, until=None):
        """Waits until the short stream has reached sample index upto (one index per period, the target time),
        or until(t, cols) is true. Checks limit() of the variables in the stream. Returns (t, cols).
        A stream that stops is an abort."""
        stall = max(1.0, STALL_MS / 1000 * self.time_scale)
        seen, last_t, progress = 0, -1, time.monotonic()
        while True:
            if self.stop.is_set():
                raise RunAborted()
            t, cols = self.scope.snapshot()
            if len(t) > seen:
                for v, c in zip(self.s_group, cols):
                    most = self.limits.get(v.name)
                    bad = np.flatnonzero(~(np.abs(c[seen:]) <= most)) if most is not None else ()
                    if len(bad):
                        at = int(t[seen + bad[0]])
                        self.request('limit', f'limit exceeded: {v.name} = {c[seen + bad[0]]:.0f} at sample {at} '
                                              f'of the probe, limit {most}')
                        raise RunAborted()
                seen = len(t)
            if len(t) and (int(t[-1]) >= upto or (until and until(t, cols))):
                return t, cols
            if len(t) and int(t[-1]) != last_t:
                last_t, progress = int(t[-1]), time.monotonic()
            th = self.scope.thread
            if th is None or not th.is_alive():
                self.port_ok = False
                self.request('port', 'port error: the stream reader of the probe ended')
                raise RunAborted()
            if time.monotonic() - progress > stall:
                self.request('stall', f'stream stopped: no sample for {STALL_MS} ms of target time during the probe')
                raise RunAborted()
            self.stop.wait(POLL_S)

    def _s_write(self, v, value, orig=None):
        """A write in a short stream. orig is given for the first change of a variable: the restore is entered
        before the write, inside the gate, so that the safe end cannot miss it."""
        with self.gate:
            if self.stop.is_set():
                raise RunAborted()
            if orig is not None:
                self.pending[v.name] = (v, orig, value)
            try:
                err = self.scope.write(v.address, v.type, value)
            except Exception as e:      # noqa: BLE001
                self.port_failed(e)
                raise RunAborted() from None
        if err:
            if orig is not None:
                self.pending.pop(v.name, None)      # nothing was sent
            raise ProbeFail('E422', f'write of {v.name} = {value} refused: {err}', self._line(v.name))
        self._file(f'write {self.target_ms()} ms {v.name} = {value} (probe)')

    # -- steps 5, 6 and 7

    def _probe_values(self, rec):
        roles = {}
        for role, names in (('measured', [v.name for v in self.chans]), ('written', list(self.allowed)),
                            ('safe', list(self.safe)), ('limit', list(self.limits)), ('idle', list(rec.idle_reads))):
            for n in names:
                roles.setdefault(n, []).append(role)
        found = [self.var(n) for n in roles]
        size, lag = self._group_size(), self.scope.output_lag_ms
        for i in range(0, len(found), size):
            group = found[i:i + size]
            self._s_start(group)
            _, cols = self._s_wait(self._s_n(lag) + 3)
            for v, c in zip(group, cols):
                self.live[v.name] = self._raw(v, c[-1])
            self._s_stop()
        self.rows = [{'name': v.name, 'role': ', '.join(roles[v.name]), 'address': v.address, 'type': v.type,
                      'value': self.live[v.name]} for v in found]
        self.emit('variables', rows=list(self.rows))
        self._file('variables ' + ', '.join(f'{r["name"]} = {r["value"]}' for r in self.rows))

    def _probe_idle(self):
        self.phase, self.idle_calls = 'idle', 0
        try:
            self.ns['check_idle']()
        except RunAborted:
            raise
        except BaseException as e:      # noqa: BLE001 - also SystemExit of a script
            code = 'E410' if isinstance(e, ExpectFailed) else 'E412' if isinstance(e, IdleRefused) else 'E411'
            text = str(e) if isinstance(e, ScriptError) else f'{type(e).__name__}: {e}'
            raise ProbeFail(code, text, self._script_line(e)) from None
        finally:
            self.phase = None

    @staticmethod
    def _other_value(orig, low, high):
        for x in (orig + 1, orig - 1):
            if low <= x <= high:
                return x
        return None

    def _write_test(self):
        """Each allowed variable without safe_value(): changed by 1, held HOLD_MS, restored, restore read back."""
        lag = self.scope.output_lag_ms
        for name in [n for n in self.allowed if n not in self.safe]:
            low, high = self.allowed[name]
            line = self._line(name)
            if low == high:
                self._problem('W401', line, f'{name}: allow() has one value, the write test is skipped')
                continue
            v = self.var(name)
            group = [v] + [self.var(n) for n in self.limits if n != name][:self._group_size() - 1]
            self._s_start(group)
            _, cols = self._s_wait(self._s_n(lag) + 3)
            orig = self._raw(v, cols[0][-1])
            new = self._other_value(orig, low, high)
            if new is None:
                self._s_stop()
                self._problem('W402', line, f'{name} reads {orig}, allow() is {low} ... {high}: the write test is skipped')
                continue
            self._change_and_hold(v, orig, new, line)
            self._restore(v, orig, new, line)
            self._s_stop()

    def _latest(self):
        t, _ = self.scope.snapshot()
        return int(t[-1]) if len(t) else 0

    def _wait_value(self, want, n0):
        """Waits for the first sample from index n0 on that reads want, at most SEEN_MS (plus the output lag) of
        target time. Samples already on the line when the write was sent show the old value, so the hold
        starts at this sample. -> (t, cols, index of the sample or None)."""
        cap = self._latest() + self._s_n(self.scope.output_lag_ms + SEEN_MS)
        t, cols = self._s_wait(cap, lambda t, cols: np.any(cols[0][n0:] == want))
        hits = np.flatnonzero(cols[0][n0:] == want)
        return t, cols, (n0 + int(hits[0]) if len(hits) else None)

    @staticmethod
    def _wanted(v, x):
        return float(np.float32(x)) if v.type == 'f32' else x

    def _change_and_hold(self, v, orig, new, line):
        """Writes the new value and watches HOLD_MS from the first sample that shows it. If it does not show up
        or does not hold, E420 is entered."""
        want = self._wanted(v, new)
        n0 = len(self.scope.snapshot()[0])
        self._s_write(v, new, orig)
        t, cols, p = self._wait_value(want, n0)
        if p is None:
            self._problem('E420', line, f'{v.name} was set from {orig} to {new} and still reads {self._raw(v, cols[0][-1])} '
                          f'after {SEEN_MS} ms: the firmware overwrites it. Write a variable that the firmware adds '
                          'behind its own logic (an offset) instead')
            return
        end = int(t[p]) + self._s_n(HOLD_MS)
        t, cols = self._s_wait(end)
        keep = t[p:] <= end
        got = cols[0][p:][keep]
        if len(got) < self._s_n(HOLD_MS) // 2:
            raise ProbeFail('E422', f'{v.name}: only {len(got)} of {self._s_n(HOLD_MS)} samples arrived during the hold', line)
        bad = np.flatnonzero(got != want)
        if len(bad):
            ms = int(t[p:][keep][bad[0]] - t[p]) * self.s_period // 1000
            self._problem('E420', line, f'{v.name} was set from {orig} to {new} and reads {self._raw(v, got[bad[0]])} '
                          f'after {ms} ms: the firmware overwrites it. Write a variable that the firmware adds '
                          'behind its own logic (an offset) instead')

    def _restore(self, v, orig, new, line):
        """Writes the original value back and reads it back: it must show up and then hold for two samples.
        Twice at most, then E421."""
        want = self._wanted(v, orig)
        last = None
        for _ in (1, 2):
            n0 = len(self.scope.snapshot()[0])
            self._s_write(v, orig)
            t, cols, p = self._wait_value(want, n0)
            if p is not None:
                t, cols = self._s_wait(int(t[p]) + 2)
                if np.all(cols[0][p:] == want):
                    self.pending.pop(v.name, None)
                    return
            last = self._raw(v, cols[0][-1])
        raise ProbeFail('E421', f'{v.name} was changed from {orig} to {new} by the write test and could not be '
                        f'restored, it reads {last}', line)

    # -- abort causes ---------------------------------------------------------------------------

    def request(self, kind, text):
        """Enters an abort cause from any thread. Sets the abort flag at once, the monitor does the rest.
        kind: end, error, abort, limit, port, stall. True if this is the first cause."""
        with self.cause_lock:
            if self.cause:
                return False
            self.cause = (kind, text, time.monotonic())
            self.stop.set()
        with self.cv:
            self.cv.notify_all()
        return True

    # -- monitor thread -------------------------------------------------------------------------

    def _drain(self, check=True):
        """Takes the samples received since the last call, checks each one against limit()."""
        if not self.streaming:
            return
        t, cols = self.scope.snapshot()
        if not len(t) or t[-1] <= self.last_t:
            return
        i = int(np.searchsorted(t, self.last_t, side='right'))
        self.last_rx = time.monotonic()
        if check:
            for k, name, most in self.limit_cols:
                c = cols[k][i:]
                bad = np.flatnonzero(~(np.abs(c) <= most))      # also catches NaN of a float
                if len(bad):
                    at = int(t[i + bad[0]])
                    self.request('limit', f'limit exceeded: {name} = {c[bad[0]]:.0f} at {at} ms, limit {most}')
                    break
        with self.cv:
            self.last_t = int(t[-1])
            for v, c in zip(self.chans, cols):
                self.latest[v.name] = self._raw(v, c[-1])
            self.cv.notify_all()

    def _check_stream(self):
        if not self.streaming:
            return
        th = self.scope.thread
        if th is None:
            self.request('stall', 'stream stopped: the reader was stopped')
        elif not th.is_alive():         # Scope's reader dies on a port exception
            self.port_ok = False
            try:
                self.scope.ser.in_waiting
                text = 'the stream reader ended'
            except Exception as e:      # noqa: BLE001
                text = str(e)
            self.request('port', f'port error: {text}')
        elif time.monotonic() - self.last_rx > STALL_MS / 1000 * self.time_scale:
            self.request('stall', f'stream stopped: no sample for {STALL_MS} ms of target time')

    def _monitor(self):
        try:
            while self.cause is None:
                try:
                    self._drain()
                    self._check_stream()
                except Exception as e:  # noqa: BLE001 - nothing may keep the safe end from running
                    self.request('error', f'internal error in the monitor: {e!r}')
                if self.cause is None:
                    time.sleep(POLL_S)
            self._safe_end()
        except Exception as e:          # noqa: BLE001
            self.fail(f'internal error in the safe end: {e!r}')
        finally:
            self.hb_stop.set()
            try:
                if self.hb_thread:
                    self.hb_thread.join(self.port_timeout)
            finally:
                self._finish()

    def _start_heartbeat(self):
        """Once per session: the probe starts it, stream_start() finds it running."""
        if self.hb_thread is None:
            self.hb_thread = threading.Thread(target=self._heartbeat, name='tune-heartbeat', daemon=True)
            self.hb_thread.start()

    def _heartbeat(self):
        """tune_alive changes every 200 samples during a stream. On the wall clock before the stream and after
        the abort flag: a stream that stalled must not stop the heartbeat before the safe values are out.
        When the stream of the run begins, one write at once: no gap from the probe into the stream."""
        mask = (1 << (8 * self.alive.width - 1)) - 1
        value = int(time.monotonic() * 1000) & mask     # differs from where the last run stopped
        hold = self.hooks.get('hold_heartbeat')
        last_t, last_wall, by_samples = None, 0.0, False
        while not self.hb_stop.is_set():
            if self.streaming and not self.stop.is_set() and last_t is not None:
                due = not by_samples or self.last_t - last_t >= HEARTBEAT_MS
                by_samples = True
            else:
                due = time.monotonic() - last_wall >= HEARTBEAT_MS / 1000 * self.time_scale
                by_samples = False
            if due and not (hold and hold.is_set()):
                value = (value + 1) & mask
                try:
                    self.scope.write(self.alive.address, self.alive.type, value)
                except Exception as e:  # noqa: BLE001
                    self.port_failed(e)
                    return
                last_t, last_wall = self.last_t, time.monotonic()
                if self.hb_prev is not None:
                    gap = last_wall - self.hb_prev
                    self.hb_gap = max(self.hb_gap, gap)
                    if self.cap_busy or self.hb_prev_busy:
                        self.hb_gap_cap = max(self.hb_gap_cap, gap)
                self.hb_prev, self.hb_prev_busy = last_wall, self.cap_busy
                self.hb_first.set()
            self.hb_stop.wait(POLL_S)

    def _bounded(self, what, fn, *args):
        """Port call of the safe end, on a thread of its own: a hanging port must not hold up the end
        of the run. Returns (True, return value), or (False, None) after reporting the error."""
        box = {}

        def call():
            try:
                box['value'] = fn(*args)
            except Exception as e:      # noqa: BLE001
                box['error'] = e

        th = threading.Thread(target=call, name='tune-port', daemon=True)
        th.start()
        th.join(self.port_timeout)
        if th.is_alive() or 'error' in box:
            self.port_ok = False
            self.fail(f'{what}: ' + (str(box['error']) if 'error' in box else 'the port does not return'))
            return False, None
        return True, box.get('value')

    def _step(self, fn, *args):
        """One step of the safe end. A step that fails must not keep the next ones from running."""
        try:
            fn(*args)
        except Exception as e:          # noqa: BLE001
            self.fail(f'internal error in the safe end, {fn.__name__}: {e!r}')

    def _safe_end(self):
        kind, text, t0 = self.cause
        started = self.started or self.verified    # once: stream_start() may pass its checks during the safe end
        self.emit('state', state='safe_end')
        self.log(text if kind != 'end' else f'{text}, safe end')
        self._step(self._pass_gate)
        if started:
            self._step(self._send_safe, t0)
            self._step(self._keep_stream, kind)
        else:
            if self.probe_only or not self.hooks.get('skip_probe'):
                self.log('safe values not written: the probe did not verify the target, nothing but the ping reached it')
            else:
                self.log('safe values not written: stream_start() did not pass its checks, nothing reached the target')
        self._step(self._stop_stream)
        self._step(self._save_open_trace)
        self._step(self._stop_script)
        if started:
            self._step(self._verify_safe)   # with the heartbeat alive: a read-back must not pass by its timeout
            self._step(self._verify_restore)

    def _pass_gate(self):
        if self.gate.acquire(timeout=self.port_timeout):    # a script write in progress is through now
            self.gate.release()
        else:
            self.fail('a script write does not return from the port')

    def _send_safe(self, t0):
        # what the write test changed goes back first, in the same message as the safe values
        restore = [(v.address, v.type, orig) for v, orig, _ in self.pending.values()]
        writes = restore + [(v.address, v.type, value) for v, value in self.safe.values()]
        for n in (1, 2):            # 'W' has no reply: twice, then read back after the stream
            ok, err = self._bounded(f'safe values, write {n}', self.scope.write_many, writes)
            if not ok:
                return
            if err:
                self.fail(f'safe values: {err}')
                return
            if n == 1:
                self.abort_ms = (time.monotonic() - t0) * 1000
        t = self.target_ms()
        for v, orig, _ in self.pending.values():
            self._file(f'write {t} ms {v.name} = {orig} (probe restore)')
        for v, value in self.safe.values():
            self._file(f'write {t} ms {v.name} = {value} (safe)')

    def _keep_stream(self, kind):
        if kind in ('port', 'stall') or not self.port_ok or not self.streaming:
            return
        ms = SAFE_KEEP_MS + self.scope.output_lag_ms
        end, wall = self.last_t + ms, time.monotonic() + 2 * ms / 1000 * self.time_scale
        while self.last_t < end and time.monotonic() < wall:
            self._drain(check=False)
            time.sleep(POLL_S)

    def _stop_stream(self):
        if self.streaming or self.short or self.capturing:
            try:
                self._drain(check=False)
            finally:
                self.streaming = self.short = self.capturing = False
                self._bounded('stop of the stream', self.scope.stop)

    def _save_open_trace(self):
        with self.trace_lock:
            tr, self.trace = self.trace, None
            if tr:
                try:
                    t, cols = self.scope.snapshot()
                    self._store_trace(tr, 'unfinished', t, cols, True)
                except OSError as e:
                    self.fail(f'open trace not saved: {e}')
            if self.cap is not None and not self.cap_saved:     # a capture that arrived and was not saved: keep what was measured
                try:
                    self._store_capture('unfinished', True)
                except OSError as e:
                    self.fail(f'capture not saved: {e}')

    def _stop_script(self):
        th = self.script_thread
        th.join(0.2)
        for _ in range(5):
            if not th.is_alive():
                return
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(th.ident), ctypes.py_object(RunAborted))
            th.join(0.4)
        self.log('warning: the script thread does not stop, it cannot reach the target any more')

    def _read_group(self, group):
        """group: [(Var, value)]. Short stream of up to 4 variables. Returns those that differ as
        [(Var, value, read value)], None if nothing could be read."""
        frame = sum(v.width for v, _ in group) + 2
        period = max(PERIOD_US, int(np.ceil(frame * 1e6 / (MAX_LOAD * self._bytes_per_s()))))
        need = 3 + self.scope.output_lag_ms
        for attempt in (1, 2):          # one disturbed start or an empty stream on a noisy link is tried once more
            last = attempt == 2
            ok, err = self._bounded('read-back', self.scope.start, [(v.address, v.type) for v, _ in group], 0, period)
            if not ok:
                return None
            if err:
                if last:
                    self.fail(f'read-back of the safe values: {err}')
                    return None
                continue
            end = time.monotonic() + max(2.0, 1.0 * self.time_scale)
            while len(self.scope.snapshot()[0]) < need and time.monotonic() < end:
                time.sleep(0.01)
            self._bounded('read-back', self.scope.stop)
            t, cols = self.scope.snapshot()
            if len(t):
                break
            if last:
                self.fail('read-back of the safe values: no sample arrived')
                return None
        bad = []
        for (v, value), c in zip(group, cols):
            got = self._raw(v, c[-1])
            want = float(np.float32(value)) if v.type == 'f32' else value
            if got != want:
                bad.append((v, value, got))
        return bad

    def _verify_safe(self):
        if not self.port_ok:
            self.log('safe values not read back: the port is gone')
            return
        items = list(self.safe.values())
        checked = 0
        for i in range(0, len(items), READBACK_GROUP):
            group = items[i:i + READBACK_GROUP]
            bad = self._read_group(group)
            if bad:
                self.log('safe values differ, rewriting: ' + ', '.join(f'{v.name} = {g}' for v, _, g in bad))
                ok, err = self._bounded('safe values, rewrite', self.scope.write_many,
                                        [(v.address, v.type, value) for v, value, _ in bad])
                if not ok or err:
                    continue
                time.sleep((self.scope.output_lag_ms + 10) / 1000 * self.time_scale)
                bad = self._read_group([(v, value) for v, value, _ in bad])
                for v, value, got in bad or ():
                    self.fail(f'safe value not reached: {v.name} is {got}, expected {value}')
            if bad is not None and not bad:
                checked += len(group)
        if checked == len(items):
            self.log(f'safe values read back: {checked} of {len(items)} correct')

    def _verify_restore(self):
        """Reads back what the write test changed and the safe end wrote back. An error for each that is not."""
        def lost(v, orig, changed, why):
            self.fail(f'{v.name} was changed from {orig} to {changed} by the write test and could not be restored: {why}')

        items = list(self.pending.values())
        if not self.port_ok:
            for v, orig, changed in items:
                lost(v, orig, changed, 'the port is gone')
            return
        for i in range(0, len(items), READBACK_GROUP):
            group = items[i:i + READBACK_GROUP]
            bad = self._read_group([(v, orig) for v, orig, _ in group])
            if bad:
                self.log('restored values differ, rewriting: ' + ', '.join(f'{v.name} = {g}' for v, _, g in bad))
                ok, err = self._bounded('restore, rewrite', self.scope.write_many,
                                        [(v.address, v.type, value) for v, value, _ in bad])
                if ok and not err:
                    time.sleep((self.scope.output_lag_ms + 10) / 1000 * self.time_scale)
                    bad = self._read_group([(v, value) for v, value, _ in bad])
            if bad is None:
                bad = [(v, orig, '?') for v, orig, _ in group]
            gone = {v.name: got for v, _, got in bad}
            for v, orig, changed in group:
                if v.name in gone:
                    lost(v, orig, changed, 'it could not be read back' if gone[v.name] == '?' else f'it reads {gone[v.name]}')
                else:
                    self.pending.pop(v.name, None)
        if items and not self.pending:
            self.log(f'restored by the safe end: {len(items)} variable(s) read back correct')

    # -- end of the run -------------------------------------------------------------------------

    def _tuning_refused(self, status):
        """Why tuning.h is not written, or None. The rule: the run ended normally (script returned, no
        abort, no error, safe values confirmed), define() was called, and no trace after the last row()
        is failed, invalid or unfinished. Without a row() that covers all traces."""
        if not self.defines:
            return 'no define()'
        if status != 'ok':
            return f'run ended {status}'
        for tr in self.traces[self.last_row_trace:]:
            if tr['failed'] or tr['invalid'] or tr['unfinished']:
                return f'trace {tr["index"]:03d} "{tr["title"]}" after the last row() does not count'
        return None

    def _write_tuning(self, status):
        why = self._tuning_refused(status)
        if why:
            if self.defines:
                self.log(f'tuning.h not written: {why}')
            return None
        path = os.path.join(self.folder, 'tuning.h')
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(f'/* tuning.h - written by mini-scope, {self.script}, {time.strftime("%Y-%m-%d %H:%M:%S")} */\n')
                f.write('#ifndef TUNING_H\n#define TUNING_H\n\n')
                for k, v in self.defines.items():
                    f.write(f'#define {k} {v}\n')
                f.write('\n#endif\n')
        except OSError as e:
            self.fail(f'tuning.h not written: {e}')
            return None
        return path

    def _summary_data(self, result):
        """The dict for render_png: what the run is known for, also after an abort or an error."""
        status, reason = result['status'], ' '.join(str(result['reason']).split())
        text, mark, chan = self.summary['summary_text'], self.summary['summary_mark'], self.summary['summary_channel']
        if status == 'ok':
            head = text or 'Run finished, no result line'
        else:
            head = reason if reason.lower().startswith(status) else f'{status}: {reason}'
        head = head if len(head) <= 130 else head[:127] + '...'
        port = getattr(self.scope.ser, 'port', None) or type(self.scope.ser).__name__
        info = [f'{self.script}, port {port}, ELF {os.path.basename(self.elf_name or "unknown")}']
        notes = []
        unit = self.units.get(chan)
        series, why = [], None
        if chan is None:
            why = 'summary_channel() was not set'
        elif not self.traces:
            why = 'no trace was recorded'
        for tr in self.traces[:SUMMARY_MAX] if chan else []:
            if chan not in tr['values']:
                continue
            y = tr['values'][chan] * (unit[0] if unit else 1)
            flags = [k for k in ('failed', 'invalid', 'unfinished') if tr[k]]
            style = {'color': SUMMARY_COLORS[len(series) % len(SUMMARY_COLORS)]}
            if flags:
                style['linestyle'] = '--'
            if mark is not None and any(_same(w, mark) for w in re.findall(r'-?\d+(?:\.\d+)?|\w+', tr['title'])):
                style.update(color='#000000', linewidth=2.8, zorder=5)
            series.append((f'{tr["index"]:03d} {tr["title"]}' + (f' ({", ".join(flags)})' if flags else ''),
                           tr['time_ms'], y, style))
        if why:
            info.append(f'No curves: {why}')
        else:
            info.append(f'{chan}, {len(series)} trace(s) from trace start, dashed: failed, invalid or unfinished'
                        + (f', marked: {mark}' if mark is not None else ''))
        if status != 'ok' and text:
            info.append(f'Result line of the script: {text}')
        if len(self.traces) > SUMMARY_MAX:
            notes.append(f'Showing the first {SUMMARY_MAX} of {len(self.traces)} traces; the rest is in the trace_NNN.csv files.')
        rows = [[_cell(v) for v in vals] + [status] for vals, status in self.table_rows[:SUMMARY_MAX]]
        mark_row = None
        if mark is not None:
            mark_row = next((i for i, r in enumerate(rows) if any(_same(c, mark) for c in r[:-1])), None)
        if len(self.table_rows) > SUMMARY_MAX:
            notes.append(f'Showing the first {SUMMARY_MAX} of {len(self.table_rows)} table rows; the rest is in results.csv.'
                         + (' The marked row is not among them.' if mark is not None and mark_row is None else ''))
        return {'stamp': time.strftime('%Y-%m-%d %H:%M:%S'), 'note': head, 'info': info, 'series': series, 'xlim': None,
                'cursors': [], 'columns': (self.columns + ['status']) if rows else [], 'rows': rows, 'notes': notes,
                'legend': 'outside', 'mark_row': mark_row, 'empty_text': why,
                'ylabel': (f'{chan} [{unit[1]}]' if unit else f'{chan} (raw)') if chan else None}

    def _summary_png(self, result):
        """Writes summary.png, returns its path or None. Never raises and never takes longer than RENDER_S: the
        picture comes after the safe values, nothing in it may hold the end of the run."""
        if self.probe_only or not self.render_png:
            self._file('no summary.png in a probe' if self.probe_only else 'summary.png not made: no render_png handed in')
            return None
        box = {}

        def work():
            try:
                box['png'] = self.render_png(self._summary_data(result))
            except Exception as e:      # noqa: BLE001
                box['err'] = repr(e)

        th = threading.Thread(target=work, name='tune-summary', daemon=True)
        th.start()
        th.join(RENDER_S)
        if th.is_alive():
            self.log(f'summary.png not written: rendering took more than {RENDER_S:g} s')
            return None
        try:
            if 'err' in box:
                raise RuntimeError(box['err'])
            path = os.path.join(self.folder, 'summary.png')
            with open(path, 'wb') as f:
                f.write(box['png'])
            return path
        except Exception as e:          # noqa: BLE001
            self.log(f'summary.png not written: {e}')
            return None

    def _close_run(self, result):
        """Fills in result, writes tuning.h and the end of run.txt, closes run.txt."""
        kind = self.cause[0] if self.cause else 'error'
        status = {'end': 'ok', 'error': 'error'}.get(kind, 'aborted')
        if status == 'ok' and self.errors:
            status = 'error'
        tuning = self._write_tuning(status)
        if not self.probe_only and not os.path.exists(os.path.join(self.folder, 'results.csv')):
            try:
                open(os.path.join(self.folder, 'results.csv'), 'w').close()
            except OSError as e:
                self.errors.append(f'results.csv: {e}')
        result.update(status=status, errors=list(self.errors), tuning_h=tuning)
        result['summary_png'] = self._summary_png(result)       # before the 'result' event, so that the receiver can show it
        text, target = result['reason'], result['abort_ms_target']
        if self.hb_gap:
            ms, cap = self.hb_gap * 1000, self.hb_gap_cap * 1000
            self._file(f'heartbeat: largest gap between two writes {ms:.0f} ms wall clock, {ms / self.time_scale:.0f} ms target time '
                       f'at scale {self.time_scale}; around a capture {cap:.0f} ms wall clock, {cap / self.time_scale:.0f} ms target time; '
                       f'the target allows {HEARTBEAT_LIMIT_MS} ms')
        self._file(f'result {status}')
        self._file(f'reason {text}')
        if target is not None:
            self._file(f'abort time {self.abort_ms:.1f} ms wall clock, {target:.1f} ms target time')
        for k, v in self.summary.items():
            if v is not None:
                self._file(f'{k} {v}')
        for k, v in self.defines.items():
            self._file(f'define {k} {v}')
        self.run_file.close()

    def _finish(self):
        """Whatever fails here, the run ends: result is a dict, done is set, the next run can start."""
        global _active
        target = None if self.abort_ms is None else self.abort_ms / self.time_scale
        result = {'status': 'error', 'reason': self.cause[1] if self.cause else 'the monitor ended', 'abort_ms': self.abort_ms,
                  'abort_ms_target': target, 'errors': list(self.errors), 'folder': self.folder, 'tuning_h': None,
                  'summary_png': None, 'rule_codes': dict(self.rule_codes), 'rows': list(self.rows),
                  'problems': [f'{p} [{p.code}]' for p in self.problems], **self.summary}
        try:
            self._close_run(result)
        except Exception as e:          # noqa: BLE001 - the GUI waits for the end of the run
            self.fail(f'internal error at the end of the run: {e!r}')
            if result['tuning_h']:      # tuning.h exists only for a run with status ok
                try:
                    os.remove(result['tuning_h'])
                except OSError as e2:
                    self.fail(f'tuning.h not removed: {e2}')
            result.update(status='error', errors=list(self.errors), tuning_h=None)
        finally:
            self.result = result
            try:
                self.emit('result', **result)
                self.emit('state', state='done')
                self.events.put(None)
                if self.deliver_thread:
                    self.deliver_thread.join(1.0)
            finally:
                with _active_lock:
                    _active = None
                self.done.set()


def _launch(script_path, scope, table, on_event, out_dir, elf_name, hooks, probe_only, render_png=None):
    """Stages 1 to 3, then a Session started on its thread. Raises RunRefused, nothing reached the target."""
    global _active
    sys.modules.setdefault('tune_api', sys.modules[__name__])
    with _active_lock:
        if _active is not None:
            raise RunRefused('another run is active')
    problems = []
    if not (hooks or {}).get('skip_checks') and os.path.isfile(script_path):
        import tune_check       # here, not at the top: tune_check imports this module
        problems = tune_check.check_script(script_path, table, baud=getattr(scope.ser, 'baudrate', None))
        errors = [p for p in problems if p.severity == 'error']
        if errors:
            lines = '\n'.join(f'{p} [{p.code}]' for p in errors)
            e = RunRefused(f'{os.path.basename(script_path)} has {len(errors)} error(s), nothing was sent:\n{lines}')
            e.problems = problems
            raise e
    with _active_lock:
        if _active is not None:
            raise RunRefused('another run is active')
        _active = s = Session(script_path, scope, table, on_event, out_dir, elf_name, hooks, probe_only, render_png)
    for p in problems:
        s.rule_codes[p.code] = s.rule_codes.get(p.code, 0) + 1
    s.start()
    for p in problems:
        s.log(f'{p} [{p.code}]')
    return s


def run(script_path, scope, table, on_event=None, out_dir='runs', elf_name=None, hooks=None, render_png=None):
    """Starts a script on a worker thread and returns its Session. See the top of this file.
    Before anything else tune_check runs stages 1 to 3 (compile, ELF check, dry run, no target): errors raise
    RunRefused with the problems as attribute `problems`, nothing reaches the target and no run folder is made.
    Warnings go to the log and run.txt; the count per rule code is result['rule_codes'].
    The script thread then runs the probe (stage 4, state 'probe') before main().
    render_png: scope_gui.render_png; with it every run ends with summary.png in the run folder (result['summary_png']).
    hooks are for the tests: 'before_send': fn(stage) inside write(), 'hold_heartbeat': Event,
    'skip_checks': True runs without the checks of tune_check, 'skip_probe': True without the probe."""
    return _launch(script_path, scope, table, on_event, out_dir, elf_name, hooks, False, render_png)


def probe(script_path, scope, table, on_event=None, out_dir=None, elf_name=None, hooks=None):
    """Stage 4 alone, for the Probe button and run_script: stages 1 to 3, then the probe, then the safe end.
    Blocks until it ended and returns the result dict of a run, with 'rows' (the variables) and 'problems'.
    abort() works from another thread. Raises RunRefused like run(). out_dir None: no run folder is kept
    (run.txt goes to a temporary folder that is removed), result['folder'] is None then."""
    s = _launch(script_path, scope, table, on_event, out_dir, elf_name, hooks, True)
    s.wait()
    if s.temp_root:
        shutil.rmtree(s.temp_root, ignore_errors=True)
        s.result['folder'] = None
    return s.result


def abort(reason='abort requested'):
    """Aborts the active run, from any thread. False if there is no run or it is already ending."""
    s = _active
    return s.request('abort', f'aborted: {reason}') if s else False


# -- the functions of a script ------------------------------------------------------------------


def _session():
    """The session of the calling script thread, or an error. Stops the script after the abort flag."""
    s = getattr(_local, 'session', None)
    if s is None:
        raise ScriptError('this function only works inside a running script')
    if s.stop.is_set():
        raise RunAborted()
    return s


def channels(*names):
    """Variables to stream, at most 4, one sample per ms."""
    s = _session()
    s.declaring('channels')
    s._mode('stream', 'channels')
    s.chans = [s.var(n) for n in names]


def allow(name, low, high):
    """The variable may be written with values from low to high. Raw integers, as in C."""
    s = _session()
    s.declaring('allow')
    v = s.var(name)
    if not low <= high:
        raise ScriptError(f'allow("{name}", {low}, {high}): min is above max')
    if v.type in TYPE_RANGE and not (TYPE_RANGE[v.type][0] <= low and high <= TYPE_RANGE[v.type][1]):
        raise ScriptError(f'allow("{name}", {low}, {high}): outside the range of {v.type}')
    s.allowed[v.name] = (low, high)


def safe_value(name, value):
    """Written at the end of the run, on abort and on error."""
    s = _session()
    s.declaring('safe_value')
    v = s.var(name)
    s.safe[v.name] = (v, value)


def limit(name, most):
    """The whole run is aborted if the magnitude of the variable exceeds this raw value."""
    s = _session()
    s.declaring('limit')
    if not most >= 0:
        raise ScriptError(f'limit("{name}", {most}): compares the magnitude, the limit must not be negative')
    s.limits[s.var(name).name] = most


def stream_start():
    """Runs all checks, starts the stream and the heartbeat, runs the trial stream."""
    _session().stream_start()


def write(name, value):
    """Writes a variable. Refused unless allowed. Returns when samples after the write can have arrived."""
    _session().write(name, value)


def read(name):
    """Value of a variable in channels() at the script cursor (see the module docstring), the raw integer."""
    return _session().read(name)


def wait_ms(ms):
    """Waits for ms of target time: received samples during a stream, else scaled wall clock."""
    _session().wait_target(ms)


def capture_channels(*names):
    """Variables of a capture, at most 4. The buffer holds 4096 bytes: two 16 bit variables give 1024 samples."""
    _session().capture_channels(names)


def capture_period_us(us):
    """Sample period of a capture in microseconds, default 100."""
    _session().capture_period_us(us)


def capture_trigger(name, edge, level, pre_percent):
    """Trigger channel, edge (RISING or FALLING), raw level, and how much of the buffer comes before the trigger."""
    _session().capture_trigger(name, edge, level, pre_percent)


def capture_start():
    """Runs all checks for capture mode, starts the heartbeat, runs the trial capture."""
    _session().capture_start()


def capture_arm():
    """Starts one capture. Returns when the pretrigger buffer is full: the trigger can fire from now on."""
    _session().capture_arm()


def capture_wait(timeout_ms):
    """Waits for the buffer: 0 when it has arrived, -1 after timeout_ms (wall clock of the port, scaled)."""
    return _session().capture_wait(timeout_ms)


def capture_max(name):
    """Largest value of the channel in the capture, a raw integer."""
    return _session().capture_max(name)


def capture_final(name):
    """Mean of the last 10 % of the capture, a raw integer."""
    return _session().capture_final(name)


def capture_rise_us(name):
    """Rise time from 10 % to 90 % of the step to capture_final(), in microseconds. -1 if not found."""
    return _session().capture_rise_us(name)


def capture_length():
    """Number of samples of the capture."""
    return _session().capture_length()


def capture_value(name, i):
    """Raw value of sample i (0 = first sample of the buffer) of the channel."""
    return _session().capture_value(name, i)


def capture_save(fmt, *args):
    """Saves the capture as a trace, its title is a C format string."""
    s = _session()
    s.capture_save(fmt % args)


def trace_begin():
    """Starts recording one curve."""
    _session().trace_begin()


def trace_end(fmt, *args):
    """Ends the curve, its title is a C format string."""
    s = _session()
    s.trace_end(fmt % args)


def trace_failed():
    """Marks the open trace as failed."""
    s = _session()
    if s.mode == 'capture':     # marks the capture, the next capture_save() draws it dashed
        if s.cap is None:
            raise ScriptError('trace_failed(): no capture, capture_wait() has to return 0 first')
        s.cap_failed = True
        return
    if s.trace is None:
        raise ScriptError('trace_failed() without trace_begin()')
    s.trace['failed'] = True


def table(*names):
    """Column names of the comparison table."""
    _session().table(names)


def row(*values):
    """One row of the comparison table."""
    _session().row(values)


def summary_channel(name):
    """The channel shown in the summary diagram."""
    s = _session()
    s.summary['summary_channel'] = s.var(name).name


def summary_text(fmt, *args):
    """The result line of the summary diagram."""
    _session().summary['summary_text'] = fmt % args


def summary_mark(value):
    """Highlights the table row and trace of this value."""
    _session().summary['summary_mark'] = value


def unit(name, factor, text):
    """Physical value = raw value x factor, for display only."""
    s = _session()
    v = s.var(name)
    s.units[v.name] = (factor, text)
    s.emit('unit', name=v.name, factor=factor, text=text)


def define(name, value):
    """One line of the exported tuning.h."""
    s = _session()
    if not re.fullmatch(r'[A-Za-z_]\w*', name):
        raise ScriptError(f'define("{name}"): not a C name')
    s.defines[name] = value


def printf(fmt, *args):
    """C format string, into the progress log."""
    s = _session()
    s.log((fmt % args).rstrip('\n'))


def Q15(x):
    """Float to Q15 integer, Q15(0.25) = 8192. Not clamped: Q15(1.0) is 32768, which allow() refuses."""
    return int(round(x * 32768))


def expect(condition, text):
    """In check_idle(): stops the run with this text if the condition is false."""
    _session()
    if not condition:
        raise ExpectFailed(text)
