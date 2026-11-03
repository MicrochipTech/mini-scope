"""test_adapt.py - the adaptation of the tuning scripts, tested against a second model on SIM (card M7d).

    python tests/test_adapt.py                 all tests
    python tests/test_adapt.py adapt rules     only these parts: adapt, inverted, rules, markers

adapt     copies of tune_startup.py, tune_speed.py and tune_current.py (tests/fixtures/adapt/) run on the second model
          (tests/mock/sim2.c, model2.h: other names, other structs, inverted logic). A line diff against scripts/ proves
          that only sections 1 to 3 changed.
inverted  names changed but the logic of the example kept: check_idle() must stop the run before the motor starts.
rules     one case per code of tune_check.RULES (the list is read from RULES, a rule without a case fails the test). Each
          case is run twice: with the mistake it must give exactly the expected code, line and message part, and without
          the mistake it must not. A case that passes both ways cannot fail and counts as an error.
markers   marker strings as variable names, ELF path, script name, user name and texts, through runtime errors, an abort
          and a limit stop: none of them may appear in tools/report.py output, doctor_report.txt or hw_report.txt.
"""
import difflib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
sys.path[:0] = [HERE, os.path.join(ROOT, 'gui'), os.path.join(ROOT, 'tools')]
import model2_serial as m2                        # noqa: E402
import tune_api                                   # noqa: E402
import tune_check                                 # noqa: E402
from elf_vars import VarError, VarTable           # noqa: E402
from scope_gui import Scope                       # noqa: E402

# ADAPT_REMOVE_MISTAKE=E220,E301,inverted,markers: the named cases are run with the mistake taken out. They must then FAIL: this is
# how the evidence shows that a negative test can fail. Unset in normal runs.
REMOVED = {x for x in os.environ.get('ADAPT_REMOVE_MISTAKE', '').split(',') if x}
FIX = os.path.join(HERE, 'fixtures')
ADAPT = os.path.join(FIX, 'adapt')
SCRIPTS = os.path.join(ROOT, 'scripts')
TMP = tempfile.mkdtemp(prefix='tune_adapt_')
tune_check.GRACE_S = 0.5
BUDGET = dict(max_calls=2000, max_seconds=1.0)
threading.excepthook = lambda a: None


def check(cond, text):
    print(('OK   ' if cond else 'FAIL ') + text, flush=True)
    if not cond:
        check.failed = True
    return cond


def read_text(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


_dlls = {}
VARIANTS = {'good': (), 'overwrite': ('M2_OVERWRITE',), 'revert': ('M2_REVERT',), 'latch': ('M2_LATCH',),
            'stuck': ('M2_STUCK_FAULT',)}


def dll(variant='good'):
    """The second model, compiled once per variant with gcc into the temp folder."""
    if variant not in _dlls:
        _dlls[variant] = m2.build(os.path.join(TMP, f'sim2_{variant}.dll'), VARIANTS[variant])
    return _dlls[variant]


def run_tool(args, cwd, env=None, timeout=240):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, *args], cwd=cwd, env=e, capture_output=True, text=True, timeout=timeout)


def on_model2(script, cwd, variant='good', extra=(), env=None, out='runs'):
    """tools/run_script.py with the port SIM mapped to the second model (tests/model2_serial.py)."""
    e = {'M2_DLL': dll(variant), **(env or {})}
    return run_tool([os.path.join(HERE, 'model2_serial.py'), '--port', 'SIM', '--out', out, *extra, script], cwd, e)


def run_folder(cwd, out='runs'):
    base = os.path.join(cwd, out)
    names = sorted(os.listdir(base)) if os.path.isdir(base) else []
    return os.path.join(base, names[-1]) if names else None


def defines(folder):
    path = os.path.join(folder, 'tuning.h') if folder else ''
    return dict(re.findall(r'#define (\w+) (\d+)', read_text(path))) if os.path.isfile(path) else {}


# -- 1. the adaptation -----------------------------------------------------------------------------------------------

def sections(lines):
    """Index of the heading lines '# ---------- 1.' and '# ---------- 4.' of a script."""
    s1 = next(i for i, x in enumerate(lines) if x.startswith('# ---------- 1.'))
    s4 = next(i for i, x in enumerate(lines) if x.startswith('# ---------- 4.'))
    return s1, s4


def lines_of(text):
    return text.splitlines()


def outside_sections(original, copy):
    """Line numbers (of the original) that the copy changed or deleted outside sections 1 to 3, or where it inserted
    something outside. The two heading lines and everything above section 1 and below section 3 count as outside."""
    a, b = lines_of(original), lines_of(copy)
    s1, s4 = sections(a)
    bad = []
    for tag, i1, i2, _, _ in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == 'insert':
            if not s1 < i1 <= s4:               # between line i1 - 1 and line i1
                bad.append(i1 + 1)
        elif tag != 'equal':
            bad += [n + 1 for n in range(i1, i2) if not s1 < n < s4]
    return bad


def changed_lines(original, copy):
    a, b = original.replace('\r\n', '\n').split('\n'), copy.replace('\r\n', '\n').split('\n')
    return sum(i2 - i1 for t, i1, i2, _, _ in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if t != 'equal')


def test_adapt():
    print('--- adapt')
    lay = m2.layout(dll())
    want = [off for off, _ in m2.AXIS.values()] + [m2.SIZE]
    check(lay == want, f'struct m2_axis: the offsets gcc computed equal the symbol table of the test: {lay == want}')
    # the diff check itself must be able to fail
    orig = read_text(os.path.join(SCRIPTS, 'tune_startup.py'))
    lines = orig.replace('\r\n', '\n').split('\n')
    s1, s4 = sections(lines)
    check(outside_sections(orig, orig) == [], 'diff check: an unchanged copy has no change outside sections 1 to 3')
    probes = {'section 4 line changed': (s4 + 3, lambda x: x + ' # x'), 'heading changed': (s4, lambda x: x + ' x'),
              'header comment changed': (2, lambda x: x + ' x'), 'section 1 line changed': (s1 + 2, lambda x: x + ' # x')}
    for name, (n, f) in probes.items():
        mutated = lines[:]
        mutated[n] = f(mutated[n])
        got = outside_sections(orig, '\n'.join(mutated))
        check(bool(got) == (name != 'section 1 line changed'), f'diff check: {name}: outside lines {got}')
    mutated = lines[:s4 + 3] + ['    pass'] + lines[s4 + 3:]
    check(outside_sections(orig, '\n'.join(mutated)) != [], 'diff check: a line inserted in section 4 is found')
    mutated = lines[:s4 - 1] + ['    pass'] + lines[s4 - 1:]
    check(outside_sections(orig, '\n'.join(mutated)) == [], 'diff check: a line inserted at the end of section 3 is allowed')
    # the example scripts do not run on the second model unchanged: the model really is different
    work = tempfile.mkdtemp(dir=TMP)
    p = on_model2(os.path.join(SCRIPTS, 'tune_startup.py'), work, extra=('--check',))
    check(p.returncode == 1 and 'E201' in p.stdout, f'tune_startup.py unchanged on the second model: stopped by E201 (exit {p.returncode})')

    ref = {}
    for name in ('startup', 'speed', 'current'):
        cwd = tempfile.mkdtemp(dir=TMP)
        p = run_tool([os.path.join(ROOT, 'tools', 'run_script.py'), '--port', 'SIM', '--out', 'runs', os.path.join(SCRIPTS, f'tune_{name}.py')], cwd)
        folder = run_folder(cwd)
        ref[name] = defines(folder)
        check(p.returncode == 0 and ref[name], f'tune_{name}.py (original) on SIM: exit {p.returncode}, {ref[name]}')
    for name in ('startup', 'speed', 'current'):
        orig = read_text(os.path.join(SCRIPTS, f'tune_{name}.py'))
        copy = read_text(os.path.join(ADAPT, f'tune_{name}_m2.py'))
        bad = outside_sections(orig, copy)
        check(not bad and changed_lines(orig, copy) > 0, f'tune_{name}_m2.py: {changed_lines(orig, copy)} lines changed, all inside sections 1 to 3 '
              f'(outside: {bad})')
        cwd = tempfile.mkdtemp(dir=TMP)
        t0 = time.time()
        p = on_model2(os.path.join(ADAPT, f'tune_{name}_m2.py'), cwd)
        folder = run_folder(cwd)
        got = defines(folder)
        check(p.returncode == 0, f'tune_{name}_m2.py on the second model: exit {p.returncode} after {time.time() - t0:.0f} s')
        if p.returncode:
            print(p.stdout[-1500:])
        check(got == ref[name] and bool(got), f'tune_{name}_m2.py: tuning.h {got} equals the original on the example model {ref[name]}')
        if name == 'startup' and folder:
            with open(os.path.join(folder, 'results.csv'), newline='') as f:
                rows = [r.strip().split(',') for r in f if r.strip()]
            check([r[:2] for r in rows[1:]] == [['50', '1'], ['100', '1'], ['200', '1'], ['400', '0']] and rows[-1][3] == 'failed',
                  f'tune_startup_m2.py: the same table as on the example model: {rows[1:]}')
        txt = read_text(os.path.join(folder, 'run.txt')) if folder else ''
        safe = 'axis[0].cl.ref = 0 (safe)' if name == 'current' else 'axis[0].ctl.disable = 1 (safe)'
        check(safe in txt, f'run.txt: the safe value of the second model is written: {safe}')


# -- 2. inverted logic ------------------------------------------------------------------------------------------------

def probe_script(src, name='t.py'):
    folder = tempfile.mkdtemp(dir=TMP)
    path = os.path.join(folder, name)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(src)
    return path


def stage4_problems(r):
    out = []
    for s in r.get('problems', []):
        m = re.match(r'(.+?):(\d+): (error|warning): (.*) \[([EW]\d{3})\]$', s, re.S)
        if m and tune_check.RULES[m[5]][1] == 4:
            out.append((m[5], int(m[2]), m[4]))
    return sorted(out)


def probe_it(path, variant='good', ser=None, table=None, hooks=None, ping=True):
    ser = ser or m2.Sim2Serial(dll(variant))
    sc = Scope(ser)
    if ping:
        sc.ping()
    r = tune_api.probe(path, sc, table or T2(variant), hooks=hooks)
    return r, ser


def test_inverted():
    print('--- inverted')
    src = read_text(os.path.join(SCRIPTS, 'tune_startup.py')).replace('\r\n', '\n')
    names = read_text(os.path.join(ADAPT, 'tune_startup_m2.py')).split('# ---------- 2.')[0]
    # section 1 of the adapted copy with sections 2 to 4 of the example: the names fit, the logic does not
    half = names + '# ---------- 2.' + src.split('# ---------- 2.', 1)[1]
    if 'inverted' in REMOVED:
        half = read_text(os.path.join(ADAPT, 'tune_startup_m2.py'))
    path = probe_script(half)
    r, _ = probe_it(path)
    got = stage4_problems(r)
    want_line = next(i for i, x in enumerate(half.split('\n'), 1) if 'expect(not has_fault()' in x)
    check(r['status'] == 'error' and [(c, ln) for c, ln, _ in got] == [('E410', want_line)] and 'no fault expected' in got[0][2],
          f'names right, logic of the example (fault != 0, bit 0 of the flags is "power ok"): E410 at the expect line {want_line}: {got}')
    # a firmware with a fault bit stuck: check_idle() of the correct adapted copy must stop
    path = probe_script(read_text(os.path.join(ADAPT, 'tune_startup_m2.py')))
    r, _ = probe_it(path, 'stuck')
    got = stage4_problems(r)
    check(r['status'] == 'error' and [c for c, _, _ in got] == ['E410'] and 'no fault expected' in got[0][2], f'fault bit stuck in the flags: E410: {got}')
    r, _ = probe_it(path, 'good')
    check(r['status'] == 'ok' and stage4_problems(r) == [], f'the adapted copy on the healthy second model: ok: {r["status"]} {stage4_problems(r)}')


# -- 3. one case per rule ---------------------------------------------------------------------------------------------

_t2 = {}


def T2(variant='good'):
    if variant not in _t2:
        _t2[variant] = m2.table(dll(variant), extra={'probef': (0x1440, 4, 'f32'), 'odd': (0x1441, 2, 'u16')})
    return _t2[variant]


ELF = VarTable.from_elf(os.path.join(FIX, 'vars.elf'))
NODEBUG = VarTable.from_elf(os.path.join(FIX, 'vars_nodebug.elf'))


class Mixed:
    """Names in `elf_names` come from the ELF fixture, all others from the second model."""

    def __init__(self, elf, elf_names):
        self.elf, self.names = elf, set(elf_names)

    def lookup(self, name, require_ram=True):
        return (self.elf if name in self.names else T2()).lookup(name, require_ram)


class Raising:
    """lookup() of one name raises this VarError rule, all others come from the second model."""

    def __init__(self, name, rule):
        self.name, self.rule = name, rule

    def lookup(self, name, require_ram=True):
        if name == self.name:
            raise VarError(self.rule, f'{name}: test error {self.rule}')
        return T2().lookup(name, require_ram)


BASE = '''from tune_api import *

VAR_RUN   = "axis[0].ctl.disable"
VAR_SPEED = "axis[0].st.rpm"
VAR_STATE = "axis[0].st.mode"
VAR_RAMP  = "axis[0].su.acc"


def check_idle():
    expect(read(VAR_SPEED) == 0, "must stand still")


def main():
    channels(VAR_SPEED, VAR_STATE)
    allow(VAR_RUN, 0, 1)
    allow(VAR_RAMP, 1, 500)
    safe_value(VAR_RUN, 1)
    safe_value(VAR_RAMP, 100)
    unit(VAR_SPEED, 0.5, "rpm")
    unit(VAR_STATE, 1, "state")
    table("ramp", "good")
    stream_start()
    write(VAR_RAMP, 100)
    write(VAR_RUN, 0)
    trace_begin()
    wait_ms(10)
    trace_end("run %d", 1)
    row(100, 1)
    printf("speed %d\\n", read(VAR_SPEED))
    write(VAR_RUN, 1)
'''

CAP = '''from tune_api import *

VAR_REF  = "axis[0].cl.ref"
VAR_CUR  = "axis[0].cl.act"
VAR_KP   = "axis[0].cl.kp"


def check_idle():
    expect(read(VAR_REF) == 0, "must stand still")


def main():
    capture_channels(VAR_REF, VAR_CUR)
    capture_period_us(100)
    capture_trigger(VAR_REF, RISING, 250, 10)
    allow(VAR_REF, 0, 1000)
    allow(VAR_KP, 0, 2000)
    safe_value(VAR_REF, 0)
    safe_value(VAR_KP, 200)
    unit(VAR_REF, 1, "raw")
    unit(VAR_CUR, 1, "raw")
    table("kp", "rise")
    capture_start()
    write(VAR_KP, 100)
    capture_arm()
    write(VAR_REF, 500)
    wait_ms(100)
    write(VAR_REF, 0)
    if capture_wait(500) != 0:
        return
    rise = capture_rise_us(VAR_CUR)
    if capture_max(VAR_CUR) > capture_final(VAR_CUR):
        trace_failed()
    capture_save("kp %d", 100)
    row(100, rise)
    printf("length %d value %d\\n", capture_length(), capture_value(VAR_CUR, 0))
'''


def mutate(edits, base):
    """edits: (old, new); old is in exactly one line, new may span lines, None deletes the line."""
    lines = base.split('\n')
    for old, new in edits:
        hit = [i for i, x in enumerate(lines) if x is not None and old in x]
        assert len(hit) == 1, f'{old!r} is in {len(hit)} lines'
        lines[hit[0]] = None if new is None else lines[hit[0]].replace(old, new)
    return '\n'.join(x for x in lines if x is not None)


def line_of(src, marker):
    return next(i for i, x in enumerate(src.split('\n'), 1) if marker in x)


CASES = []          # rules of stages 1 to 3: checked with tune_check.check_script
PROBES = []         # rules of stage 4: checked with tune_api.probe on the second model


def case(code, edits, wants, base=BASE, table=None, **kw):
    """wants: [(code, marker, message part)]; the marker finds the line in the changed script."""
    CASES.append(dict(code=code, edits=edits, wants=wants, base=base, table=table, kw=kw))


def stage13_cases():
    CASES.clear()
    cap = dict(base=CAP)
    c = case
    # stage 1
    c('E101', [('write(VAR_RUN, 0)', 'write(VAR_RUN, 0))')], [('E101', 'write(VAR_RUN, 0))', '')])
    c('E102', [('write(VAR_RUN, 0)', 'wirte(VAR_RUN, 0)')], [('E102', 'wirte(', 'wirte')])
    c('E103', [('def check_idle', 'LEVEL = 5\n\n\ndef helper():\n    print(LEVEL)\n    LEVEL = 6\n\n\ndef check_idle')], [('E103', 'print(LEVEL)', 'LEVEL')])
    c('E104', [('def main', 'def mian')], [('E104', 'from tune_api', 'main')])
    c('E105', [('def check_idle', 'def check_idel')], [('E105', 'from tune_api', 'check_idle')])
    # stage 2: the variable names, on a line VAR_RUN = "..."
    c('E201', [('"axis[0].ctl.disable"', '"axis[0].ctl.disabel"')], [('E201', 'VAR_RUN   =', 'did you mean')])
    elf = [('E202', 'motor', 'name a member'), ('E203', 'grid', 'name an element'), ('E204', 'motor.ptr', 'pointer'),
           ('E205', 'motor.flags.ready', 'bit field'), ('E206', 'motor.wide', 'bytes wide'), ('E207', 'table_c[1]', 'not in RAM'),
           ('E208', 'motor.run.x', 'no member'), ('E209', 'counter', 'exists')]
    for code, name, frag in elf:
        c(code, [('"axis[0].ctl.disable"', f'"{name}"')], [(code, 'VAR_RUN   =', frag)], table=Mixed(ELF, [name]))
    c('E210', [('"axis[0].ctl.disable"', '"motor.speed2"')], [('E210', 'VAR_RUN   =', 'debug')], table=Mixed(NODEBUG, ['motor.speed2']))
    c('E211', [('"axis[0].ctl.disable"', '"axis[0].ctl.disable"')], [('E211', 'VAR_RUN   =', 'test error unsupported')],
      table=Raising('axis[0].ctl.disable', 'unsupported'))
    more = [('channels(VAR_SPEED, VAR_STATE)', 'channels(VAR_SPEED, VAR_STATE, "axis[0].st.torque", "axis[0].st.rpm_cmd", "axis[0].st.flags")'),
            ('unit(VAR_STATE, 1, "state")', 'unit(VAR_STATE, 1, "state")\n    unit("axis[0].st.torque", 1, "")\n    unit("axis[0].st.rpm_cmd", 1, "")'
             '\n    unit("axis[0].st.flags", 1, "")')]
    c('E212', more, [('E212', 'channels(', 'at most')], baud=1_000_000)
    c('E213', [], [('E213', 'stream_start()', 'UART')], baud=57600)
    no_allow = [('allow(VAR_RUN, 0, 1)', None), ('allow(VAR_RAMP, 1, 500)', None), ('safe_value(VAR_RUN, 1)', None),
                ('safe_value(VAR_RAMP, 100)', None), ('write(VAR_RAMP, 100)', None), ('write(VAR_RUN, 0)', None), ('write(VAR_RUN, 1)', None)]
    c('E214', no_allow, [('E214', 'stream_start()', 'allow()'), ('E215', 'stream_start()', 'safe_value()')])
    c('E215', [('safe_value(VAR_RUN, 1)', None), ('safe_value(VAR_RAMP, 100)', None)],
      [('E215', 'stream_start()', 'safe_value()'), ('W204', 'VAR_RUN   =', ''), ('W204', 'VAR_RAMP  =', '')])
    c('E216', [('channels(VAR_SPEED, VAR_STATE)', None), ('unit(VAR_SPEED, 0.5, "rpm")', None), ('unit(VAR_STATE, 1, "state")', None),
               ('printf("speed %d\\n", read(VAR_SPEED))', None)], [('E216', 'stream_start()', 'channels()')])
    c('E217', [], [('E217', 'stream_start()', 'tune_alive')], table=m2.table(dll(), drop=['tune_alive']))
    c('E218', [('capture_trigger(VAR_REF, RISING, 250, 10)', 'capture_trigger("probef", RISING, 250, 10)'),
               ('capture_channels(VAR_REF, VAR_CUR)', 'capture_channels(VAR_REF, VAR_CUR, "probef")')],
      [('E218', 'capture_trigger("probef"', 'float'), ('W202', 'capture_channels(', ''), ('W205', 'capture_channels(', '')], **cap)
    c('E219', [], [('E219', 'capture_start()', 'transfer')], baud=9600, **cap)
    c('E220', [('allow(VAR_RAMP, 1, 500)', 'allow(VAR_RAMP, 1, 70000)')], [('E220', 'allow(VAR_RAMP', 'outside the range')])
    c('E221', [('allow(VAR_RAMP, 1, 500)', 'allow(VAR_RAMP, 500, 1)')], [('E221', 'allow(VAR_RAMP', 'min is above max')])
    c('E222', [('safe_value(VAR_RAMP, 100)', 'safe_value(VAR_RAMP, 900)')], [('E222', 'safe_value(VAR_RAMP', 'outside allow()')])
    c('E223', [('safe_value(VAR_RAMP, 100)', 'safe_value(VAR_RAMP, 100)\n    safe_value("axis[0].sl.kp", 0)')],
      [('E223', 'safe_value("axis[0].sl.kp"', 'without allow()')])
    c('E224', [('safe_value(VAR_RUN, 1)', 'limit(VAR_RAMP, 100)\n    safe_value(VAR_RUN, 1)')], [('E224', 'limit(VAR_RAMP', 'not in channels()')])
    c('E225', [('safe_value(VAR_RAMP, 100)', 'safe_value(VAR_RAMP, 100.5)')], [('E225', 'safe_value(VAR_RAMP', 'not a whole number')])
    c('E226', [('safe_value(VAR_RUN, 1)', 'limit(VAR_SPEED, -5)\n    safe_value(VAR_RUN, 1)')], [('E226', 'limit(VAR_SPEED', 'negative')])
    c('E227', [('capture_period_us(100)', 'capture_period_us(0)')], [('E227', 'capture_period_us(0)', 'whole number')], **cap)
    c('E229', [('capture_period_us(100)', 'capture_period_us(5)')], [('E229', 'capture_period_us(5)', 'below 10 us')], **cap)
    c('E228', [('RISING', '3')], [('E228', 'capture_trigger', 'RISING or FALLING')], **cap)
    c('W201', [('"axis[0].ctl.disable"', '"plain.run"')], [('W201', 'VAR_RUN   =', 'volatile')], table=Mixed(ELF, ['plain.run']))
    c('W202', [('"axis[0].st.rpm"', '"motor.pos"')], [('W202', 'VAR_SPEED =', '32')], table=Mixed(ELF, ['motor.pos']))
    c('W203', [('allow(VAR_REF, 0, 1000)', 'allow(VAR_REF, 0, 1000)\n    limit(VAR_CUR, 1500)')], [('W203', 'limit(VAR_CUR', 'capture')], **cap)
    c('W204', [('safe_value(VAR_RAMP, 100)', None)], [('W204', 'VAR_RAMP  =', 'safe_value()')])
    c('W206', [('capture_period_us(100)', 'capture_period_us(30)')], [('W206', 'capture_period_us(30)', 'interrupt load')], **cap)
    c('W205', [('unit(VAR_STATE, 1, "state")', None)], [('W205', 'VAR_STATE =', 'unit()')])
    # stage 3
    c('E301', [('write(VAR_RAMP, 100)', 'write(VAR_RAMP, 600)')], [('E301', 'write(VAR_RAMP', 'outside allow()')])
    c('E302', [('wait_ms(10)', 'wait_ms(10)\n    read("axis[0].st.flags")')], [('E302', 'read("axis[0].st.flags")', 'not in channels()')])
    c('E303', [('wait_ms(10)', 'wait_ms(10)\n    write("axis[0].sl.kp", 1)')], [('E303', 'write("axis[0].sl.kp"', 'not allowed')])
    c('E304', [('row(100, 1)', 'row(100)')], [('E304', 'row(100)', 'columns')])
    c('E305', [('table("ramp", "good")', None)], [('E305', 'row(100, 1)', 'table()')])
    c('E306', [('stream_start()', 'write(VAR_RUN, 1)\n    stream_start()')], [('E306', 'write(VAR_RUN, 1)', 'stream_start()')])
    c('E307', [('write(VAR_RAMP, 100)', 'channels(VAR_STATE)\n    write(VAR_RAMP, 100)')], [('E307', 'channels(VAR_STATE)', 'before stream_start')])
    c('E308', [('trace_begin()', None)], [('E308', 'trace_end(', 'trace_begin()')])
    c('E309', [('write(VAR_RAMP, 100)', 'stream_start()  # again\n    write(VAR_RAMP, 100)')], [('E309', '# again', 'already')])
    c('E310', [('write(VAR_RUN, 1)', 'define("1abc", 3)')], [('E310', 'define(', 'not a C name')])
    c('E311', [('printf("speed %d\\n", read(VAR_SPEED))', 'printf("speed %d %d\\n", read(VAR_SPEED))')], [('E311', 'printf(', 'do not fit')])
    c('E312', [('wait_ms(10)', 'wait_ms("10")')], [('E312', 'wait_ms(', 'number')])
    c('E313', [('wait_ms(10)', 'wait_ms(10)\n    read(5)')], [('E313', 'read(5)', 'string')])
    c('E314', [('write(VAR_KP, 100)', 'write(VAR_KP, 100)\n    x = read(VAR_CUR)  # READ')], [('E314', '# READ', 'capture mode')], **cap)
    c('E315', [('capture_start()', 'trace_begin()  # MIX\n    capture_start()')], [('E315', '# MIX', 'cannot be mixed')], **cap)
    c('E316', [('write(VAR_REF, 500)', 'capture_arm()  # TWICE\n    write(VAR_REF, 500)')], [('E316', '# TWICE', 'twice')], **cap)
    c('E390', [('write(VAR_RUN, 0)', 'write(VAR_RUN)')], [('E390', 'write(VAR_RUN)', 'missing 1 required positional argument')])
    c('W301', [('row(100, 1)', 'row(100, 1)\n    while read(VAR_STATE) != 12:\n        wait_ms(10)')], [('W301', ('while read', '        wait_ms(10)'), 'API calls')])


def match(got, wants, src):
    """got: Problems of the checker. True when they are exactly wants: code, line (found in src) and message part."""
    have = sorted((p.code, p.line) for p in got)
    ok_lines = lambda marker: {line_of(src, m) for m in ((marker,) if isinstance(marker, str) else marker)}      # noqa: E731
    left = list(have)
    for code, marker, _ in wants:
        hit = next((x for x in left if x[0] == code and x[1] in ok_lines(marker)), None)
        if hit is None:
            return False, f'got {have}, wanted {[(c, sorted(ok_lines(m))) for c, m, _ in wants]}'
        left.remove(hit)
    if left:
        return False, f'got {have}, wanted {[(c, sorted(ok_lines(m))) for c, m, _ in wants]}'
    for code, marker, frag in wants:
        p = next(x for x in got if x.code == code and x.line in ok_lines(marker))
        if frag.lower() not in p.text.lower():
            return False, f'{code} text {p.text!r} lacks {frag!r}'
    return True, ''


_n = [0]


def write_script(src):
    _n[0] += 1
    folder = os.path.join(TMP, f's{_n[0]}')
    os.makedirs(folder)
    path = os.path.join(folder, 't.py')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(src)
    return path


def run_case13(c, mistake):
    mistake = mistake and c['code'] not in REMOVED
    src = mutate(c['edits'], c['base'])
    path = write_script(src if mistake else c['base'])
    # without the mistake: also the table and the options of the case are the plain ones (the mistake may sit there)
    table, kw = (c['table'], c['kw']) if mistake else (None, {})
    got = tune_check.check_script(path, table or T2(), **{**BUDGET, **kw})
    return match(got, c['wants'], src)


def test_rules_stage13():
    print('--- rules, stages 1 to 3')
    stage13_cases()
    for c in CASES:
        ok, why = run_case13(c, True)
        if check(ok, f'{c["code"]}: the mistake gives exactly the expected code, line and message' + ('' if ok else f': {why}')):
            COVERED.add(c['code'])
        alone, _ = run_case13(c, False)
        check(not alone, f'{c["code"]}: the same expectation fails on the script without the mistake')
        if alone:
            COVERED.discard(c['code'])
    # the clean scripts really are clean
    for name, base in (('stream', BASE), ('capture', CAP)):
        got = tune_check.check_script(write_script(base), T2(), **BUDGET)
        check(got == [], f'the {name} base script has no problems: {got}')
    src = mutate([('write(VAR_RUN, 0)', 'wirte(VAR_RUN, 0)')], BASE)
    p = tune_check.check_script(write_script(src), T2(), **BUDGET)[0]
    check(str(p).startswith('t.py:' + str(line_of(src, 'wirte(')) + ': error: '), f'the form is file:line: severity: text: {p}')


# -- stage 4 ----------------------------------------------------------------------------------------------------------

STARTUP2 = read_text(os.path.join(ADAPT, 'tune_startup_m2.py'))
ODD = '''from tune_api import *


def check_idle():
    expect(read("axis[0].st.rpm") == 0, "x")


def main():
    channels("axis[0].st.rpm")
    allow("axis[0].ctl.disable", 0, 1)
    safe_value("axis[0].ctl.disable", 1)
    stream_start()
'''


class Silent(m2.Sim2Serial):
    """The target never answers."""

    def read(self, n=1):
        time.sleep(self.timeout)
        return b''

    @property
    def in_waiting(self):
        return 0


def probe_case(code, mistake, wants, base=STARTUP2, status=None):
    PROBES.append(dict(code=code, mistake=mistake, wants=wants, base=base, status=status or 'error'))


def stage4_cases():
    PROBES.clear()
    p = probe_case
    p('E401', dict(ser=Silent, ping=False), [('E401', None, 'ping')], base=ODD)
    p('E402', dict(ser=lambda d: m2.Sim2Serial(d, bad_build_id=True)), [('E402', None, 'not the build of the ELF')], base=ODD)
    p('E402', dict(table=lambda: m2.table(dll(), text=False)), [('E402', None, 'cannot prove its build')], base=ODD)
    p('E403', dict(edits=[('channels("axis[0].st.rpm")', 'channels("odd")'), ('read("axis[0].st.rpm") == 0', 'read("odd") == 0')]),
      [('E403', 'channels(', 'cannot be streamed')], base=ODD)
    p('E404', dict(hooks=lambda: {'hold_heartbeat': _held()}), [('E404', None, 'heartbeat')], base=ODD)
    p('E410', dict(edits=[('read(VAR_STATE) == 12', 'read(VAR_STATE) == 7')]), [('E410', 'expect(not in_closed_loop()', 'closed loop')])
    p('E410', dict(edits=[('(read(VAR_FAULT) & 0x03F0) != 0', 'read(VAR_FAULT) != 0')]), [('E410', 'expect(not has_fault()', 'no fault expected')])
    p('E410', dict(variant='stuck'), [('E410', 'expect(not has_fault()', 'no fault expected')])
    p('E411', dict(edits=[('expect(stands_still(), ', 'x = 1 // (read(VAR_RAMP_RATE) - 100)  # DIV\n    expect(stands_still(), ')]),
      [('E411', '# DIV', 'ZeroDivisionError')])
    p('E412', dict(edits=[('expect(stands_still(), ', 'write(VAR_RUN, 1)  # IDLE\n    expect(stands_still(), ')]),
      [('E412', '# IDLE', 'check_idle()')])
    p('E420', dict(variant='overwrite'), [('E420', 'allow(VAR_RAMP_RATE', 'overwrites')])
    p('E420', dict(variant='revert'), [('E420', 'allow(VAR_RAMP_RATE', 'overwrites')])
    p('E421', dict(variant='latch'), [('E421', 'allow(VAR_ALIGN_CURRENT', 'could not be restored')])
    p('E422', dict(patch='refuse'), [('E422', 'allow(VAR_RAMP_RATE', 'refused')])
    p('W401', dict(edits=[('allow(VAR_HANDOVER_SPEED, 100, 2000)', 'allow(VAR_HANDOVER_SPEED, 600, 600)')]),
      [('W401', 'allow(VAR_HANDOVER_SPEED', 'one value')], status='ok')
    p('W402', dict(edits=[('allow(VAR_HANDOVER_SPEED, 100, 2000)', 'allow(VAR_HANDOVER_SPEED, 700, 800)'),
                          ('HANDOVER_SPEED     = 600', 'HANDOVER_SPEED     = 750')]),
      [('W402', 'allow(VAR_HANDOVER_SPEED', 'reads 600')], status='ok')


_hb = []


def _held():
    e = threading.Event()
    e.set()
    _hb.append(e)
    return e


def run_probe(c, mistake):
    """-> (ok, why). mistake False: the same probe with the correct script, model and port."""
    mistake = mistake and c['code'] not in REMOVED
    m = c['mistake']
    base = c['base']
    edits = m.get('edits', [])
    src_m = mutate(edits, base) if edits else base
    src = src_m if mistake else base
    variant = m.get('variant', 'good') if mistake else 'good'
    ser = (m['ser'](dll(variant)) if m.get('ser') else None) if mistake else None
    table = m['table']() if (mistake and m.get('table')) else None
    hooks = m['hooks']() if (mistake and m.get('hooks')) else None
    ping = m.get('ping', True) if mistake else True
    path = probe_script(src)
    orig_write = Scope.write
    ctx = mock.patch.object(Scope, 'write', lambda s, a, t, v: 'port refused' if a == m2.BASE + 26 else orig_write(s, a, t, v)) \
        if (mistake and m.get('patch') == 'refuse') else mock.patch.object(Scope, 'ping', Scope.ping)
    with ctx:
        try:
            r, ser = probe_it(path, variant, ser, table, hooks, ping)
        except tune_api.RunRefused as e:
            return False, f'refused before the probe: {str(e)[:120]}'
    got = stage4_problems(r)
    exp = sorted((code, line_of(src_m, mk) if mk else 1) for code, mk, _ in c['wants'])
    if [(a, b) for a, b, _ in got] != exp:
        return False, f'got {[(a, b) for a, b, _ in got]}, wanted {exp}'
    for (code, line, text), (_, _, frag) in zip(got, sorted(c['wants'], key=lambda w: w[0])):
        if frag.lower() not in text.lower():
            return False, f'{code} text {text!r} lacks {frag!r}'
    if r['status'] != c['status']:
        return False, f'status {r["status"]}, wanted {c["status"]}'
    return True, ''


def test_rules_stage4():
    print('--- rules, stage 4 (the target probe on the second model)')
    stage4_cases()
    for c in PROBES:
        label = c['code'] + ' ' + ', '.join(sorted(c['mistake']))
        ok, why = run_probe(c, True)
        if check(ok, f'{label}: the mistake gives exactly the expected code, line and message' + ('' if ok else f': {why}')):
            COVERED.add(c['code'])
        alone, _ = run_probe(c, False)
        check(not alone, f'{label}: the same expectation fails on the correct script, model and port')
        if alone:
            COVERED.discard(c['code'])


COVERED = set()
GAPS = {}           # code -> reason: a rule that cannot be provoked on SIM. Empty means: all of RULES are covered.


def test_rules_complete():
    print('--- rules, completeness against RULES')
    codes = set(tune_check.RULES)
    check(len(codes) >= 68, f'RULES has {len(codes)} codes')
    missing = sorted(codes - COVERED - set(GAPS))
    extra = sorted((COVERED | set(GAPS)) - codes)
    check(not missing, f'every code of RULES has a case: missing {missing}')
    check(not extra, f'no case for a code that RULES does not have: {extra}')
    check(not (COVERED & set(GAPS)), 'no code is both covered and a gap')
    print(f'     covered {len(COVERED)} of {len(codes)}; gaps: {GAPS or "none"}')
    # SCRIPTING.md section 9 lists the same codes
    doc = read_text(os.path.join(ROOT, 'SCRIPTING.md'))
    sect = doc.split('## 9. Error codes', 1)[1]
    docs = set(re.findall(r'^\| ([EW]\d{3}) \|', sect, re.M))
    check(docs == codes, f'SCRIPTING.md section 9 lists exactly the codes of RULES: {sorted(docs ^ codes)}')


# -- 4. markers ---------------------------------------------------------------------------------------------------------

MARK = dict(var='MKRVAR_', dir='MKRDIR_q7', user='MKRUSER_w3', elf='MKRELF_e5', text='MKRTEXT_t1', script='MKRSCRIPT_s2',
            abort='MKRABORT_a4', expect='MKREXPECT_x6', unit='MKRUNIT_u8', define='MKRDEF_d9', printf='MKRPRINT_p0')
MARKERS = [v for v in MARK.values()]


def marked(src):
    """The names of the second model in a script, with the variable marker in front of every identifier."""
    def one(m):
        return '"' + re.sub(r'[A-Za-z_]\w*', lambda x: MARK['var'] + x.group(0), m.group(1)) + '"'
    return re.sub(r'"(axis\[0\][^"]*)"', one, src)


def leaks(text):
    return [m for m in MARKERS if m in text]


def marker_scripts():
    """name -> script text. All on the marked names; the errors are of the kind the dry run cannot see, because the fake target
    reads 0 everywhere and the real motor turns."""
    s = marked(STARTUP2)
    mk = MARK
    lines = s.split('\n')
    i = next(n for n, x in enumerate(lines) if x.strip() == 'if has_fault():' and 'start_motor' in '\n'.join(lines[max(0, n - 8):n]))

    def ins(body):
        return '\n'.join(lines[:i] + body + lines[i:])

    return {
        'ok': s.replace('unit(VAR_SPEED, 0.5, "rpm")', f'unit(VAR_SPEED, 0.5, "{mk["unit"]}")')
               .replace('printf("Suggested ramp rate: %d, written to tuning.h\\n", result)', f'printf("{mk["printf"]} %d\\n", result)')
               .replace('define("RAMP_RATE", result)', f'define("{mk["define"]}", result)'),
        'value_error': ins(['        if read(VAR_SPEED) > 0:', f'            int("{mk["text"]}")']),
        'script_error': ins(['        if read(VAR_SPEED) > 0:', '            write(VAR_RAMP_RATE, 9999)']),
        'limit': s.replace('limit(VAR_CURRENT, 1500)', 'limit(VAR_CURRENT, 100)'),
        'abort': s,
        'idle': s.replace('expect(stands_still(), "motor must stand still: check VAR_SPEED")',
                          f'expect(read(VAR_SPEED) == 99, "{mk["expect"]} {mk["var"]}")'),
        'elf_error': s.replace('disable"', 'disabel"', 1),
    }


def scan(label, text, allowed=()):
    hit = [m for m in leaks(text) if m not in allowed]
    return check(not hit, f'{label}: no marker in it' + (f' (found {hit})' if hit else ''))


def xfail(cond, text):
    """A known finding: expected to fail. If it passes, the finding is gone and the test must be updated, so that fails."""
    print(('XPASS (finding gone, update the test) ' if cond else 'XFAIL ') + text, flush=True)
    if cond:
        check.failed = True


LEAKS = {x for x in os.environ.get('ADAPT_LEAK', '').split(',') if x}      # report, doctor, hw: plant a leak in the copy of that tool


def plant_leaks(root):
    """ADAPT_LEAK=report,doctor,hw puts a path or the user name into the output of the COPY of that tool (the files under tools/ of the
    repository are not touched). The marker test must then fail, which shows that it can."""
    edits = {'report': ('report.py', "    text = build(read_lines(a.doctor), hw_json, read_lines(a.hw), read_lines(a.run))",
                        "    text = build(read_lines(a.doctor), hw_json, read_lines(a.hw), read_lines(a.run)) + 'path ' + os.path.abspath(a.run) + chr(10)"),
             'doctor': ('doctor.py', "f'mini-scope doctor  {datetime.datetime.now():%Y-%m-%d %H:%M}')",
                        "f'mini-scope doctor  {datetime.datetime.now():%Y-%m-%d %H:%M} {ROOT}')"),
             'hw': ('hw_check.py', "f'mini-scope hw_check  {datetime.datetime.now():%Y-%m-%d %H:%M}  port {port}, {a.baud} baud')",
                    "f'mini-scope hw_check  {datetime.datetime.now():%Y-%m-%d %H:%M}  port {port}, {a.baud} baud {os.environ[chr(85)+chr(83)+chr(69)+chr(82)+chr(78)+chr(65)+chr(77)+chr(69)]}')")}
    for key in LEAKS:
        name, old, new = edits[key]
        path = os.path.join(root, 'tools', name)
        text = read_text(path)
        assert text.count(old) == 1, f'{key}: the line to change is not in {name} any more'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text.replace(old, new))


def marker_tree():
    """A copy of the tools, the GUI modules, the firmware and the tests in a folder named with a marker, below a folder named
    like a user: doctor.py, hw_check.py and run_script.py find their ROOT there, and the paths they know carry the markers."""
    root = os.path.join(tempfile.mkdtemp(dir=TMP), MARK['dir'], MARK['user'])
    skip = shutil.ignore_patterns('__pycache__', 'dist', 'build', '.generated_files', 'private', 'docs', '.git', '*.pdf', 'fixtures')
    for d in ('tools', 'gui', 'firmware', 'tests', 'test.X'):
        shutil.copytree(os.path.join(ROOT, d), os.path.join(root, d), ignore=skip)
    shutil.copytree(os.path.join(HERE, 'fixtures', 'adapt'), os.path.join(root, 'tests', 'fixtures', 'adapt'))
    shutil.copy(os.path.join(HERE, 'sim.dll'), os.path.join(root, 'tests', 'sim.dll'))
    plant_leaks(root)
    return root


def test_markers():
    print('--- markers')
    check(leaks('x ' + MARK['user'] + ' y') == [MARK['user']] and leaks('clean text') == [], 'scanner: finds a marker, ignores a clean text')
    root = marker_tree()
    home = os.path.join(root, 'home')
    os.makedirs(home)
    env = {'USERNAME': MARK['user'], 'USER': MARK['user'], 'LOGNAME': MARK['user'], 'USERPROFILE': home, 'HOME': home,
           'MPLCONFIGDIR': os.path.join(home, 'mpl'), 'M2_MARK': MARK['var']}
    elf_dir = os.path.join(root, MARK['elf'])
    os.makedirs(elf_dir)
    elf = os.path.join(elf_dir, 'firmware.elf')
    with open(elf, 'wb') as f:
        f.write(b'\x7fELF')
    check(MARK['dir'] in root and MARK['user'] in root, 'the tools run from a folder whose path holds two markers')
    tools = os.path.join(root, 'tools')
    work = os.path.join(root, 'work')
    os.makedirs(work)
    p = run_tool([os.path.join(tools, 'doctor.py')], work, env)
    doctor = os.path.join(root, 'doctor_report.txt')
    check(p.returncode in (0, 1) and 'Result:' in p.stdout and os.path.isfile(doctor), f'doctor.py ran in the marker setting: exit {p.returncode}')
    check(MARK['dir'] in p.stdout, 'doctor.py prints its folder to the console, so the marker is in the tool output (the scan has something to find)')
    scan('doctor_report.txt', read_text(doctor))
    hw = os.path.join(root, 'hw_report.txt')
    p = run_tool([os.path.join(tools, 'hw_check.py'), '--port', 'SIM', '--secs', '5', '--report', hw], work, env)
    check(os.path.isfile(hw) and 'Result:' in p.stdout, f'hw_check.py --port SIM ran in the marker setting: exit {p.returncode}')
    check(MARK['dir'] in p.stdout, 'hw_check.py prints a path to the console too (the marker is there)')
    scan('hw_report.txt', read_text(hw))
    scan('hw_report.json', read_text(os.path.splitext(hw)[0] + '.json'))

    for name, src in marker_scripts().items():
        folder = os.path.join(work, name)
        os.makedirs(folder)
        path = os.path.join(folder, MARK['script'] + '.py')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(src)
        e = dict(env, M2_DLL=dll())
        if name == 'abort':
            e.update(M2_ABORT_AFTER='3', M2_ABORT_TEXT=MARK['abort'])
        args = [os.path.join(root, 'tests', 'model2_serial.py'), '--port', 'SIM', '--out', 'runs', '--elf', elf]
        if name == 'idle':
            args.append('--probe')
        p = run_tool(args + [path], folder, e)
        rep_path = os.path.join(folder, 'run_report.txt')
        rep = read_text(rep_path) if os.path.isfile(rep_path) else ''
        status = re.search(r'^status (\w+)', rep, re.M)
        want = {'ok': 'ok', 'abort': 'aborted', 'limit': 'aborted'}.get(name, 'error')
        check(bool(status) and status[1] == want, f'{name}: run_report.txt says status {want}: {status[1] if status else None} (exit {p.returncode})')
        if name in ('value_error', 'script_error'):
            line = line_of(src, 'int("' + MARK['text'] if name == 'value_error' else '9999')
            check(f'{MARK["script"]}.py:{line}: error:' in p.stdout, f'{name}: the console names the script line {line} of the runtime error')
        shown = [m for m in (MARK['var'], MARK['text'], MARK['abort'], MARK['expect'], MARK['script']) if m in p.stdout]
        check(bool(shown) if name != 'ok' else True, f'{name}: the console of the run shows markers {shown}: the customer sees his own names there')
        # run_report.txt is the input of report.py, not the report: it holds the script base name by design (run_script.py docstring)
        scan(f'{name}: run_report.txt, except the script base name', rep, allowed=(MARK['script'],))
        check(f'script {MARK["script"]}.py' in rep, f'{name}: run_report.txt has the script base name, as documented: report.py has to drop it')
        out = os.path.join(folder, 'mini-scope_report.txt')
        r = run_tool([os.path.join(tools, 'report.py'), '--doctor', doctor, '--hw', hw, '--run', rep_path, '--out', out], folder, env)
        check(r.returncode == 0 and os.path.isfile(out), f'{name}: report.py wrote its file')
        # F1, fixed: the console output names the file, never the folder of the user
        scan(f'{name}: console output of report.py', r.stdout)
        scan(f'{name}: mini-scope_report.txt', read_text(out))
        check(f'status {want}' in read_text(out), f'{name}: the report has the status of the run: {want}')
    p = run_tool(['-c', 'import os; print(os.environ["USERNAME"], os.path.expanduser("~"))'], root, env)
    check(MARK['user'] in p.stdout, f'the child processes see the marker user name and home: {p.stdout.strip()[-50:]}')


TESTS = {'adapt': test_adapt, 'inverted': test_inverted, 'rules': lambda: (test_rules_stage13(), test_rules_stage4(), test_rules_complete()),
         'markers': test_markers}

if __name__ == '__main__':
    t0 = time.time()
    for name, fn in TESTS.items():
        if len(sys.argv) < 2 or name in sys.argv[1:]:
            fn()
    shutil.rmtree(TMP, ignore_errors=True)
    print(f'{time.time() - t0:.0f} s, ' + ('FAILED' if getattr(check, 'failed', False) else 'all passed'))
    sys.exit(1 if getattr(check, 'failed', False) else 0)
