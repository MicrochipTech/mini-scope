"""tune_check.py - finds the mistakes in a tuning script before anything is written to the target.

    problems = tune_check.check_script('tune_startup.py', table)       # list of Problem, no target needed
    for p in problems: print(p)                                         # tune_startup.py:19: error: text

Stage 1 compile: syntax errors (compile) and undefined names (pyflakes), the required functions.
Stage 2 ELF check and stage 3 dry run share one recorder: the API functions are replaced by recorders, the
script runs on them (main(), then check_idle()), and every call is checked and recorded with its script line.
Stage 4 (the target probe) runs in tune_api, because it needs the target; its rules (E4xx, W4xx) are in RULES.
record() exposes what it needs: the declarations of the script and the variables check_idle() reads.

The script is not changed. Its `from tune_api import *` is answered by a private module object: the dry run
executes the script with its own `__import__`, so sys.modules and the real tune_api are not touched, a real run
cannot reach the recorder and the recorder has no Scope, so it cannot reach a port. A run that never ends (read()
gives 0 for ever) is stopped after a call budget per function and a time budget for the whole dry run, with a
warning at the script line. No file is written.

Every rule has a stable code (RULES below): E = error, W = warning. The GUI, the report and the run result
(rule_codes) use the codes only. Line numbers point into the script. A problem is reported once per code, line
and variable.
"""
import ast
import builtins
import ctypes
import difflib
import os
import re
import sys
import threading
import time
import types
from typing import NamedTuple

import tune_api
from elf_vars import VarError
from pyflakes import checker, messages

MAX_CALLS = 50_000          # API calls of one function (main, check_idle) in a dry run
MAX_SECONDS = 5.0           # wall clock of a whole dry run
GRACE_S = 2.0               # after the time is up: how long the stop request is repeated
MAX_RECORDED = 200_000      # entries of Recording.calls
HANDLE_STAR = True          # False: pyflakes on the unchanged tree. Only the tests set it, to show what the trap does.

# code: (severity, stage, meaning). One table, M7d and the report build on it.
RULES = {
    'E101': ('error', 1, 'syntax error'),
    'E102': ('error', 1, 'undefined name (also a mistyped API function or constant)'),
    'E103': ('error', 1, 'local variable used before it is assigned'),
    'E104': ('error', 1, 'main() is missing'),
    'E105': ('error', 1, 'check_idle() is missing'),

    'E201': ('error', 2, 'variable not found in the ELF (suggestions in the text)'),
    'E202': ('error', 2, 'struct without a member name'),
    'E203': ('error', 2, 'array without an index'),
    'E204': ('error', 2, 'pointer'),
    'E205': ('error', 2, 'bit field'),
    'E206': ('error', 2, 'width other than 1, 2 or 4 bytes'),
    'E207': ('error', 2, 'address not in RAM (const data in flash)'),
    'E208': ('error', 2, 'name cannot be resolved (syntax, no such member or index, bad address)'),
    'E209': ('error', 2, 'name exists several times (static in several files)'),
    'E210': ('error', 2, 'the ELF has no debug information'),
    'E211': ('error', 2, 'type cannot be read'),
    'E212': ('error', 2, 'more channels than fit'),
    'E213': ('error', 2, 'the stream needs more of the UART than allowed'),
    'E214': ('error', 2, 'no allow() before stream_start()'),
    'E215': ('error', 2, 'no safe_value() before stream_start()'),
    'E216': ('error', 2, 'no channels() before stream_start(), no capture_channels() before capture_start()'),
    'E217': ('error', 2, 'the heartbeat variable tune_alive is not in the ELF'),
    'E218': ('error', 2, 'f32 variable as capture trigger'),
    'E219': ('error', 2, 'the transfer of a capture takes longer than allowed at this baud rate'),
    'E220': ('error', 2, 'allow() range outside the C type'),
    'E221': ('error', 2, 'allow() with min above max'),
    'E222': ('error', 2, 'safe_value() outside the allow() range'),
    'E223': ('error', 2, 'safe_value() on a variable without allow()'),
    'E224': ('error', 2, 'limit() on a variable that is not in channels()'),
    'E225': ('error', 2, 'safe_value() is not a whole number'),
    'E226': ('error', 2, 'limit() with a negative value'),
    'E227': ('error', 2, 'capture_period_us() is not a whole number from 1 to 65535'),
    'E228': ('error', 2, 'capture_trigger() missing, or edge, level, pretrigger or channel wrong'),
    'E229': ('error', 2, 'capture_period_us() below the floor of 10 us, mini-scope does not hold it (1 to 2 us hangs the target)'),
    'W201': ('warning', 2, 'a written variable is not volatile'),
    'W202': ('warning', 2, 'a 32-bit variable can tear'),
    'W203': ('warning', 2, 'limit() in capture mode is only checked after the transfer'),
    'W204': ('warning', 2, 'a written variable has no safe_value()'),
    'W205': ('warning', 2, 'a measured variable has no unit(), shown as raw'),
    'W206': ('warning', 2, 'capture_period_us() below 50 us may not hold under interrupt load'),

    'E301': ('error', 3, 'write() value outside the allow() range'),
    'E302': ('error', 3, 'read() of a variable that is not in channels()'),
    'E303': ('error', 3, 'write() of a variable without allow()'),
    'E304': ('error', 3, 'row() has another number of values than table() has columns'),
    'E305': ('error', 3, 'table() or row() used wrongly'),
    'E306': ('error', 3, 'call before stream_start()'),
    'E307': ('error', 3, 'declaration after stream_start()'),
    'E308': ('error', 3, 'trace_begin(), trace_end() and trace_failed() out of order'),
    'E309': ('error', 3, 'stream_start() called twice'),
    'E310': ('error', 3, 'define() name is not a C name'),
    'E311': ('error', 3, 'format string does not match its arguments'),
    'E312': ('error', 3, 'a value is not a number'),
    'E313': ('error', 3, 'a variable name is not a string'),
    'E314': ('error', 3, 'read() in capture mode (outside check_idle())'),
    'E315': ('error', 3, 'stream calls and capture calls in one script'),
    'E316': ('error', 3, 'capture calls out of order (arm, wait, then max, final, rise, length, value, save)'),
    'E390': ('error', 3, 'runtime error in the script (missing argument, wrong type, ...)'),
    'W301': ('warning', 3, 'dry run stopped by its budget, the rest was not checked'),

    'E401': ('error', 4, 'the target does not answer the ping'),
    'E402': ('error', 4, 'the firmware of the target does not match the ELF, or the ELF cannot prove its build (tune_build, tune_build_id)'),
    'E403': ('error', 4, 'variables cannot be read from the target'),
    'E404': ('error', 4, 'the heartbeat variable cannot be written'),
    'E410': ('error', 4, 'expect() in check_idle() is false: the target is not what the script assumes'),
    'E411': ('error', 4, 'runtime error in check_idle()'),
    'E412': ('error', 4, 'check_idle() touches the target (write, stream_start)'),
    'E420': ('error', 4, 'a written variable does not hold for 100 ms: the firmware overwrites it'),
    'E421': ('error', 4, 'a variable changed by the write test could not be restored'),
    'E422': ('error', 4, 'the write test could not be done (stream or port)'),
    'W401': ('warning', 4, 'write test skipped: allow() has a single value'),
    'W402': ('warning', 4, 'write test skipped: the current value is outside allow() and no neighbour is inside'),
}
VAR_RULE = {'not_found': 'E201', 'needs_member': 'E202', 'needs_index': 'E203', 'pointer': 'E204',
            'bitfield': 'E205', 'width': 'E206', 'not_ram': 'E207', 'ambiguous': 'E209', 'no_debug': 'E210',
            'unsupported': 'E211', 'syntax': 'E208', 'not_struct': 'E208', 'not_array': 'E208',
            'index_range': 'E208', 'bad_address': 'E208'}
BLOCKING = ('E101', 'E102', 'E103')     # without these the script cannot run, not even on the recorder


class Problem(NamedTuple):
    code: str
    severity: str           # 'error' | 'warning'
    file: str               # base name of the script
    line: int               # line in the script
    text: str
    stage: int              # 1 compile, 2 ELF check, 3 dry run

    def __str__(self):
        return f'{self.file}:{self.line}: {self.severity}: {self.text}'


class Recording(NamedTuple):
    problems: list          # Problem, stages 2 and 3
    calls: list             # (line, name, args) of every API call, in order
    stopped: bool           # the budget ended the dry run
    declared: dict = {}     # what main() declared: chans [Var], allowed {name: (low, high, line)},
                            # safe {name: (Var, value, line)}, limits {name: (most, line)},
                            # chans_line
    idle_reads: tuple = ()  # Var names read by check_idle(), in order of first use


def check_script(path, table, baud=None, max_calls=MAX_CALLS, max_seconds=MAX_SECONDS):
    """All problems of the script, sorted by stage, line, code. Nothing is written, no target is needed.
    baud: of the port, for the UART load of the stream (default: tune_api.BAUD)."""
    name = os.path.basename(path)
    try:
        with open(path, 'rb') as f:
            source = f.read()
    except OSError as e:
        return [Problem('E101', 'error', name, 1, f'cannot read the script: {e.strerror or e}', 1)]
    problems, tree = _compile(name, path, source)
    if tree is not None and not any(p.code in BLOCKING for p in problems):
        problems += dry_run(path, source, tree, table, baud, max_calls, max_seconds).problems
    return sorted(problems, key=lambda p: (p.stage, p.line, p.code))


def check_compile(path):
    """Stage 1 alone (syntax, undefined names, required functions), fast, no table: the GUI runs it when the
    file is selected or saved."""
    name = os.path.basename(path)
    try:
        with open(path, 'rb') as f:
            source = f.read()
    except OSError as e:
        return [Problem('E101', 'error', name, 1, f'cannot read the script: {e.strerror or e}', 1)]
    return sorted(_compile(name, path, source)[0], key=lambda p: (p.line, p.code))


def record(path, table, baud=None, max_calls=MAX_CALLS, max_seconds=MAX_SECONDS):
    """The dry run alone: Recording with the problems of stages 2 and 3 and every API call with its script line.
    None if the script does not compile."""
    with open(path, 'rb') as f:
        source = f.read()
    problems, tree = _compile(os.path.basename(path), path, source)
    if tree is None or any(p.code in BLOCKING for p in problems):
        return None
    return dry_run(path, source, tree, table, baud, max_calls, max_seconds)


# -- stage 1 --------------------------------------------------------------------------------------


def _is_star(node):
    return isinstance(node, ast.ImportFrom) and node.module == 'tune_api' and any(a.name == '*' for a in node.names)


def _top_names(tree):
    """Names bound at the top level of the script, for suggestions."""
    names = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            names.update(t.id for t in n.targets if isinstance(t, ast.Name))
    return names


def _compile(name, path, source):
    """Stage 1. -> (problems, tree); tree is None after a syntax error."""
    def err(code, line, text):
        return Problem(code, 'error', name, line or 1, text, 1)

    try:
        compile(source, path, 'exec')
        tree = ast.parse(source, path)
    except SyntaxError as e:
        return [err('E101', e.lineno, e.msg)], None
    except (ValueError, RecursionError, MemoryError) as e:     # null bytes, absurd nesting
        return [err('E101', 1, str(e))], None
    problems = []
    star = any(_is_star(n) for n in tree.body)
    check_tree, api = tree, []
    if star and HANDLE_STAR:
        # With the star import pyflakes only says "may be undefined from star import" and reports no undefined
        # name at all. So: take the star import out and hand the API names in as builtins.
        api = list(tune_api.__all__)
        check_tree = ast.Module(body=[n for n in tree.body if not _is_star(n)], type_ignores=[])
    seen = set()
    known = sorted(set(api) | _top_names(tree))
    for m in sorted(checker.Checker(check_tree, filename=name, builtins=api).messages, key=lambda m: m.lineno):
        if isinstance(m, messages.UndefinedName):
            word = m.message_args[0]
            if (word, m.lineno) in seen:
                continue
            seen.add((word, m.lineno))
            near = difflib.get_close_matches(word, known, 3, 0.7)
            hint = f' - did you mean {", ".join(near)}?' if near else ''
            problems.append(err('E102', m.lineno, f"undefined name '{word}'{hint}"))
        elif isinstance(m, messages.UndefinedLocal):
            problems.append(err('E103', m.lineno, m.message % m.message_args))
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if 'main' not in defined:
        problems.append(err('E104', 1, 'no main() in the script'))
    if 'check_idle' not in defined:
        problems.append(err('E105', 1, 'no check_idle() in the script: it states what must be true at standstill'))
    return problems, tree


# -- stages 2 and 3 -------------------------------------------------------------------------------


class _Stop(BaseException):
    """Ends the dry run at the budget. A BaseException, so that `except Exception` in a script cannot hold it."""


def dry_run(path, source, tree, table, baud=None, max_calls=MAX_CALLS, max_seconds=MAX_SECONDS):
    """Stages 2 and 3 on the recorder. -> Recording. Runs on a thread of its own, see the top of this file."""
    d = _Dry(path, source, tree, table, baud, max_calls, max_seconds)
    return d.run()


def _async_raise(thread_id):
    ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(thread_id), ctypes.py_object(_Stop))


class _Dry:
    def __init__(self, path, source, tree, table, baud, max_calls, max_seconds):
        self.path = path
        self.name = os.path.basename(path)
        self.source = source
        self.table = table
        self.baud = baud or tune_api.BAUD
        self.max_calls, self.max_seconds = max_calls, max_seconds
        self.lock = threading.Lock()
        self.closed = False
        self.problems, self.seen, self.calls = [], set(), []
        self.cache = {}                     # name -> Var | VarError
        self.consts = {}                    # string value -> line of `NAME = "value"` in the script
        for n in tree.body:
            if (isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                    and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)):
                self.consts.setdefault(n.value.value, n.lineno)
        self.has_main = any(isinstance(n, ast.FunctionDef) and n.name == 'main' for n in tree.body)
        self.has_idle = any(isinstance(n, ast.FunctionDef) and n.name == 'check_idle' for n in tree.body)
        self.phase = 'exec'
        self.n_calls = 0
        self.line = 1                       # script line of the API call in progress
        self.expired = False
        self.stopped = False
        self.stops = 0
        self._declare_state()

    def _declare_state(self):
        self.chans = []                     # Var
        self.chans_line = 1
        self.allowed = {}                   # name -> (min, max, line)
        self.safe = {}                      # name -> (Var, value, line)
        self.limits = {}                    # name -> (max, line)
        self.units = set()
        self.idle_reads = []                # names read by check_idle()
        self.columns = None
        self.started = False
        self.trace_open = False
        self.mode = None                    # 'stream' or 'capture', set by the first call of either kind
        self.cap_period = tune_api.CAPTURE_PERIOD_US
        self.cap_trigger = None             # (Var, line)
        self.cap_state = None               # None, 'armed' (capture_arm()) or 'have' (capture_wait())

    # -- running --------------------------------------------------------------------------------

    def run(self):
        t = threading.Thread(target=self._thread, name='tune-check', daemon=True)
        t.start()
        t.join(self.max_seconds)
        if t.is_alive():
            self.expired = True
            deadline = time.monotonic() + GRACE_S
            while t.is_alive() and time.monotonic() < deadline:
                _async_raise(t.ident)
                t.join(0.1)
            if t.is_alive():                # blocked in a call that does not return: leave it, say where
                frame = sys._current_frames().get(t.ident)
                self._stopped(self._frame_line(frame), self.phase, f'{self.max_seconds:g} s, still blocked')
        with self.lock:
            self.closed = True
            declared = {'chans': list(self.chans), 'allowed': dict(self.allowed), 'safe': dict(self.safe),
                        'limits': dict(self.limits), 'chans_line': self.chans_line}
            return Recording(list(self.problems), list(self.calls), self.stopped, declared, tuple(self.idle_reads))

    def _thread(self):
        try:
            ns = self._namespace()
            code = compile(self.source, self.path, 'exec')
            if self._phase('exec', lambda: exec(code, ns)):         # noqa: S102 - the script is the user's own program
                if self.has_main and self._phase('main', ns['main']) and not self.stopped:
                    self._end_checks()
                if self.has_idle and not self.expired:
                    self._phase('check_idle', ns['check_idle'])
        except BaseException:               # noqa: BLE001 - a late stop request must not print a traceback
            pass

    def _phase(self, phase, fn):
        """Runs one part of the script. True if it ended normally."""
        self.phase, self.n_calls = phase, 0
        try:
            fn()
            return True
        except _Stop as e:
            self._stopped(self._tb_line(e.__traceback__), phase,
                          f'{self.max_calls} API calls' if self.n_calls > self.max_calls else f'{self.max_seconds:g} s')
        except BaseException as e:          # noqa: BLE001 - also SystemExit of a script
            self.add('E390', self._tb_line(e.__traceback__), f'{type(e).__name__}: {e}')
        return False

    def _stopped(self, line, phase, budget):
        self.stopped = True
        self.add('W301', line, f'dry run of {phase}() stopped after {budget}: the fake target never changes, so a loop '
                               f'that waits for a state or a fault does not end here. The rest of {phase}() was not checked')

    def _tb_line(self, tb):
        line = 1
        while tb:
            if tb.tb_frame.f_code.co_filename == self.path:
                line = tb.tb_lineno
            tb = tb.tb_next
        return line

    def _frame_line(self, frame):
        while frame:
            if frame.f_code.co_filename == self.path:
                return frame.f_lineno
            frame = frame.f_back
        return 1

    def add(self, code, line, text, subject=''):
        sev, stage, _ = RULES[code]
        key = (code, line, subject)
        with self.lock:
            if self.closed or key in self.seen:
                return
            self.seen.add(key)
            self.problems.append(Problem(code, sev, self.name, line, text, stage))

    # -- the module the script imports ----------------------------------------------------------

    def _namespace(self):
        real = builtins.__import__
        module = types.ModuleType('tune_api')
        for k, fn in self._api().items():
            setattr(module, k, fn)
        module.__all__ = list(tune_api.__all__)
        module.ScriptError = tune_api.ScriptError

        def private_import(name, globals=None, locals=None, fromlist=(), level=0):
            if level == 0 and name == 'tune_api':
                return module
            return real(name, globals, locals, fromlist, level)

        b = dict(vars(builtins))
        b['__import__'] = private_import
        return {'__name__': '__tune_script__', '__file__': self.path, '__builtins__': b}

    def _enter(self, name, args):
        """Every API call: budget, script line, record."""
        self.n_calls += 1
        self.line = self._frame_line(sys._getframe(2))
        if len(self.calls) < MAX_RECORDED:
            self.calls.append((self.line, name, args))
        if self.expired:
            self.stops += 1
            while self.stops > 3:           # a script that swallows the stop: park the thread, it costs nothing
                time.sleep(3600)
            raise _Stop()
        if self.n_calls > self.max_calls:
            raise _Stop()

    def var(self, name):
        """Var or None (the problem is recorded). Same rules as Session.var()."""
        if not isinstance(name, str):
            self.add('E313', self.line, f'the variable name must be a string, not {name!r}')
            return None
        if name not in self.cache:
            try:
                v = self.table.lookup(name)
                self.cache[name] = v if v.type else v._replace(width=2, type='s16')
            except VarError as e:
                self.cache[name] = e
        v = self.cache[name]
        if isinstance(v, VarError):
            # the line of `VAR_X = "..."` if there is one: that is where the customer fixes it
            self.add(VAR_RULE.get(v.rule, 'E208'), self.consts.get(name, self.line), v.message, name)
            return None
        return v

    def vline(self, name, line):
        return self.consts.get(name, line)

    def _api(self):
        d = self
        api = {}

        def declared(fn):
            """Registers fn as API function, counts and records every call."""
            name = fn.__name__
            fn.__qualname__ = name          # a missing argument reads `write() missing ...`

            def call(*a, **k):
                d._enter(name, a)
                return fn(*a, **k)
            api[name] = call
            return fn

        def declaring(call):
            if d.started:
                d.add('E307', d.line, f'{call}() must come before {"capture_start" if d.mode == "capture" else "stream_start"}()', call)
                return False
            return True

        def in_mode(want, call):
            """The first call of either kind sets the mode, the other kind is E315."""
            if d.mode not in (None, want):
                d.add('E315', d.line, f'{call}(): stream calls and capture calls cannot be mixed in one script', call)
                return False
            d.mode = want
            return True

        def number(*xs):
            if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in xs):
                return True
            d.add('E312', d.line, f'a number is expected, got {", ".join(repr(x) for x in xs)}')
            return False

        def fmt_ok(fmt, args):
            try:
                fmt % args
                return True
            except (TypeError, ValueError) as e:
                d.add('E311', d.line, f'format string {fmt!r} and its arguments do not fit: {e}')
                return False

        def note_var(v, written):
            if v.width == 4:
                d.add('W202', d.vline(v.name, d.line), f'{v.name} is {v.ctype or "32 bit"} wide: a 32-bit value is '
                      'read in two parts and can tear', v.name)
            if written and v.is_volatile is False:
                d.add('W201', d.vline(v.name, d.line), f'{v.name} is written but not volatile in C: the compiler may keep '
                      'it in a register', v.name)

        @declared
        def channels(*names):
            if not declaring('channels') or not in_mode('stream', 'channels'):
                return
            d.chans_line = d.line
            if len(names) > tune_api.MAX_CHANNELS:
                d.add('E212', d.line, f'{len(names)} channels, at most {tune_api.MAX_CHANNELS}')
            d.chans = [v for v in (d.var(n) for n in names) if v]
            for v in d.chans:
                note_var(v, False)

        @declared
        def allow(name, low, high):
            if not declaring('allow'):
                return
            v = d.var(name)
            if not v or not number(low, high):
                return
            note_var(v, True)
            if not low <= high:
                d.add('E221', d.line, f'allow("{name}", {low}, {high}): min is above max')
            elif v.type in tune_api.TYPE_RANGE and not (tune_api.TYPE_RANGE[v.type][0] <= low
                                                        and high <= tune_api.TYPE_RANGE[v.type][1]):
                d.add('E220', d.line, f'allow("{name}", {low}, {high}): outside the range of {v.type}')
            d.allowed[v.name] = (low, high, d.line)

        @declared
        def safe_value(name, value):
            if not declaring('safe_value'):
                return
            v = d.var(name)
            if v and number(value):
                d.safe[v.name] = (v, value, d.line)

        @declared
        def limit(name, most):
            if not declaring('limit'):
                return
            v = d.var(name)
            if not v or not number(most):
                return
            if not most >= 0:
                d.add('E226', d.line, f'limit("{name}", {most}): compares the magnitude, the limit must not be negative')
            d.limits[v.name] = (most, d.line)
            # W203 for a script in capture mode comes with capture_start(), see capture_checks()

        @declared
        def stream_start():
            if not in_mode('stream', 'stream_start'):
                return
            if d.started:
                d.add('E309', d.line, 'stream_start() was already called')
                return
            d.stream_checks(d.line)
            d.started = True

        @declared
        def write(name, value):
            v = d.var(name)
            if not v or not number(value):
                return
            if v.name not in d.allowed:
                d.add('E303', d.line, f'write("{name}"): not allowed, it needs allow("{name}", min, max)', v.name)
            else:
                lo, hi, _ = d.allowed[v.name]
                if lo <= hi and not lo <= value <= hi:
                    d.add('E301', d.line, f'write("{name}", {value}): outside allow() {lo} ... {hi}', v.name)
            if not d.started:
                d.add('E306', d.line, f'write() before {"capture_start" if d.mode == "capture" else "stream_start"}()')

        @declared
        def read(name):
            v = d.var(name)
            # in check_idle() the target is at standstill and not streaming: any variable of the ELF can be read
            if v and d.phase == 'check_idle':
                if v.name not in d.idle_reads:
                    d.idle_reads.append(v.name)
            elif d.mode == 'capture':
                d.add('E314', d.line, f'read("{name}"): read() does not work in capture mode, the target sends the buffer '
                      'after the capture. Use capture_value() or capture_final()')
            elif v:
                if v.name not in [c.name for c in d.chans]:
                    d.add('E302', d.line, f'read("{name}"): not in channels()', v.name)
                elif not d.started:
                    d.add('E306', d.line, f'read("{name}"): no sample yet, read() needs stream_start()')
            return 0

        @declared
        def wait_ms(ms):
            number(ms)

        @declared
        def trace_begin():
            if not in_mode('stream', 'trace_begin'):
                return
            if not d.started:
                d.add('E306', d.line, 'trace_begin() before stream_start()')
            if d.trace_open:
                d.add('E308', d.line, 'trace_begin() without trace_end() of the last trace')
            d.trace_open = True

        @declared
        def trace_end(fmt, *args):
            fmt_ok(fmt, args)
            if not in_mode('stream', 'trace_end'):
                return
            if not d.trace_open:
                d.add('E308', d.line, 'trace_end() without trace_begin()')
            d.trace_open = False

        @declared
        def trace_failed():
            if d.mode == 'capture':         # marks the capture that capture_save() stores next
                if d.cap_state != 'have':
                    d.add('E308', d.line, 'trace_failed() without a capture: capture_wait() has to return first')
                return
            if not d.trace_open:
                d.add('E308', d.line, 'trace_failed() without trace_begin()')

        @declared
        def table(*names):
            if d.columns is not None:
                d.add('E305', d.line, 'table() was already called, a run has one table')
            elif not names:
                d.add('E305', d.line, 'table() needs column names')
            else:
                d.columns = list(names)

        @declared
        def row(*values):
            if d.columns is None:
                d.add('E305', d.line, 'row() without table()')
            elif len(values) != len(d.columns):
                d.add('E304', d.line, f'row() has {len(values)} values, table() has {len(d.columns)} columns')

        @declared
        def summary_channel(name):
            d.var(name)

        @declared
        def summary_text(fmt, *args):
            fmt_ok(fmt, args)

        @declared
        def summary_mark(value):
            pass

        @declared
        def unit(name, factor, text):
            v = d.var(name)
            if v:
                d.units.add(v.name)

        @declared
        def define(name, value):
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_]\w*', name):
                d.add('E310', d.line, f'define("{name}"): not a C name')

        @declared
        def printf(fmt, *args):
            fmt_ok(fmt, args)

        @declared
        def expect(condition, text):
            pass                            # false is no error here: read() gives 0 on the fake target

        api['Q15'] = tune_api.Q15           # pure function
        api['RISING'], api['FALLING'] = tune_api.RISING, tune_api.FALLING

        # -- capture mode: the dry run keeps a state (None, 'armed', 'have') to find calls out of order. capture_wait()
        # returns 0, every value is 0.

        def capture_call(call):
            """False, with E315, in a script that streams."""
            return in_mode('capture', call)

        def need_capture(call, name=None):
            if d.mode is None or not d.started:
                d.add('E306', d.line, f'{call}() before capture_start()', call)
                return False
            if d.cap_state != 'have':
                d.add('E316', d.line, f'{call}(): no capture yet, capture_arm() and capture_wait() have to come first', call)
                return False
            if name is not None:
                v = d.var(name)
                if v and v.name not in [c.name for c in d.chans]:
                    d.add('E302', d.line, f'{call}("{name}"): not in capture_channels()', v.name)
                    return False
            return True

        @declared
        def capture_channels(*names):
            if not declaring('capture_channels') or not capture_call('capture_channels'):
                return
            d.chans_line = d.line
            if len(names) > tune_api.MAX_CAPTURE_CHANNELS:
                d.add('E212', d.line, f'{len(names)} capture channels, at most {tune_api.MAX_CAPTURE_CHANNELS}')
            d.chans = [v for v in (d.var(n) for n in names) if v]
            for v in d.chans:
                note_var(v, False)

        @declared
        def capture_period_us(us):
            if not declaring('capture_period_us') or not capture_call('capture_period_us') or not number(us):
                return
            if us != int(us) or not 1 <= us <= 65535:
                d.add('E227', d.line, f'capture_period_us({us}): a whole number of microseconds from 1 to 65535')
            elif us < tune_api.CAPTURE_MIN_US:
                d.add('E229', d.line, f'capture_period_us({us}): below {tune_api.CAPTURE_MIN_US} us mini-scope does not hold its period '
                      f'(at 1 to 2 us the target hangs). Use {tune_api.CAPTURE_MIN_US} us or more')
            else:
                if us < tune_api.CAPTURE_WARN_US:
                    d.add('W206', d.line, f'capture_period_us({us}): may not hold under interrupt load. Measured with a 60 % control '
                          'interrupt above mini-scope: 50 us at 32 MIPS, 100 us at 100 MIPS; without load 15 us and 5 us. '
                          'A period it cannot hold gives wrong spacing without any error')
                d.cap_period = int(us)

        @declared
        def capture_trigger(name, edge, level, pre_percent):
            if not declaring('capture_trigger') or not capture_call('capture_trigger'):
                return
            v = d.var(name)
            ok = number(level, pre_percent)
            if v and v.type == 'f32':
                d.add('E218', d.line, f'capture_trigger("{name}"): {v.name} is a float, the target compares integers')
            if edge not in (tune_api.RISING, tune_api.FALLING):
                d.add('E228', d.line, f'capture_trigger("{name}"): the edge must be RISING or FALLING')
            if ok and not 0 <= pre_percent <= 100:
                d.add('E228', d.line, f'capture_trigger("{name}"): the pretrigger is 0 to 100 percent, not {pre_percent}')
            if ok and v and v.type in tune_api.TYPE_RANGE:
                lo, hi = tune_api.TYPE_RANGE[v.type]
                if level != int(level) or not lo <= level <= hi:
                    d.add('E228', d.line, f'capture_trigger("{name}", {level}): the level must be a whole number inside the range of {v.type}')
            if v:
                d.cap_trigger = (v, d.line)

        @declared
        def capture_start():
            if not capture_call('capture_start'):
                return
            if d.started:
                d.add('E309', d.line, 'capture_start() was already called')
                return
            d.capture_checks(d.line)
            d.started = True

        @declared
        def capture_arm():
            if not capture_call('capture_arm'):
                return
            if not d.started:
                d.add('E306', d.line, 'capture_arm() before capture_start()')
                return
            if d.cap_state == 'armed':
                d.add('E316', d.line, 'capture_arm() twice: capture_wait() is missing')
            d.cap_state = 'armed'

        @declared
        def capture_wait(timeout_ms):
            number(timeout_ms)
            if not capture_call('capture_wait'):
                return 0
            if not d.started:
                d.add('E306', d.line, 'capture_wait() before capture_start()')
            elif d.cap_state != 'armed':
                d.add('E316', d.line, 'capture_wait() without capture_arm()')
            else:
                d.cap_state = 'have'
            return 0

        @declared
        def capture_max(name):
            if capture_call('capture_max'):
                need_capture('capture_max', name)
            return 0

        @declared
        def capture_final(name):
            if capture_call('capture_final'):
                need_capture('capture_final', name)
            return 0

        @declared
        def capture_rise_us(name):
            if capture_call('capture_rise_us'):
                need_capture('capture_rise_us', name)
            return 0

        @declared
        def capture_length():
            if capture_call('capture_length'):
                need_capture('capture_length')
            return 0

        @declared
        def capture_value(name, i):
            if capture_call('capture_value') and need_capture('capture_value', name):
                number(i)
            return 0

        @declared
        def capture_save(fmt, *args):
            fmt_ok(fmt, args)
            if capture_call('capture_save'):
                need_capture('capture_save')

        for k in tune_api.__all__:          # a name of the API without a recorder is a mistake of this file
            assert k in api, k
        return api

    # -- checks that need the whole declaration -------------------------------------------------

    def stream_checks(self, line):
        """What Session._refusal() refuses at stream_start(), for all causes at once."""
        if not self.allowed:
            self.add('E214', line, 'no allow(): a script must declare what it may write')
        if not self.safe:
            self.add('E215', line, 'no safe_value(): a script must declare what stops the motor')
        if not self.chans:
            self.add('E216', line, 'no channels()')
        self.declaration_checks()
        frame = sum(v.width for v in self.chans) + 2        # as Session._load: sync + seq + channels
        load = frame * 1e6 / tune_api.PERIOD_US / (self.baud / 10)
        if load > tune_api.MAX_LOAD:
            self.add('E213', line, f'the stream needs {load:.0%} of the UART, at most {tune_api.MAX_LOAD:.0%}')
        if self.var_quiet(tune_api.ALIVE) is None:
            self.add('E217', line, f'{tune_api.ALIVE} is not in the ELF, the firmware needs the heartbeat variable')

    def capture_checks(self, line):
        """What Session._refusal() refuses at capture_start(), for all causes at once."""
        if not self.allowed:
            self.add('E214', line, 'no allow(): a script must declare what it may write')
        if not self.safe:
            self.add('E215', line, 'no safe_value(): a script must declare what stops the motor')
        if not self.chans:
            self.add('E216', line, 'no capture_channels()')
        if self.cap_trigger is None:
            self.add('E228', line, 'no capture_trigger(): a capture needs a trigger channel, edge, level and pretrigger')
        elif self.cap_trigger[0].name not in [v.name for v in self.chans]:
            self.add('E228', self.cap_trigger[1], f'capture_trigger("{self.cap_trigger[0].name}") on a variable that is not '
                     'in capture_channels()', self.cap_trigger[0].name)
        self.declaration_checks()
        for name, (_, lline) in self.limits.items():
            self.add('W203', lline, f'limit("{name}") in capture mode is only checked after the transfer of the buffer, '
                     'not while the capture runs', name)
        ms = (tune_api.CAPBUF + 5) / (self.baud / 10) * 1000
        if ms > tune_api.TRANSFER_MAX_MS:
            self.add('E219', line, f'the transfer of a capture takes {ms:.0f} ms at {self.baud} baud, at most '
                     f'{tune_api.TRANSFER_MAX_MS} ms: nothing is supervised meanwhile')
        if self.var_quiet(tune_api.ALIVE) is None:
            self.add('E217', line, f'{tune_api.ALIVE} is not in the ELF, the firmware needs the heartbeat variable')

    def var_quiet(self, name):
        try:
            return self.table.lookup(name)
        except VarError:
            return None

    def declaration_checks(self):
        for name, (v, value, line) in self.safe.items():
            if name not in self.allowed:
                self.add('E223', line, f'safe_value("{name}") on a variable without allow()', name)
                continue
            lo, hi, _ = self.allowed[name]
            if lo <= hi and not lo <= value <= hi:
                self.add('E222', line, f'safe_value("{name}", {value}) is outside allow() {lo} ... {hi}', name)
            if v.type != 'f32' and value != int(value):
                self.add('E225', line, f'safe_value("{name}", {value}) is not a whole number', name)
        names = [v.name for v in self.chans]
        for name, (_, line) in self.limits.items():
            if name not in names:
                self.add('E224', line, f'limit("{name}") on a variable that is not in channels()', name)

    def _end_checks(self):
        """After a main() that ended normally: warnings that need the complete declaration."""
        if not self.started:
            self.declaration_checks()
        for name, (_, _, line) in self.allowed.items():
            if name not in self.safe:
                self.add('W204', self.vline(name, line), f'{name} may be written but has no safe_value(): an abort '
                         'does not reset it', name)
        for v in self.chans:
            if v.name not in self.units:
                self.add('W205', self.vline(v.name, self.chans_line), f'{v.name} is measured but has no unit(), '
                         'shown as raw', v.name)
