"""test_check.py - gui/tune_check.py: stages 1 to 3 of "Script errors", and their use in tune_api.run().

    python tests/test_check.py

One test per rule: a small valid script (BASE) gets one change, the problems must be exactly the expected codes
at the expected lines. The appendix script of the plan (scripts/tune_startup.py) is checked as it is.
"""
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, '..', 'gui'), os.path.join(HERE, '..', 'tools')]
import tune_api                                   # noqa: E402
import tune_check                                 # noqa: E402
from elf_vars import VarTable, VarError           # noqa: E402
from scope_gui import Scope                       # noqa: E402
from sim_serial import SimSerial, SYMBOLS, SYMBOL_TYPES   # noqa: E402

FIX = os.path.join(HERE, 'fixtures')
SCRIPT = os.path.join(HERE, '..', 'scripts', 'tune_startup.py')
SIM = VarTable.from_symbols(SYMBOLS, SYMBOL_TYPES)
ELF = VarTable.from_elf(os.path.join(FIX, 'vars.elf'))
SIM_ELF = os.path.join(HERE, '..', 'test.X', 'dist', 'sim', 'production', 'test.X.production.elf')
TMP = tempfile.mkdtemp(prefix='tune_check_')
tune_check.GRACE_S = 0.5            # the tests do not wait for the full grace time
BUDGET = dict(max_calls=2000, max_seconds=1.0)

BASE = '''from tune_api import *

VAR_RUN   = "motor.run"
VAR_SPEED = "motor.speed"
VAR_STATE = "motor.state"
VAR_RAMP  = "startup.ramp_rate"


def check_idle():
    expect(read(VAR_SPEED) == 0, "must stand still")


def main():
    channels(VAR_SPEED, VAR_STATE)
    allow(VAR_RUN, 0, 1)
    allow(VAR_RAMP, 1, 500)
    safe_value(VAR_RUN, 0)
    safe_value(VAR_RAMP, 100)
    unit(VAR_SPEED, 0.5, "rpm")
    unit(VAR_STATE, 1, "state")
    table("ramp", "good")
    stream_start()
    write(VAR_RAMP, 100)
    write(VAR_RUN, 1)
    trace_begin()
    wait_ms(10)
    trace_end("run %d", 1)
    row(100, 1)
    printf("speed %d\\n", read(VAR_SPEED))
    write(VAR_RUN, 0)
'''


class Mixed:
    """Names in `elf_names` come from the ELF fixture, all others from the SIM table."""

    def __init__(self, elf, elf_names):
        self.elf, self.names = elf, set(elf_names)

    def lookup(self, name, require_ram=True):
        return (self.elf if name in self.names else SIM).lookup(name, require_ram)


class Raising:
    """lookup() of one name raises this VarError rule, all others come from the SIM table."""

    def __init__(self, name, rule):
        self.name, self.rule = name, rule

    def lookup(self, name, require_ram=True):
        if name == self.name:
            raise VarError(self.rule, f'{name}: test error {self.rule}')
        return SIM.lookup(name, require_ram)


def check(cond, text):
    print(('OK   ' if cond else 'FAIL ') + text)
    if not cond:
        check.failed = True


def mutate(edits, base=BASE):
    """edits: (old, new); old is found in exactly one line, new may hold several lines, None deletes the line."""
    lines = base.split('\n')
    for old, new in edits:
        hit = [i for i, x in enumerate(lines) if x is not None and old in x]
        assert len(hit) == 1, f'{old!r} is in {len(hit)} lines'
        lines[hit[0]] = None if new is None else lines[hit[0]].replace(old, new)
    return '\n'.join(x for x in lines if x is not None)


def line_of(src, marker):
    return next(i for i, x in enumerate(src.split('\n'), 1) if marker in x)


_n = [0]


def write_script(src, name=None):
    _n[0] += 1
    folder = os.path.join(TMP, f's{_n[0]}')
    os.makedirs(folder)
    path = os.path.join(folder, name or 't.py')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(src)
    return path


def run_check(src, table=SIM, **kw):
    return tune_check.check_script(write_script(src), table, **{**BUDGET, **kw})


def case(name, edits, want, table=SIM, base=BASE, **kw):
    """want: [(code, marker)], marker identifies the line in the changed script. Exactly these problems."""
    src = mutate(edits, base)
    got = sorted((p.code, p.line) for p in run_check(src, table, **kw))
    exp = sorted((c, line_of(src, m)) for c, m in want)
    check(got == exp, f'{name}: {got}' + ('' if got == exp else f'  expected {exp}'))
    return src


# -- the interface -----------------------------------------------------------------------------------------------

def test_interface():
    check(run_check(BASE) == [], 'BASE has no problems')
    p = tune_check.Problem('E203', 'error', 'tune_startup.py', 19, 'text', 2)
    check(str(p) == 'tune_startup.py:19: error: text', f'str(problem): {p}')
    check(p._fields == ('code', 'severity', 'file', 'line', 'text', 'stage'), f'fields: {p._fields}')
    bad = [c for c, (sev, st, text) in tune_check.RULES.items()
           if not (c[0] == {'error': 'E', 'warning': 'W'}[sev] and st in (1, 2, 3, 4) and text)]
    check(not bad, f'RULES: code letter matches severity, stage 1 to 4, meaning given ({len(tune_check.RULES)} codes)')
    codes = sorted(tune_check.RULES)
    print('     codes: ' + ' '.join(codes))
    check(all(c in tune_check.RULES for c in ('E218', 'E219', 'E227', 'E228', 'W203', 'E314', 'E315', 'E316')), 'the capture rules are in the table')
    check(set(tune_check.VAR_RULE.values()) <= set(tune_check.RULES), 'every VarError mapping is a known code')
    check(all(p.stage == 1 for p in run_check('x = = 1\n')), 'a syntax error is stage 1')
    check(run_check(BASE.replace('check_idle', 'idle_check'))[0].file == 't.py', 'file is the base name')


# -- capture mode (card 6.1) -------------------------------------------------------------------------------------

CAP = '''from tune_api import *

VAR_REF  = "motor.current_ref"
VAR_CUR  = "motor.current"
VAR_KP   = "motor.current_kp"


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
FLOAT = VarTable.from_symbols({**SYMBOLS, 'probef': (SYMBOLS['motor.kp'][0], 4)}, {**SYMBOL_TYPES, 'probef': 'f32'})


def test_capture_rules():
    def cs(name, edits, want, **kw):
        return case(name, edits, want, base=CAP, **kw)

    check(run_check(CAP) == [], 'CAP has no problems')
    cs('E218 f32 trigger', [('capture_trigger(VAR_REF, RISING, 250, 10)', 'capture_trigger("probef", RISING, 250, 10)'),
                            ('capture_channels(VAR_REF, VAR_CUR)', 'capture_channels(VAR_REF, VAR_CUR, "probef")')],
       [('E218', 'capture_trigger("probef"'), ('W202', 'capture_channels('), ('W205', 'capture_channels(')], table=FLOAT)
    cs('E219 transfer at 9600 baud', [], [('E219', 'capture_start()')], baud=9600)
    cs('57600 baud is accepted', [], [], baud=57600)
    cs('E227 period 0', [('capture_period_us(100)', 'capture_period_us(0)')], [('E227', 'capture_period_us(0)')])
    cs('E227 period 70000', [('capture_period_us(100)', 'capture_period_us(70000)')], [('E227', 'capture_period_us(70000)')])
    cs('E227 period 100.5', [('capture_period_us(100)', 'capture_period_us(100.5)')], [('E227', 'capture_period_us(100.5)')])
    cs('E229 period 9', [('capture_period_us(100)', 'capture_period_us(9)')], [('E229', 'capture_period_us(9)')])
    cs('E229 period 1', [('capture_period_us(100)', 'capture_period_us(1)')], [('E229', 'capture_period_us(1)')])
    cs('W206 period 10', [('capture_period_us(100)', 'capture_period_us(10)')], [('W206', 'capture_period_us(10)')])
    cs('W206 period 49', [('capture_period_us(100)', 'capture_period_us(49)')], [('W206', 'capture_period_us(49)')])
    cs('period 50 has no warning', [('capture_period_us(100)', 'capture_period_us(50)')], [])
    cs('E228 edge', [('RISING', '3')], [('E228', 'capture_trigger')])
    cs('E228 level outside s16', [('250, 10', '70000, 10')], [('E228', 'capture_trigger')])
    cs('E228 pretrigger 120', [('250, 10', '250, 120')], [('E228', 'capture_trigger')])
    cs('E228 no trigger', [('capture_trigger(VAR_REF, RISING, 250, 10)', None)], [('E228', 'capture_start()')])
    cs('E228 trigger not in the channels', [('capture_trigger(VAR_REF', 'capture_trigger(VAR_KP')], [('E228', 'capture_trigger')])
    cs('E212 five channels', [('capture_channels(VAR_REF, VAR_CUR)', 'capture_channels(VAR_REF, VAR_CUR, VAR_KP, VAR_REF, VAR_CUR)')],
       [('E212', 'capture_channels'), ('W205', 'VAR_KP   =')])
    cs('E216 no capture_channels()', [('capture_channels(VAR_REF, VAR_CUR)', 'capture_channels()')],
       [('E216', 'capture_start()'), ('E228', 'capture_trigger'), ('E302', 'capture_rise_us'), ('E302', 'capture_max('),
        ('E302', 'capture_value')])
    cs('E214 no allow()', [('allow(VAR_REF, 0, 1000)', None), ('allow(VAR_KP, 0, 2000)', None)],
       [('E214', 'capture_start()'), ('E223', 'safe_value(VAR_REF'), ('E223', 'safe_value(VAR_KP'), ('E303', 'write(VAR_KP'),
        ('E303', 'write(VAR_REF, 500)'), ('E303', 'write(VAR_REF, 0)')])
    cs('W203 limit() in capture mode', [('allow(VAR_REF, 0, 1000)', 'allow(VAR_REF, 0, 1000)\n    limit(VAR_CUR, 1500)')], [('W203', 'limit(VAR_CUR')])
    cs('E224 limit() of a variable that is not captured', [('allow(VAR_REF, 0, 1000)', 'allow(VAR_REF, 0, 1000)\n    limit(VAR_KP, 1500)')],
       [('E224', 'limit(VAR_KP'), ('W203', 'limit(VAR_KP')])
    cs('E314 read() in capture mode', [('write(VAR_KP, 100)', 'write(VAR_KP, 100)\n    x = read(VAR_CUR)  # READ')], [('E314', '# READ')])
    cs('E315 stream call: trace_begin()', [('capture_start()', 'trace_begin()  # MIX\n    capture_start()')], [('E315', '# MIX')])
    cs('E315 stream call: channels()', [('capture_period_us(100)', 'channels(VAR_CUR)  # MIX\n    capture_period_us(100)')], [('E315', '# MIX')])
    cs('E315 stream call: stream_start()', [('write(VAR_KP, 100)', 'stream_start()  # MIX')], [('E315', '# MIX')])
    case('E315 capture call in a script that streams', [('trace_begin()', 'capture_arm()  # MIX\n    trace_begin()')], [('E315', '# MIX')])
    cs('E316 capture_arm() twice', [('write(VAR_REF, 500)', 'capture_arm()  # TWICE\n    write(VAR_REF, 500)')], [('E316', '# TWICE')])
    cs('E316 capture_wait() without capture_arm()', [('write(VAR_KP, 100)', 'capture_wait(100)  # EARLY\n    write(VAR_KP, 100)')], [('E316', '# EARLY')])
    cs('E316 capture_max() before capture_wait()', [('write(VAR_KP, 100)', 'x = capture_max(VAR_CUR)  # EARLY\n    write(VAR_KP, 100)')],
       [('E316', '# EARLY')])
    cs('E306 capture_arm() before capture_start()', [('capture_start()', 'capture_arm()  # EARLY\n    capture_start()')], [('E306', '# EARLY')])
    cs('E306 write() before capture_start()', [('capture_start()', 'write(VAR_KP, 100)  # EARLY\n    capture_start()')], [('E306', '# EARLY')])
    cs('E307 capture_period_us() after capture_start()', [('write(VAR_KP, 100)', 'capture_period_us(50)  # LATE')], [('E307', '# LATE')])
    cs('E309 capture_start() twice', [('write(VAR_KP, 100)', 'capture_start()  # TWICE')], [('E309', '# TWICE')])
    cs('E308 trace_failed() without a capture', [('write(VAR_KP, 100)', 'trace_failed()  # EARLY\n    write(VAR_KP, 100)')], [('E308', '# EARLY')])
    cs('E311 capture_save() format', [('capture_save("kp %d", 100)', 'capture_save("kp %d %d", 100)')], [('E311', 'capture_save')])
    cs('E312 capture_wait() with text', [('capture_wait(500)', 'capture_wait("x")')], [('E312', 'capture_wait')])
    cs('E302 capture_max() of a variable that is not captured', [('capture_max(VAR_CUR)', 'capture_max(VAR_KP)')], [('E302', 'capture_max')])
    rec = tune_check.record(write_script(CAP), SIM, **BUDGET)
    names = [c[1] for c in rec.calls]
    check(not rec.problems and names.count('capture_arm') == 1 and 'capture_wait' in names and 'capture_save' in names,
          f'dry run of CAP: no problem, {len(rec.calls)} calls recorded')
    check([v.name for v in rec.declared['chans']] == ['motor.current_ref', 'motor.current'], 'dry run: the capture channels are the declared channels')


def test_capture_script():
    path = os.path.join(HERE, '..', 'scripts', 'tune_current.py')
    with open(path, encoding='utf-8') as f:
        src = f.read()
    want = sorted([('W204', line_of(src, 'VAR_KP          =')), ('W204', line_of(src, 'VAR_KI          =')), ('W203', line_of(src, 'limit(VAR_CURRENT'))])
    tables = [('SIM table', SIM)]
    if os.path.isfile(SIM_ELF):
        tables.append(('ELF of test.X', VarTable.from_elf(SIM_ELF)))
    for name, t in tables:
        t0 = time.time()
        problems = tune_check.check_script(path, t)
        got = sorted((p.code, p.line) for p in problems)
        check(got == want, f'tune_current.py on the {name}: no errors, W203 and 2 x W204 ({time.time() - t0:.2f} s): {got}')
        rec = tune_check.record(path, t)
        names = [c[1] for c in rec.calls]
        check(not rec.stopped and names.count('capture_start') == 1 and 'capture_wait' in names and names.count('capture_save') == 4 + 3,
              f'{name}: dry run ended by itself, {len(rec.calls)} calls, {names.count("capture_save")} capture_save()')


# -- stage 1 -----------------------------------------------------------------------------------------------------

def test_stage1():
    case('E101 syntax error', [('write(VAR_RUN, 1)', 'write(VAR_RUN, 1))')], [('E101', 'write(VAR_RUN, 1))')])
    case('E101 bad indentation', [('trace_begin()', ' trace_begin()')], [('E101', ' trace_begin()')])
    case('E102 mistyped function', [('write(VAR_RUN, 1)', 'wirte(VAR_RUN, 1)')], [('E102', 'wirte(')])
    case('E102 mistyped constant', [('write(VAR_RUN, 1)', 'write(VAR_RUNN, 1)')], [('E102', 'VAR_RUNN')])
    src = mutate([('write(VAR_RUN, 1)', 'wirte(VAR_RUN, wirte(VAR_RUN, 1))')])
    got = [(p.code, p.line) for p in run_check(src)]
    check(got == [('E102', line_of(src, 'wirte('))], f'the same name twice on one line is reported once: {got}')
    p = run_check(mutate([('write(VAR_RUN, 1)', 'wirte(VAR_RUN, 1)')]))[0]
    check("did you mean write" in p.text, f'E102 suggests the name: {p}')
    p102 = run_check(mutate([('from tune_api import *', None)]))
    check(len(p102) >= 15 and {p.code for p in p102} == {'E102'}, f'without the star import every API call is an undefined name: {len(p102)} x E102')
    case('E103 used before assigned', [('def check_idle', 'LEVEL = 5\n\n\ndef helper():\n    print(LEVEL)\n    LEVEL = 6\n\n\ndef check_idle')],
         [('E103', 'print(LEVEL)')])
    case('E104 no main()', [('def main', 'def mian')], [('E104', 'from tune_api')])
    case('E105 no check_idle()', [('def check_idle', 'def check_idel')], [('E105', 'from tune_api')])
    # unused variables stay out
    check(run_check(mutate([('trace_begin()', 'unused = 5\n    trace_begin()')])) == [], 'an unused variable is no problem')


def test_star_trap():
    """With `from tune_api import *` pyflakes does not report undefined names. HANDLE_STAR is the switch."""
    from pyflakes import checker, messages
    import ast
    src = mutate([('write(VAR_RUN, 1)', 'wirte(VAR_RUN, 1)'), ('write(VAR_RAMP, 100)', 'write(VAR_RAMPP, 100)')])
    raw = checker.Checker(ast.parse(src), 't.py').messages
    msgs = [str(m) for m in raw]
    print('     raw pyflakes on the unchanged tree:')
    for m in msgs[:4]:
        print('       ' + m)
    check(msgs and not any(isinstance(m, messages.UndefinedName) for m in raw), 'pyflakes alone reports no undefined name, only star import notes')
    want = sorted([('E102', line_of(src, 'wirte(')), ('E102', line_of(src, 'VAR_RAMPP'))])
    got = sorted((p.code, p.line) for p in run_check(src))
    check(got == want, f'handled: mistyped function and mistyped constant are errors at their lines: {got}')
    tune_check.HANDLE_STAR = False
    try:
        off = [(p.code, p.line) for p in run_check(src)]
    finally:
        tune_check.HANDLE_STAR = True
    check(not any(c == 'E102' for c, _ in off), f'star handling switched off: no E102 any more (the test above would fail): {off}')


# -- stage 2: variables ------------------------------------------------------------------------------------------

def test_stage2_elf():
    """Real VarErrors from the DWARF fixture, one per rule. The name is on a VAR_ line: the error is reported there."""
    cases = [('E201', 'motr.run', 'did you mean'), ('E202', 'motor', 'name a member'), ('E203', 'grid', 'name an element'),
             ('E204', 'motor.ptr', 'pointer'), ('E205', 'motor.flags.ready', 'bit field'), ('E206', 'motor.wide', 'bytes wide'),
             ('E207', 'table_c[1]', 'not in RAM'), ('E208', 'motor.run.x', 'no member'), ('E208', 'motor.phase[3].current', 'out of range'),
             ('E209', 'counter', 'exists')]
    for code, name, word in cases:
        src = mutate([('"motor.run"', f'"{name}"')])
        t = Mixed(ELF, [name])
        problems = run_check(src, t)
        got = [(p.code, p.line) for p in problems]
        check(got == [(code, line_of(src, 'VAR_RUN   ='))], f'{code} {name}: {got}')
        check(problems and (word in problems[0].text or word.split()[0] in problems[0].text), f'{code} text: {problems[0].text if problems else None}')
    nodebug = VarTable.from_elf(os.path.join(FIX, 'vars_nodebug.elf'))
    src = mutate([('"motor.run"', '"motor.speed2"')])
    check([p.code for p in run_check(src, Mixed(nodebug, ['motor.speed2']))] == ['E210'], 'E210 ELF without debug information')


def test_stage2_rules_mapping():
    for rule, code in tune_check.VAR_RULE.items():
        got = [(p.code, p.line) for p in run_check(BASE, Raising('motor.run', rule))]
        check(got == [(code, line_of(BASE, 'VAR_RUN   ='))], f'VarError {rule} -> {code}: {got}')
    got = [p.code for p in run_check(BASE, Raising('motor.run', 'something_new'))]
    check(got == ['E208'], f'an unknown VarError rule -> E208: {got}')


def test_stage2_names_without_constant():
    src = mutate([('write(VAR_RUN, 1)', 'write("motor.runn", 1)')])
    got = [(p.code, p.line) for p in run_check(src)]
    check(got == [('E201', line_of(src, 'motor.runn'))], f'a name written in the call: the error is at the call: {got}')
    src = mutate([('"motor.run"', '"motor.runn"')])
    got = [(p.code, p.line) for p in run_check(src)]
    check(got == [('E201', line_of(src, 'VAR_RUN   ='))], f'a name from a VAR_ line, used 4 times: one error at the VAR_ line: {got}')
    p = run_check(src)[0]
    check('motor.run' in p.text and 'did you mean' in p.text, f'suggestion in the text: {p}')


def test_stage2_declarations():
    case('E212 more channels than fit', [('channels(VAR_SPEED, VAR_STATE)', 'channels(VAR_SPEED, VAR_STATE, "motor.iq", "motor.fault", "motor.kp")'),
                                          ('unit(VAR_STATE, 1, "state")', 'unit(VAR_STATE, 1, "state")\n    unit("motor.iq", 1, "")\n'
                                           '    unit("motor.fault", 1, "")\n    unit("motor.kp", 1, "")')],
         [('E212', 'channels(')], baud=1_000_000)
    case('E213 UART load', [], [('E213', 'stream_start()')], baud=57600)
    case('E213 passes at 115200 with 4 channels', [('channels(VAR_SPEED, VAR_STATE)', 'channels(VAR_SPEED, VAR_STATE, "motor.iq", "motor.fault")'),
                                                   ('unit(VAR_STATE, 1, "state")', 'unit(VAR_STATE, 1, "state")\n    unit("motor.iq", 1, "")\n'
                                                    '    unit("motor.fault", 1, "")')], [])
    case('E214 no allow()', [('allow(VAR_RUN, 0, 1)', None), ('allow(VAR_RAMP, 1, 500)', None), ('safe_value(VAR_RUN, 0)', None),
                             ('safe_value(VAR_RAMP, 100)', None), ('write(VAR_RAMP, 100)', None), ('write(VAR_RUN, 1)', None),
                             ('write(VAR_RUN, 0)', None)],
         [('E214', 'stream_start()'), ('E215', 'stream_start()')])
    case('E215 no safe_value()', [('safe_value(VAR_RUN, 0)', None), ('safe_value(VAR_RAMP, 100)', None)],
         [('E215', 'stream_start()'), ('W204', 'VAR_RUN   ='), ('W204', 'VAR_RAMP  =')])
    case('E216 no channels()', [('channels(VAR_SPEED, VAR_STATE)', None), ('unit(VAR_SPEED, 0.5, "rpm")', None),
                                ('unit(VAR_STATE, 1, "state")', None), ('printf("speed %d\\n", read(VAR_SPEED))', None)],
         [('E216', 'stream_start()')])
    case('E217 no heartbeat variable', [], [('E217', 'stream_start()')],
         table=VarTable.from_symbols({k: v for k, v in SYMBOLS.items() if k != 'tune_alive'}, SYMBOL_TYPES))
    case('E220 allow() outside the C type', [('allow(VAR_RAMP, 1, 500)', 'allow(VAR_RAMP, 1, 70000)')], [('E220', 'allow(VAR_RAMP')])
    case('E221 allow() min above max', [('allow(VAR_RAMP, 1, 500)', 'allow(VAR_RAMP, 500, 1)')], [('E221', 'allow(VAR_RAMP')])
    case('E222 safe_value() outside allow()', [('safe_value(VAR_RAMP, 100)', 'safe_value(VAR_RAMP, 900)')], [('E222', 'safe_value(VAR_RAMP')])
    case('E223 safe_value() without allow()', [('safe_value(VAR_RAMP, 100)', 'safe_value(VAR_RAMP, 100)\n    safe_value("motor.kp", 0)')],
         [('E223', 'safe_value("motor.kp"')])
    case('E224 limit() not measured', [('safe_value(VAR_RUN, 0)', 'limit(VAR_RAMP, 100)\n    safe_value(VAR_RUN, 0)')], [('E224', 'limit(VAR_RAMP')])
    case('E225 safe_value() not whole', [('safe_value(VAR_RAMP, 100)', 'safe_value(VAR_RAMP, 100.5)')], [('E225', 'safe_value(VAR_RAMP')])
    case('E226 negative limit()', [('safe_value(VAR_RUN, 0)', 'limit(VAR_SPEED, -5)\n    safe_value(VAR_RUN, 0)')], [('E226', 'limit(VAR_SPEED')])
    case('limit() on a measured variable is fine', [('safe_value(VAR_RUN, 0)', 'limit(VAR_SPEED, 5)\n    safe_value(VAR_RUN, 0)')], [])


def test_stage2_warnings():
    case('W201 written, not volatile', [('"motor.run"', '"plain.run"')], [('W201', 'VAR_RUN   =')], table=Mixed(ELF, ['plain.run']))
    case('W201 volatile is fine', [('"motor.run"', '"motor.run"')], [], table=Mixed(ELF, ['motor.run']))
    case('W202 32-bit variable', [('"motor.speed"', '"motor.pos"')], [('W202', 'VAR_SPEED =')], table=Mixed(ELF, ['motor.pos']))
    case('W204 written, no safe_value()', [('safe_value(VAR_RAMP, 100)', None)], [('W204', 'VAR_RAMP  =')])
    case('W205 measured, no unit()', [('unit(VAR_STATE, 1, "state")', None)], [('W205', 'VAR_STATE =')])
    check(all(p.severity == 'warning' for p in run_check(mutate([('unit(VAR_STATE, 1, "state")', None)]))), 'warnings are warnings')


# -- stage 3 -----------------------------------------------------------------------------------------------------

def test_stage3():
    case('E301 value outside allow()', [('write(VAR_RAMP, 100)', 'write(VAR_RAMP, 600)')], [('E301', 'write(VAR_RAMP')])
    case('E301 once for a loop', [('write(VAR_RAMP, 100)', 'for n in range(5):\n        write(VAR_RAMP, 600 + n)')], [('E301', 'write(VAR_RAMP')])
    case('E302 read() not measured', [('wait_ms(10)', 'wait_ms(10)\n    read("motor.fault")')], [('E302', 'read("motor.fault")')])
    case('E303 write() not allowed', [('wait_ms(10)', 'wait_ms(10)\n    write("motor.kp", 1)')], [('E303', 'write("motor.kp"')])
    case('E304 row() wrong count', [('row(100, 1)', 'row(100)')], [('E304', 'row(100)')])
    case('E305 row() without table()', [('table("ramp", "good")', None)], [('E305', 'row(100, 1)')])
    case('E305 table() twice', [('stream_start()', 'table("again")\n    stream_start()')], [('E305', 'table("again")')])
    case('E306 write() before stream_start()', [('stream_start()', 'write(VAR_RUN, 0)\n    stream_start()')], [('E306', 'write(VAR_RUN, 0)')])
    case('E306 trace_begin() before stream_start()', [('stream_start()', 'trace_begin()\n    trace_end("x")\n    stream_start()')],
         [('E306', 'trace_begin()')])
    case('E307 declaration after stream_start()', [('write(VAR_RAMP, 100)', 'channels(VAR_STATE)\n    write(VAR_RAMP, 100)')], [('E307', 'channels(VAR_STATE)')])
    case('E308 trace_end() without trace_begin()', [('trace_begin()', None)], [('E308', 'trace_end(')])
    case('E308 trace_begin() twice', [('trace_begin()', 'trace_begin()\n    trace_begin()  # again')], [('E308', '# again')])
    case('E308 trace_failed() without trace', [('wait_ms(10)', 'wait_ms(10)\n    trace_end("x")\n    trace_failed()\n    trace_begin()')],
         [('E308', 'trace_failed()')])
    case('E309 stream_start() twice', [('write(VAR_RAMP, 100)', 'stream_start()  # again\n    write(VAR_RAMP, 100)')], [('E309', '# again')])
    case('E310 define() name', [('write(VAR_RUN, 0)', 'define("1abc", 3)')], [('E310', 'define(')])
    case('E311 printf() format', [('printf("speed %d\\n", read(VAR_SPEED))', 'printf("speed %d %d\\n", read(VAR_SPEED))')], [('E311', 'printf(')])
    case('E311 trace_end() format', [('trace_end("run %d", 1)', 'trace_end("run %d")')], [('E311', 'trace_end(')])
    case('E312 not a number', [('wait_ms(10)', 'wait_ms("10")')], [('E312', 'wait_ms(')])
    case('E312 limit() with a string', [('safe_value(VAR_RUN, 0)', 'limit(VAR_SPEED, "5")\n    safe_value(VAR_RUN, 0)')], [('E312', 'limit(VAR_SPEED')])
    case('E313 name not a string', [('wait_ms(10)', 'wait_ms(10)\n    read(5)')], [('E313', 'read(5)')])
    src = case('E390 missing argument', [('write(VAR_RUN, 1)', 'write(VAR_RUN)')], [('E390', 'write(VAR_RUN)')])
    p = run_check(src)[0]
    check('write() missing 1 required positional argument' in p.text, f'E390 text: {p}')
    case('E390 error in a helper: the line of the call in the script', [('def main', 'def helper():\n    return 1 / 0\n\n\ndef main'),
                                                                        ('wait_ms(10)', 'helper()')], [('E390', 'return 1 / 0')])
    case('E390 error in check_idle', [('expect(read(VAR_SPEED) == 0, "must stand still")', 'expect(read(VAR_SPEED) == 0)')],
         [('E390', 'expect(read(VAR_SPEED) == 0)')])
    case('read() of any variable in check_idle() is fine', [('read(VAR_SPEED) == 0', 'read("motor.fault") == 0')], [])
    case('expect() false is no error', [('read(VAR_SPEED) == 0', 'read(VAR_SPEED) == 1')], [])
    # a stage 3 problem is reported once, also when stage 2 finds the same thing
    src = mutate([('write(VAR_RAMP, 100)', 'for n in range(50):\n        write("motor.kp", n)')])
    check([p.code for p in run_check(src)] == ['E303'], '50 bad writes from one line: one E303')


def test_budget():
    src = mutate([('row(100, 1)', 'row(100, 1)\n    while read(VAR_STATE) != 3:\n        wait_ms(10)')])
    rec = tune_check.record(write_script(src), SIM, **BUDGET)
    lines = {line_of(src, 'while read'), line_of(src, '        wait_ms(10)')}
    w = [p for p in rec.problems if p.code == 'W301']
    check(len(rec.problems) == 1 and len(w) == 1 and w[0].line in lines and 'API calls' in w[0].text and 'main()' in w[0].text,
          f'endless loop with API calls: {w[0] if w else rec.problems}')
    check(rec.stopped and 1900 < len(rec.calls) < 2100, f'stopped at the call budget: {len(rec.calls)} calls recorded')

    t0 = time.time()
    src = mutate([('row(100, 1)', 'row(100, 1)\n    while True:\n        pass')])
    rec = tune_check.record(write_script(src), SIM, max_calls=2000, max_seconds=0.5)
    dt = time.time() - t0
    w = [p for p in rec.problems if p.code == 'W301']
    check(len(rec.problems) == 1 and w and w[0].line in (line_of(src, 'while True'), line_of(src, 'pass')) and '0.5 s' in w[0].text, f'endless loop without API calls: {w[0] if w else rec.problems}')
    check(dt < 2.0, f'... and it took {dt:.2f} s')
    check(not [t for t in threading.enumerate() if t.name == 'tune-check'], 'the checker thread has ended')

    src = mutate([('row(100, 1)', 'row(100, 1)\n    while True:\n        pass'), ('expect(read(VAR_SPEED) == 0, "must stand still")',
                                                                                 'expect(read(VAR_SPEED) == 0)')])
    got = sorted(p.code for p in tune_check.record(write_script(src), SIM, max_calls=2000, max_seconds=0.5).problems)
    check(got == ['W301'], f'after a stop by time check_idle() is not run: {got}')
    src = mutate([('row(100, 1)', 'row(100, 1)\n    while True:\n        wait_ms(1)'), ('expect(read(VAR_SPEED) == 0, "must stand still")',
                                                                                    'expect(read(VAR_SPEED) == 0)')])
    got = sorted(p.code for p in tune_check.record(write_script(src), SIM, **BUDGET).problems)
    check(got == ['E390', 'W301'], f'after a stop by calls check_idle() still runs: {got}')

    src = mutate([('row(100, 1)', 'row(100, 1)\n    import time\n    time.sleep(3)')])
    t0 = time.time()
    rec = tune_check.record(write_script(src), SIM, max_calls=2000, max_seconds=0.3)
    w = [p for p in rec.problems if p.code == 'W301']
    check(w and w[0].line == line_of(src, 'time.sleep') and 'still blocked' in w[0].text and time.time() - t0 < 2.5,
          f'a blocking call is reported at its line: {w[0] if w else rec.problems}')
    time.sleep(3)


def test_swallowed_stop():
    src = mutate([('row(100, 1)', 'row(100, 1)\n    while True:\n        try:\n            wait_ms(1)\n        except BaseException:\n            pass')])
    t0 = time.time()
    rec = tune_check.record(write_script(src), SIM, max_calls=2000, max_seconds=0.3)
    w = [p for p in rec.problems if p.code == 'W301']
    check(w and time.time() - t0 < 3, f'a script that swallows the stop does not hang the check: {w[0] if w else rec.problems}')


# -- nothing left behind -----------------------------------------------------------------------------------------

def test_clean():
    path = write_script(BASE)
    before = dict(modules=set(sys.modules), api=dict(vars(tune_api)), threads=sorted(t.name for t in threading.enumerate()),
                  files=sorted(os.listdir(os.path.dirname(path))), cwd=sorted(os.listdir('.')))
    for _ in range(3):
        tune_check.check_script(path, SIM, **BUDGET)
    tune_check.check_script(SCRIPT, SIM)
    after = dict(modules=set(sys.modules), api=dict(vars(tune_api)), threads=sorted(t.name for t in threading.enumerate()),
                 files=sorted(os.listdir(os.path.dirname(path))), cwd=sorted(os.listdir('.')))
    check(after['modules'] == before['modules'], f'sys.modules unchanged ({len(before["modules"])} modules)')
    check(sys.modules['tune_api'] is tune_api, 'sys.modules["tune_api"] is the real module')
    check(after['api'] == before['api'], 'tune_api module dict unchanged (same objects)')
    check(tune_api._active is None and not hasattr(tune_api._local, 'session'), 'no active session, no session on this thread')
    check(after['threads'] == before['threads'], f'no thread left: {after["threads"]}')
    check(after['files'] == before['files'] and after['cwd'] == before['cwd'], 'no file written')
    # the script cannot reach the real API: even import tune_api inside the dry run gives the recorder
    src = mutate([('def main', 'import tune_api as api_module\n\n\ndef main'), ('write(VAR_RAMP, 100)', 'api_module.write(VAR_RAMP, 100)')])
    check(run_check(src) == [], 'import tune_api as module also works on the recorder')


class Counting(SimSerial):
    """What reaches the port."""

    def __init__(self):
        super().__init__()
        self.sent = []

    def write(self, data):
        self.sent.append(bytes(data))
        return super().write(data)


GOOD = '''from tune_api import *


def check_idle():
    expect(read("motor.state") == 0, "idle")


def main():
    channels("motor.speed", "motor.state", "motor.fault", "motor.run")
    allow("motor.run", 0, 1)
    safe_value("motor.run", 0)
    allow("startup.ramp_rate", 1, 500)
    unit("motor.speed", 0.5, "rpm")
    stream_start()
    write("motor.run", 1)
    wait_ms(50)
    write("motor.run", 0)
    printf("done\\n")
'''


def test_run():
    tune_api.TRIAL_MS = 300
    out = os.path.join(TMP, 'runs')
    # 1. an error: refused, nothing reaches the port, no run folder
    path = write_script(GOOD.replace('write("motor.run", 1)', 'wirte("motor.run", 1)'))
    ser = Counting()
    sc = Scope(ser)
    try:
        tune_api.run(path, sc, SIM, out_dir=out)
        check(False, 'run() refuses a script with an error')
    except tune_api.RunRefused as e:
        codes = [p.code for p in e.problems]
        check(codes == ['E102'] and 't.py:' in str(e) and 'E102' in str(e), f'RunRefused with problems {codes}: {str(e)[:90]!r}')
    check(ser.sent == [] and not os.path.exists(out) and tune_api._active is None, 'nothing sent, no run folder, no active run')
    # 2. a script without check_idle() is refused too
    try:
        tune_api.run(write_script(GOOD.replace('def check_idle', 'def check_idel')), sc, SIM, out_dir=out)
        check(False, 'no check_idle()')
    except tune_api.RunRefused as e:
        check([p.code for p in e.problems if p.severity == 'error'] == ['E105'] and ser.sent == [], 'no check_idle(): refused with E105')
    # 3. skip_checks keeps a small script working
    path = write_script('from tune_api import *\ndef main():\n    channels("motor.speed")\n    stream_start()\n')
    sc.ping()
    s = tune_api.run(path, sc, SIM, out_dir=out, hooks={'skip_checks': True, 'skip_probe': True})
    r = s.wait(30)
    check(r is not None and 'stream_start() refused' in r['reason'] and r['rule_codes'] == {}, f'skip_checks: no checks, rule_codes {r and r["rule_codes"]}')
    # 4. a good script after the dry runs above: the real run works, warnings are logged and counted
    ev = []
    sc = Scope(SimSerial())
    sc.ping()
    s = tune_api.run(write_script(GOOD), sc, SIM, on_event=ev.append, out_dir=out, hooks={'skip_probe': True})
    r = s.wait(60)
    check(r is not None and r['status'] == 'ok', f'real run right after the dry runs: {r and (r["status"], r["reason"])}')
    check(r['rule_codes'] == {'W204': 1, 'W205': 3}, f'result rule_codes: {r["rule_codes"]}')
    log = [e['text'] for e in ev if e['type'] == 'log']
    warn = [x for x in log if 'warning' in x]
    check(len(warn) == 4 and all(x.startswith('t.py:') and x.endswith(']') for x in warn), f'warnings in the log: {warn[:2]}')
    with open(os.path.join(s.folder, 'run.txt'), encoding='utf-8') as f:
        txt = f.read()
    check(all(x in txt for x in warn), 'the warnings are in run.txt')
    check(all(isinstance(k, str) and isinstance(v, int) for k, v in r['rule_codes'].items()), 'rule_codes: only codes and counts')
    check(sys.modules['tune_api'] is tune_api and tune_api._active is None, 'state after the real run')


# -- the appendix script -----------------------------------------------------------------------------------------

def test_appendix():
    path = SCRIPT
    with open(path, encoding='utf-8') as f:
        src = f.read()
    want = sorted([('W205', line_of(src, '"motor.state"')), ('W205', line_of(src, '"motor.fault"')),
                   ('W204', line_of(src, '"startup.ramp_rate"')), ('W204', line_of(src, '"startup.align_current"')),
                   ('W204', line_of(src, '"startup.handover_speed"'))])
    tables = [('SIM table', SIM)]
    if os.path.isfile(SIM_ELF):
        tables.append(('ELF of test.X', VarTable.from_elf(SIM_ELF)))
    else:
        print('     (test.X/dist/sim/production/test.X.production.elf does not exist: ELF part skipped, build the sim configuration)')
    for name, t in tables:
        t0 = time.time()
        problems = tune_check.check_script(path, t)
        got = sorted((p.code, p.line) for p in problems)
        check(got == want, f'tune_startup.py on the {name}: no errors, the 5 expected warnings ({time.time() - t0:.2f} s)')
        for p in problems:
            print(f'       {p} [{p.code}]')
        rec = tune_check.record(path, t)
        names = [c[1] for c in rec.calls]
        check(not rec.stopped and 'stream_start' in names and 'wait_ms' in names and names.count('trace_end') == 4,
              f'{name}: dry run ended by itself, {len(rec.calls)} calls recorded')
        first = next(c for c in rec.calls if c[1] == 'channels')
        check(first[0] == line_of(src, '    channels(VAR_SPEED'), f'{name}: channels() recorded at line {first[0]}')


TESTS = {'capture_rules': test_capture_rules, 'capture_script': test_capture_script, 'interface': test_interface, 'stage1': test_stage1, 'star': test_star_trap, 'elf': test_stage2_elf,
         'mapping': test_stage2_rules_mapping, 'names': test_stage2_names_without_constant, 'declarations': test_stage2_declarations,
         'warnings': test_stage2_warnings, 'stage3': test_stage3, 'clean': test_clean, 'run': test_run, 'appendix': test_appendix,
         'budget': test_budget, 'swallow': test_swallowed_stop}

for name, fn in TESTS.items():
    if len(sys.argv) < 2 or name in sys.argv[1:]:
        print(f'--- {name}')
        fn()
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if getattr(check, 'failed', False) else 0)
