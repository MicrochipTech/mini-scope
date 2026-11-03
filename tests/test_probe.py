"""test_probe.py - stage 4 of "Script errors" (the target probe of gui/tune_api.py) against sim.dll.

    python tests/test_probe.py                  all tests
    python tests/test_probe.py good overwrite   only the tests with these names (see TESTS at the end)

A scenario is a function that returns (passed, text). The test asserts it. A mutation test (mut_*) breaks the
code under test on purpose and asserts that the same scenario fails: it shows that the test can fail.
"""
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, '..', 'gui'), os.path.join(HERE, '..', 'tools')]
import tune_api                                   # noqa: E402
from elf_vars import VarTable                     # noqa: E402
from scope_gui import Scope                       # noqa: E402
from sim_serial import SimSerial, SYMBOLS, SYMBOL_TYPES   # noqa: E402

TABLE = VarTable.from_symbols(SYMBOLS, SYMBOL_TYPES)
ADDR = {n: a for n, (a, _) in SYMBOLS.items()}
RUN, RAMP, ALIGN, HAND = ADDR['motor.run'], ADDR['startup.ramp_rate'], ADDR['startup.align_current'], ADDR['startup.handover_speed']
ALIVE, CURRENT = ADDR['tune_alive'], ADDR['motor.current']
ADDR_KP, ADDR_KI, ADDR_REF = ADDR['motor.current_kp'], ADDR['motor.current_ki'], ADDR['motor.current_ref']
TMP = tempfile.mkdtemp(prefix='tune_probe_')
FIXTURE = os.path.join(HERE, '..', 'scripts', 'tune_startup.py')
with open(FIXTURE, encoding='utf-8') as f:
    STARTUP = f.read()
threading.excepthook = lambda a: None           # Scope's reader dies with the port in the port error test
tune_api.TRIAL_MS = 300


def check(cond, text):
    print(('OK   ' if cond else 'FAIL ') + text)
    if not cond:
        check.failed = True


class LogSerial(SimSerial):
    """Logs at the port what the target receives: (time, kind, address, value), kind W or a command byte.
    silent: nothing comes back. broken: every access raises."""
    silent = broken = False

    def __init__(self):
        super().__init__()
        self.log = []

    def _bad(self):
        if self.broken:
            raise OSError('device disappeared')

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

    def write(self, data):
        self._bad()
        b, now = bytes(data), time.perf_counter()
        if b[:1] == b'W':
            for i in range(0, len(b), 8):
                addr, width = struct.unpack('<HB', b[i + 1:i + 4])
                self.log.append((now, 'W', addr, int.from_bytes(b[i + 4:i + 4 + width], 'little', signed=False)))
        else:
            self.log.append((now, chr(b[0]), None, None))
        return super().write(b)

    def writes(self, addr):
        return [x[3] for x in self.log if x[1] == 'W' and x[2] == addr]

    def writes_after(self, index, addr):
        return [x[3] for x in self.log[index:] if x[1] == 'W' and x[2] == addr]


def script(*edits, src=STARTUP, name='t.py'):
    """The tune_startup.py fixture with (old, new) replacements, in a folder of its own."""
    for old, new in edits:
        assert old in src, old
        src = src.replace(old, new)
    folder = tempfile.mkdtemp(dir=TMP)
    path = os.path.join(folder, name)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(src)
    return path


def line_of(path, text):
    with open(path, encoding='utf-8') as f:
        return next(i for i, x in enumerate(f, 1) if text in x)


def when_written(ser, addr, fn):
    """Calls fn as soon as the target has received a write to addr."""
    def watch():
        end = time.time() + 60
        while time.time() < end:
            if ser.writes(addr):
                fn()
                return
            time.sleep(0.002)
    threading.Thread(target=watch, daemon=True).start()


def run_it(path, ser=None, full=False, hooks=None, ping=True):
    """probe() or run() with a Scope on ser. -> (result, events, Scope, run.txt text)."""
    ser = ser or LogSerial()
    sc = Scope(ser)
    if ping:
        sc.ping()
    ev = []
    out = os.path.join(os.path.dirname(path), 'runs')
    if full:
        s = tune_api.run(path, sc, TABLE, on_event=ev.append, out_dir=out, hooks=hooks)
        r = s.wait(180)
    else:
        r = tune_api.probe(path, sc, TABLE, on_event=ev.append, out_dir=out, hooks=hooks)
    assert r is not None, 'the run did not end'
    with open(os.path.join(r['folder'], 'run.txt'), encoding='utf-8') as f:
        txt = f.read()
    return r, ev, sc, txt


def peek(sc, *names):
    """Current values in the model, by a short stream."""
    if sc.start([(ADDR[n], SYMBOL_TYPES[n]) for n in names], 0, 1000):
        return None
    time.sleep(0.1)
    _, cols = sc.snapshot()
    sc.stop()
    return [int(c[-1]) for c in cols]


def states(ev):
    return [e['state'] for e in ev if e['type'] == 'state']


def stage4(codes):
    return {k: v for k, v in codes.items() if k[1] == '4'}


# -- the probe on a good script ----------------------------------------------------------------------------------

def test_good():
    path = script()
    ser = LogSerial()
    t0 = time.perf_counter()
    r, ev, sc, txt = run_it(path, ser)
    secs = time.perf_counter() - t0
    check(r['status'] == 'ok' and not r['errors'], f'good script: {r["status"]} {r["reason"]} ({secs:.1f} s on SIM)')
    check(states(ev) == ['starting', 'probe', 'safe_end', 'done'], f'states: {states(ev)}')
    check(stage4(r['rule_codes']) == {}, f'no stage 4 code: {r["rule_codes"]}')
    var_ev = [e for e in ev if e['type'] == 'variables']
    check(len(var_ev) == 1 and var_ev[0]['rows'] == r['rows'], 'one variables event, the same rows as the result')
    by = {x['name']: x for x in r['rows']}
    want = {'motor.speed': 'measured, idle', 'motor.iq': 'measured, limit', 'motor.state': 'measured, idle',
            'motor.fault': 'measured, idle', 'motor.run': 'written, safe', 'startup.ramp_rate': 'written',
            'startup.align_current': 'written', 'startup.handover_speed': 'written'}
    check({n: x['role'] for n, x in by.items()} == want, f'roles: {[(n, x["role"]) for n, x in by.items()]}')
    check(by['startup.ramp_rate']['value'] == 100 and by['startup.ramp_rate']['address'] == RAMP
          and by['startup.ramp_rate']['type'] == 'u16', f'row: {by["startup.ramp_rate"]}')
    log = ser.log
    first_w = next(x for x in log if x[1] == 'W')
    before = log[:log.index(first_w)]
    check(first_w[2] == ALIVE and set(x[1] for x in before) <= {'P', 'C', 'R', 'S'} and [x[1] for x in before].count('C') == 1,
          f'before the first heartbeat only the ping and the one read-only stream of the build id reached the target: {[x[1] for x in before]}')
    first_c = next(x for x in log[log.index(first_w):] if x[1] == 'C')
    check(first_c[0] - first_w[0] >= 0.05, f'first read {1000 * (first_c[0] - first_w[0]):.0f} ms after the first heartbeat (>= 50)')
    check(ser.writes(RAMP) == [101, 100] and ser.writes(ALIGN) == [401, 400] and ser.writes(HAND) == [601, 600],
          f'write test: +1 and restore: {ser.writes(RAMP)} {ser.writes(ALIGN)} {ser.writes(HAND)}')
    check(ser.writes(RUN) == [0, 0], f'motor.run (has safe_value) is only written with the safe value, twice: {ser.writes(RUN)}')
    check(peek(sc, 'startup.ramp_rate', 'startup.align_current', 'startup.handover_speed', 'motor.run') == [100, 400, 600, 0],
          'the model has its values back')
    check('write' in txt and 'startup.ramp_rate = 101 (probe)' in txt and 'startup.ramp_rate = 100 (probe)' in txt,
          'run.txt has the probe writes')
    n = len(ser.log)
    time.sleep(0.5)
    check(len(ser.log) == n, 'the heartbeat stopped with the end of the probe')
    check('safe values read back: 1 of 1 correct' in '\n'.join(e['text'] for e in ev if e['type'] == 'log'), 'safe values read back')


# -- check_idle ---------------------------------------------------------------------------------------------------

def scenario_expect(inverted):
    """Fault flag or state value wrong: the run stops at the expect line, the motor never starts."""
    ok = True
    for old, new, text in (('return read(VAR_FAULT) != 0', 'return read(VAR_FAULT) == 0', 'expect(not has_fault()'),
                           ('return read(VAR_STATE) == 3', 'return read(VAR_STATE) == 0', 'expect(not in_closed_loop()')):
        path = script((old, new))
        ser = LogSerial()
        r, ev, sc, txt = run_it(path, ser, full=True)
        line = line_of(path, text)
        good = (r['status'] == 'error' and r['reason'].startswith(f't.py:{line}: error: ') and r['rule_codes'].get('E410') == 1
                and 'trial' not in states(ev) and set(ser.writes(RUN)) <= {0} and not ser.writes(RAMP))
        print(f'       {text}: {r["status"]} {r["reason"]!r} codes {stage4(r["rule_codes"])} run writes {ser.writes(RUN)}')
        ok = ok and good
    return ok


def test_expect():
    check(scenario_expect(True), 'inverted fault flag and wrong state value: error at the expect line, no write to motor.run but the safe 0')


def test_mut_expect():
    with mock.patch.object(tune_api.Session, '_probe_idle', lambda self: None):
        check(not scenario_expect(True), 'MUTATION check_idle() not run: the scenario fails')


def test_idle_rules():
    cases = [('E412', 'write("motor.run", 1)', 'in check_idle()'),
             ('E411', 'x = 1 / (read("startup.ramp_rate") - 100)', 'ZeroDivisionError'),
             ('E411', 'read("motor.kp") if read("startup.ramp_rate") == 100 else 0', 'has not read'),
             ('E411', 'while read("motor.state") == 0:\n        pass', 'more than')]
    for code, body, want in cases:
        path = script(('    expect(stands_still(), "motor must stand still: check VAR_SPEED")',
                       '    ' + body.replace('\n', '\n    ')))
        ser = LogSerial()
        r, ev, sc, txt = run_it(path, ser)
        line = line_of(path, body.split('\n')[0])
        check(r['status'] == 'error' and r['rule_codes'].get(code) == 1 and want in r['reason']
              and r['reason'].startswith(f't.py:{line}: error: ') and not ser.writes(RAMP) and set(ser.writes(RUN)) <= {0},
              f'check_idle {code}: {r["reason"][:100]!r}')
    # module level code: once in the session (the dry runs execute it on the recorder, not on the target)
    marker = os.path.join(TMP, 'marker.txt')
    path = script(('from tune_api import *\n', f'from tune_api import *\nopen({marker!r}, "a").write("x")\n'))
    run_it(path)
    with open(marker) as f:
        n = len(f.read())
    check(n == 3, f'module level code ran {n} times for a probe: dry run of the check, dry run of the probe, the session once')


# -- the write test ----------------------------------------------------------------------------------------------

def scenario_overwrite():
    """allow() on a variable the firmware computes every tick: detected, restored, the others still tested."""
    path = script(('    allow(VAR_RUN, 0, 1) ', '    allow(VAR_RUN, 0, 1) ')) if False else script(
        ('    allow(VAR_RUN, 0, 1)', '    allow("motor.current", -1000, 1000)\n    allow(VAR_RUN, 0, 1)'))
    ser = LogSerial()
    r, ev, sc, txt = run_it(path, ser)
    line = line_of(path, 'allow("motor.current"')
    msg = next((x for x in r['errors'] if 'E420' in x), '')
    msg = next((e['text'] for e in ev if e['type'] == 'log' and '[E420]' in e['text']), '')
    good = (r['status'] == 'error' and r['rule_codes'].get('E420') == 1 and r['reason'].startswith(f't.py:{line}: error: motor.current')
            and 'overwrites' in msg and 'offset' in msg
            and ser.writes(RAMP) == [101, 100] and ser.writes(HAND) == [601, 600] and ser.writes(CURRENT)[-1] == 0
            and peek(sc, 'startup.ramp_rate', 'startup.align_current', 'startup.handover_speed') == [100, 400, 600])
    print(f'       {r["status"]} {r["reason"]!r}\n       codes {stage4(r["rule_codes"])}, writes ramp {ser.writes(RAMP)} current {ser.writes(CURRENT)}')
    return good


def test_overwrite():
    check(scenario_overwrite(), 'a variable the firmware overwrites: E420 at its allow() line, text names the cure, the others tested and restored')


def test_mut_overwrite():
    def blind(self, v, orig, new, line):
        self._s_write(v, new, orig)
        self._s_wait(self._latest() + self._s_n(tune_api.HOLD_MS))
    with mock.patch.object(tune_api.Session, '_change_and_hold', blind):
        check(not scenario_overwrite(), 'MUTATION hold not checked: the scenario fails')


def test_single_value():
    path = script(('    allow(VAR_HANDOVER_SPEED, 100, 2000)', '    allow(VAR_HANDOVER_SPEED, 600, 600)'))
    ser = LogSerial()
    r, ev, sc, txt = run_it(path, ser)
    check(r['status'] == 'ok' and r['rule_codes'].get('W401') == 1 and not ser.writes(HAND),
          f'allow() with one value: warning W401, variable not written ({r["status"]}, {r["rule_codes"].get("W401")})')


def test_near_range():
    # at the top of the range -1 is used; a value outside allow() with no neighbour inside is skipped with W402
    src = '''from tune_api import *


def check_idle():
    expect(read("motor.state") == 0, "state")


def main():
    channels("motor.speed", "motor.state")
    allow("startup.ramp_rate", 1, 100)
    allow("startup.align_current", 900, 1000)
    allow("motor.run", 0, 1)
    safe_value("motor.run", 0)
    stream_start()
    wait_ms(10)
'''
    ser = LogSerial()
    r, ev, sc, txt = run_it(script(src=src), ser)
    check(r['status'] == 'ok' and ser.writes(RAMP) == [99, 100] and r['rule_codes'].get('W402') == 1 and not ser.writes(ALIGN),
          f'top of the range uses -1 {ser.writes(RAMP)}, value outside allow() skipped with W402 {r["rule_codes"].get("W402")}')


# -- abort, port error, silent port, limit during the write test ------------------------------------------------------

def scenario_restore(kind):
    """The probe ends in the middle of the write test: the changed variable is back before the safe values."""
    path = script()
    ser = LogSerial()
    if kind == 'abort':
        when_written(ser, RAMP, lambda: tune_api.abort('test'))
    elif kind == 'silent':
        when_written(ser, RAMP, lambda: setattr(ser, 'silent', True))
    elif kind == 'broken':
        when_written(ser, RAMP, lambda: setattr(ser, 'broken', True))
    r, ev, sc, txt = run_it(path, ser)
    ser.silent = ser.broken = False
    sc.stop()               # the DLL is shared: a stream left running there would spoil the next test
    log = ser.log
    i_change = next(i for i, x in enumerate(log) if x[1] == 'W' and x[2] == RAMP)
    after = ser.writes_after(i_change, RAMP)
    i_safe = next((i for i, x in enumerate(log) if i > i_change and x[1] == 'W' and x[2] == RUN), None)
    i_back = next((i for i, x in enumerate(log) if i > i_change and x[1] == 'W' and x[2] == RAMP and x[3] == 100), None)
    errors = ' | '.join(r['errors'])
    print(f'       {kind}: {r["status"]} {r["reason"]!r}\n       errors {errors!r}, ramp writes after the change {after}, '
          f'restore at port entry {i_back}, first safe write {i_safe}')
    if kind == 'broken':        # nothing can be written: reported with name and both values, at once in the log
        logged = any('startup.ramp_rate was changed from 100 to 101' in e['text'] for e in ev if e['type'] == 'log')
        return r['status'] == 'aborted' and 'port error' in r['reason'] and logged and 'could not be restored' in errors
    good = r['status'] == 'aborted' and i_back is not None and (i_safe is None or i_back < i_safe) and not r['errors'] \
        if kind == 'abort' else r['status'] == 'aborted' and i_back is not None and (i_safe is None or i_back < i_safe)
    return good and peek(sc, 'startup.ramp_rate') == [100]


def test_abort():
    check(scenario_restore('abort'), 'abort in the write test: restored before the safe values, read back, no error')


def test_mut_restore():
    orig_send, orig_verify = tune_api.Session._send_safe, tune_api.Session._verify_restore

    def send_without_restore(self, t0):
        self.pending.clear()
        return orig_send(self, t0)
    with mock.patch.object(tune_api.Session, '_send_safe', send_without_restore):
        check(not scenario_restore('abort'), 'MUTATION no restore on abort: the scenario fails')
    assert tune_api.Session._verify_restore is orig_verify


def test_port_error():
    check(scenario_restore('broken'), 'port error in the write test: an error with name and both values, status aborted')


def test_silent():
    check(scenario_restore('silent'), 'port silent in the write test: stream stall ends it, the restore is still written first')


def test_limit():
    # the written variable is also a limit variable, with its current value as the limit: the +1 trips it
    src = '''from tune_api import *


def check_idle():
    expect(read("motor.state") == 0, "state")


def main():
    channels("motor.speed", "startup.align_current")
    allow("startup.align_current", 0, 1000)
    allow("motor.run", 0, 1)
    safe_value("motor.run", 0)
    limit("startup.align_current", 400)
    stream_start()
    wait_ms(10)
'''
    path = script(src=src)
    ser = LogSerial()
    r, ev, sc, txt = run_it(path, ser)
    log = ser.log
    i_change = next(i for i, x in enumerate(log) if x[1] == 'W' and x[2] == ALIGN)
    i_back = next((i for i, x in enumerate(log) if i > i_change and x[1] == 'W' and x[2] == ALIGN and x[3] == 400), None)
    i_safe = next((i for i, x in enumerate(log) if i > i_change and x[1] == 'W' and x[2] == RUN), None)
    check(r['status'] == 'aborted' and r['reason'].startswith('limit exceeded: startup.align_current = 401'),
          f'limit variable in the group trips: {r["status"]} {r["reason"]!r}')
    check(i_back is not None and i_safe is not None and i_back < i_safe and peek(sc, 'startup.align_current') == [400],
          'limit in the write test: restored before the safe values')


# -- ping, build id ----------------------------------------------------------------------------------------------------

def test_no_ping():
    path = script()
    ser = LogSerial()
    ser.silent = True
    r, ev, sc, txt = run_it(path, ser, ping=False)
    check(r['status'] == 'error' and r['rule_codes'].get('E401') == 1 and 'TROUBLESHOOTING.md' in r['reason'],
          f'no ping answer: E401 naming TROUBLESHOOTING.md: {r["reason"][:80]!r}')
    check([x[1] for x in ser.log] == ['P'], f'nothing but the ping at the port: {[x[1] for x in ser.log]}')
    check('safe values not written' in '\n'.join(e['text'] for e in ev if e['type'] == 'log'), 'no safe values: the target was not verified')


def test_build_id():
    path = script()
    ser = LogSerial()
    with mock.patch.object(tune_api, 'build_id_check', lambda s: 'wrong firmware'):
        r, ev, sc, txt = run_it(path, ser)
    check(r['status'] == 'error' and r['rule_codes'].get('E402') == 1 and {x[1] for x in ser.log} == {'P'},
          f'build id mismatch: E402, only the ping reached the target ({[x[1] for x in ser.log]})')


def test_refused():
    path = script(('wait_ms(POLL_MS)\n        time_ms += POLL_MS\n\n    return -1\n\n\n# Returns 0', 'wait_ms(POLL_MS)\n        time_ms += POLL_MS\n\n    return -1\n\n\n# Returns 0'),
                  ('def main():', 'def main():\n    wirte("motor.run", 1)'))
    ser = LogSerial()
    sc = Scope(ser)
    try:
        tune_api.probe(path, sc, TABLE)
        check(False, 'probe() refuses a script with an error')
    except tune_api.RunRefused as e:
        check(ser.log == [] and tune_api._active is None, f'stages 1 to 3 with errors: RunRefused, nothing sent ({str(e)[:60]!r})')


# -- heartbeat across the probe, the whole sweep -----------------------------------------------------------------

def scenario_second_run(wait_s=1.6):
    """The heartbeat of run 1 stopped, the target latched fault 5: run 2 clears it before check_idle()."""
    src = '''from tune_api import *


def check_idle():
    expect(read("motor.fault") == 0, "no fault expected")
    expect(read("motor.state") == 0, "state 0 expected")


def main():
    channels("motor.speed", "motor.fault")
    allow("motor.run", 0, 1)
    safe_value("motor.run", 0)
    stream_start()
    wait_ms(50)
'''
    ser = LogSerial()
    sc = Scope(ser)
    sc.ping()
    out = os.path.join(TMP, 'second')
    results = []
    latched = None
    for n in (1, 2):
        s = tune_api.run(script(src=src), sc, TABLE, out_dir=out)
        results.append(s.wait(60))
        if n == 1:
            time.sleep(wait_s)
            latched = peek(sc, 'motor.fault')
    print(f'       run 1 {results[0]["status"]}, fault after {wait_s} s without heartbeat: {latched}, run 2 {results[1]["status"]} {results[1]["reason"]!r}')
    return latched == [5] and results[0]['status'] == 'ok' and results[1]['status'] == 'ok'


def test_second_run():
    check(scenario_second_run(), 'second run right after the first: fault 5 was latched, check_idle() passes')


def test_mut_heartbeat():
    start = tune_api.Session._start_heartbeat

    def only_in_stream_start(self):         # the heartbeat as before the probe existed
        if self.started:
            start(self)
        else:
            self.hb_first.set()
    with mock.patch.object(tune_api.Session, '_start_heartbeat', only_in_stream_start):
        check(not scenario_second_run(), 'MUTATION heartbeat only from stream_start(): the second run fails at check_idle()')


def test_sweep():
    # baseline: the same script without the probe; then with it
    res = {}
    for name, hooks in (('without probe', {'skip_probe': True}), ('without probe, again', {'skip_probe': True}), ('with probe', None)):
        path = script()
        ser = LogSerial()
        r, ev, sc, txt = run_it(path, ser, full=True, hooks=hooks)
        with open(os.path.join(r['folder'], 'results.csv'), encoding='utf-8') as f:
            csv_text = f.read()
        with open(r['tuning_h'], encoding='utf-8') as f:
            h = [x for x in f.read().splitlines() if x.startswith('#define') and 'TUNING' not in x]
        beats = [x[0] for x in ser.log if x[1] == 'W' and x[2] == ALIVE]
        gaps = sorted(((b - a) * 1000 / sc.time_scale, i) for i, (a, b) in enumerate(zip(beats, beats[1:])))[::-1]
        res[name] = (r, csv_text, h)
        check(r['status'] == 'ok', f'{name}: {r["status"]} {r["reason"]}')
        print(f'       {name}: {len(beats)} heartbeats, three largest gaps {[round(g) for g, _ in gaps[:3]]} ms target time, '
              f'states {states(ev)}')
        spans = [min((nb for nb in beats if nb >= tc), default=tc) - max((pb for pb in beats if pb <= tc), default=tc)
                 for tc in [x[0] for x in ser.log if x[1] == 'C']]
        print(f'       {name}: {len(spans)} stream starts, the gap around a stream start is at most {1000 * max(spans) / sc.time_scale:.0f} ms')
        check(1000 * max(spans) / sc.time_scale < 1000, f'{name}: gap around every stream start below 1000 ms')
        check(gaps[0][0] < 1000, f'{name}: largest gap {gaps[0][0]:.0f} ms < 1000 ms (timeout of the target)')
        if name == 'with probe':
            check(states(ev) == ['starting', 'probe', 'trial', 'running', 'safe_end', 'done'], f'states: {states(ev)}')
            # heartbeat threads: one, started in the probe
            check(len([t for t in threading.enumerate() if t.name == 'tune-heartbeat']) == 0, 'the heartbeat thread ended with the run')
    a, a2, b = res['without probe'], res['without probe, again'], res['with probe']

    def columns(text, drop):
        return [[c for i, c in enumerate(line.split(',')) if i not in drop] for line in text.split()]
    print('       results.csv without probe:', a[1].split())
    print('       results.csv without probe, again:', a2[1].split())
    print('       results.csv with probe:   ', b[1].split())
    check(columns(a[1], {2}) == columns(b[1], {2}) == columns(a2[1], {2}),
          'results.csv: ramp_rate, good_starts and status are the same with and without the probe')
    print('       (time_ms is the start time in 10 ms polls on the wall clock of SIM: it differs between two runs without the probe too)')
    check(a[2] == b[2] == a2[2] and len(b[2]) == 3, f'tuning.h is the same: {b[2]}')


# -- the probe on a capture script (card 6.1) --------------------------------------------------------------------------

CURRENT_SCRIPT = os.path.join(HERE, '..', 'scripts', 'tune_current.py')


def test_capture():
    ser = LogSerial()
    t0 = time.perf_counter()
    r, ev, sc, txt = run_it(CURRENT_SCRIPT, ser)
    secs = time.perf_counter() - t0
    check(r['status'] == 'ok' and not r['errors'], f'probe of tune_current.py: {r["status"]} {r["reason"]} ({secs:.1f} s on SIM)')
    check(states(ev) == ['starting', 'probe', 'safe_end', 'done'], f'states: {states(ev)}')
    check(stage4(r['rule_codes']) == {}, f'no stage 4 code: {r["rule_codes"]}')
    by = {x['name']: x['role'] for x in r['rows']}
    want = {'motor.current_ref': 'measured, written, safe, idle', 'motor.current': 'measured, limit', 'motor.current_kp': 'written',
            'motor.current_ki': 'written'}
    check(by == want, f'roles: {by}')
    check(ser.writes(ADDR_KP) == [201, 200] and ser.writes(ADDR_KI) == [21, 20], f'write test of the gains: {ser.writes(ADDR_KP)} {ser.writes(ADDR_KI)}')
    check(ser.writes(ADDR_REF) == [0, 0], f'motor.current_ref has a safe_value() and is only written with it, twice: {ser.writes(ADDR_REF)}')
    check(peek(sc, 'motor.current_kp', 'motor.current_ki', 'motor.current_ref') == [200, 20, 0], 'the model has its values back')
    # the whole run, with the probe in front
    ser = LogSerial()
    r, ev, sc, txt = run_it(CURRENT_SCRIPT, ser, full=True)
    check(r['status'] == 'ok' and r['tuning_h'] and 'trial' in states(ev) and 'running' in states(ev),
          f'full run with the probe: {r["status"]} {r["reason"]}, states {states(ev)}')
    with open(r['tuning_h'], encoding='utf-8') as f:
        h = f.read()
    check('#define CURRENT_KP 400\n' in h and '#define CURRENT_KI 20\n' in h, 'tuning.h has kp 400 and ki 20')
    check(ser.writes(ADDR_KP) == [201, 200, 100, 200, 400, 800, 400] and ser.writes(ADDR_KI) == [21, 20, 20],
          f'the probe writes the gains before the run, then the script: {ser.writes(ADDR_KP)} {ser.writes(ADDR_KI)}')
    check('heartbeat: largest gap' in txt, 'run.txt has the heartbeat gap')


TESTS = {'good': test_good, 'expect': test_expect, 'mut_expect': test_mut_expect, 'idle': test_idle_rules,
         'overwrite': test_overwrite, 'mut_overwrite': test_mut_overwrite, 'single': test_single_value,
         'near': test_near_range, 'abort': test_abort, 'mut_restore': test_mut_restore, 'port': test_port_error,
         'silent': test_silent, 'limit': test_limit, 'noping': test_no_ping, 'buildid': test_build_id,
         'refused': test_refused, 'second': test_second_run, 'mut_heartbeat': test_mut_heartbeat, 'sweep': test_sweep,
         'capture': test_capture}

for name, fn in TESTS.items():
    if len(sys.argv) < 2 or name in sys.argv[1:]:
        print(f'--- {name}')
        fn()
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if getattr(check, 'failed', False) else 0)
