"""test_elf_vars.py - gui/elf_vars.py against tests/fixtures/vars.elf (built from vars.c and vars2.c, see build.txt).

    python tests/test_elf_vars.py

Needs no MPLAB X. The addresses are checked against the ELF symbol table (the same numbers xc-dsc-nm prints)
plus member offsets worked out by hand from the C layout of vars.c, so DWARF is compared with something else.
If test.X was built (configuration sim), the same is done for its variables.
"""
import os
import sys
import time

from elftools.elf.elffile import ELFFile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, '..', 'gui')]
from elf_vars import VarTable, VarError   # noqa: E402
from sim_serial import SYMBOLS, SYMBOL_TYPES   # noqa: E402

FIX = os.path.join(HERE, 'fixtures')
SIM_ELF = os.path.join(HERE, '..', 'test.X', 'dist', 'sim', 'production', 'test.X.production.elf')


def check(cond, text):
    print(('OK   ' if cond else 'FAIL ') + text)
    if not cond:
        check.failed = True


def symtab(path):
    """C name -> [addresses] of the ELF symbol table (the C compiler prefixes '_')."""
    out = {}
    with open(path, 'rb') as f:
        for s in ELFFile(f).get_section_by_name('.symtab').iter_symbols():
            if s['st_info']['type'] == 'STT_OBJECT' and s.name.startswith('_'):
                out.setdefault(s.name[1:], []).append(s['st_value'])
    return out


def err(t, expr, **kw):
    try:
        t.lookup(expr, **kw)
    except VarError as e:
        return e
    return None


def rule(t, expr, want, text=None, **kw):
    e = err(t, expr, **kw)
    check(e is not None and e.rule == want, text or f'{expr!r} -> {want}' + ('' if e is None else f'  ({e})' if e.rule == want else f' (got {e.rule})'))
    return e


t0 = time.perf_counter()
T = VarTable.from_elf(os.path.join(FIX, 'vars.elf'))
print(f'fixture loaded in {time.perf_counter() - t0:.3f} s')
check(T.has_debug, 'fixture has debug information')
S = symtab(os.path.join(FIX, 'vars.elf'))

# (expression, symbol, offset by hand from vars.c, width, type, volatile, const)
M, P = 'motor', 'plain'
GOOD = [
    ('motor.run', M, 0, 2, 'u16', True, False),             # typedef word_t -> uint16_t
    ('motor.speed', M, 2, 2, 's16', True, False),           # q15_t -> int16_t
    ('motor.mode', M, 4, 2, 'u16', True, False),            # enum through a typedef
    ('motor.phase[0].current', M, 6, 2, 's16', True, False),
    ('motor.phase[1].current', M, 10, 2, 's16', True, False),
    ('motor.phase[2].duty', M, 16, 2, 'u16', True, False),
    ('motor.inner.a', M, 18, 1, 'u8', True, False),         # anonymous struct type
    ('motor.inner.b', M, 19, 1, 's8', True, False),
    ('motor.word', M, 20, 2, 'u16', True, False),           # member of an anonymous union
    ('motor.sword', M, 20, 2, 's16', True, False),
    ('motor.lo', M, 22, 1, 'u8', True, False),              # member of an anonymous struct
    ('motor.hi', M, 23, 1, 'u8', True, False),
    ('motor.gain', M, 26, 4, 'f32', True, False),
    ('motor.pos', M, 30, 4, 's32', True, False),
    ('motor.ticks', M, 34, 4, 'u32', True, False),
    ('plain.run', P, 0, 2, 'u16', False, False),            # not volatile: the qualifier comes from the variable
    ('plain.phase[1].current', P, 10, 2, 's16', False, False),
    ('trim', 'trim', 0, 1, 's8', True, False),
    ('bytes[0]', 'bytes', 0, 1, 'u8', True, False),
    ('bytes[3]', 'bytes', 3, 1, 'u8', True, False),
    ('grid[0][0]', 'grid', 0, 2, 's16', True, False),
    ('grid[1][2]', 'grid', 10, 2, 's16', True, False),      # (1 * 3 + 2) * 2
    ('smode', 'smode', 0, 2, 's16', True, False),           # enum with a negative value: signed
    ('dmode', 'dmode', 0, 2, 'u16', True, False),
    ('uv.l', 'uv', 0, 4, 'u32', True, False),
    ('uv.w[1]', 'uv', 2, 2, 'u16', True, False),
    ('fl', 'fl', 0, 4, 'f32', True, False),
    ('dbl', 'dbl', 0, 4, 'f32', True, False),              # double is 32 bit with this compiler
    ('flag', 'flag', 0, 1, 'u8', True, False),
    ('ch', 'ch', 0, 1, 's8', True, False),
    ('uch', 'uch', 0, 1, 'u8', True, False),
    ('only_here', 'only_here', 0, 2, 'u16', True, False),   # unique static
    ('  motor . phase [ 1 ] . current ', M, 10, 2, 's16', True, False),   # blanks
    ('vars.c:only_here', 'only_here', 0, 2, 'u16', True, False),
]
for expr, sym, off, width, typ, vol, const in GOOD:
    try:
        v = T.lookup(expr)
        ok = (v.address == S[sym][0] + off and v.width == width and v.type == typ
              and v.is_volatile == vol and v.is_const == const and v.in_ram)
        check(ok, f'{expr.strip():24} 0x{v.address:04X} {v.width} {v.type} vol={v.is_volatile} const={v.is_const}'
                  + ('' if ok else f'   expected 0x{S[sym][0] + off:04X} {width} {typ} vol={vol} const={const}'))
    except VarError as e:
        check(False, f'{expr!r} raised {e.rule}: {e}')

# the two statics of the same name
a = err(T, 'counter')
check(a is not None and a.rule == 'ambiguous' and 'vars.c:counter' in a.message and 'vars2.c:counter' in a.message,
      f'counter exists twice, the message names both files: {a}')
c1, c2 = T.lookup('vars.c:counter').address, T.lookup('vars2.c:counter').address
check(c1 != c2 and sorted([c1, c2]) == sorted(S['counter']), f'vars.c:counter 0x{c1:04X}, vars2.c:counter 0x{c2:04X} are the two symbols')
rule(T, 'nofile.c:counter', 'not_found')

# const data in flash
for expr, sym, off in [('limit_c', 'limit_c', 0), ('table_c[2]', 'table_c', 4), ('cv_ram', 'cv_ram', 0), ('cv_q15', 'cv_q15', 0)]:
    rule(T, expr, 'not_ram', f'{expr} is in flash')
    v = T.lookup(expr, require_ram=False)
    check(v.address == S[sym][0] + off and not v.in_ram and v.is_const, f'{expr} with require_ram=False: 0x{v.address:04X}, const, not in RAM')
v = T.lookup('cv_ram', require_ram=False)
check(v.is_volatile and v.is_const and v.type == 'u16', 'const volatile chain: both qualifiers, u16')
v = T.lookup('cv_q15', require_ram=False)
check(v.is_volatile and v.is_const and v.type == 's16' and v.ctype == 'q15_t', 'volatile const typedef: both qualifiers, s16')

# errors
e = rule(T, 'moter.run', 'not_found')
check(e and 'did you mean motor.run?' in e.message, f'wording: {e}')
e = rule(T, 'motor.spead', 'not_found')
check(e and e.suggestions[:1] == ['motor.speed'] and 'did you mean motor.speed?' in e.message, f'member typo: {e}')
e = rule(T, 'motor.phase[1].curent', 'not_found')
check(e and e.suggestions == ['motor.phase[1].current'], f'typo inside an array element: {e}')
e = rule(T, 'MOTOR.run', 'not_found')
check(e and 'motor.run' in e.suggestions, 'upper case name: suggests motor.run')
rule(T, 'motor', 'needs_member')
rule(T, 'motor.inner', 'needs_member')
rule(T, 'motor.phase[1]', 'needs_member')
rule(T, 'uv', 'needs_member')
rule(T, 'motor.phase', 'needs_index')
rule(T, 'grid', 'needs_index')
rule(T, 'grid[1]', 'needs_index')
rule(T, 'bytes', 'needs_index')
e = rule(T, 'motor.phase[3].current', 'index_range')
check(e and '0 ... 2' in e.message, f'range is named: {e}')
rule(T, 'grid[2][0]', 'index_range')
rule(T, 'grid[0][3]', 'index_range')
rule(T, 'motor_p->run', 'pointer')
rule(T, 'motor_p.run', 'pointer')
rule(T, 'motor.ptr', 'pointer')
rule(T, 'bytes_p', 'pointer')
e = rule(T, 'motor.flags.fault', 'bitfield')
check(e and 'read the whole word' in e.message and 'mask' in e.message, f'bit field: {e}')
rule(T, 'motor.flags.level', 'bitfield')
rule(T, 'motor.flags', 'needs_member')
rule(T, 'motor.wide', 'width', 'long long member is refused')
rule(T, 'big', 'width', 'long long variable is refused')
rule(T, 'motor.run.x', 'not_struct')
rule(T, 'motor[1]', 'not_array')
rule(T, 'motor.run[0]', 'not_array')
rule(T, 'motor.run[', 'syntax')
rule(T, 'motor.run+1', 'syntax')
rule(T, '1abc', 'syntax')
rule(T, '', 'syntax')

# plain addresses
for text, addr in [('0x1100', 0x1100), ('4352', 0x1100), (' 0X10aa ', 0x10AA)]:
    v = T.lookup(text)
    check(v.address == addr and v.width is None and v.type is None and v.in_ram, f'address {text!r} -> 0x{v.address:04X}, no width, no type')
check(not T.lookup('0x8344').in_ram, 'address in flash: in_ram False')
rule(T, '0x12345', 'bad_address')

# names()
N = T.names()
check(N == sorted(set(N)), f'names() sorted, no duplicates ({len(N)})')
for n in ['motor.run', 'motor.phase[2].duty', 'motor.hi', 'motor.lo', 'motor.word', 'grid[1][2]', 'uv.w[1]', 'trim', 'only_here',
          'vars.c:counter', 'vars2.c:counter', 'plain.phase[0].current', 'fl']:
    check(n in N, f'names() has {n}')
for n in ['counter', 'motor.flags.fault', 'motor.wide', 'motor.ptr', 'motor', 'motor.phase', 'table_c[0]', 'limit_c', 'big', 'motor_p']:
    check(n not in N, f'names() has no {n}')
bad = [n for n in N if err(T, n) is not None]
check(not bad, f'every name of names() resolves ({len(bad)} do not) {bad[:3]}')

# no debug information
ND = VarTable.from_elf(os.path.join(FIX, 'vars_nodebug.elf'))
check(not ND.has_debug and ND.names() == [], 'ELF without debug information: has_debug False, no names')
e = rule(ND, 'motor.run', 'no_debug')
check(e and '-g' in e.message, f'the message names the fix: {e}')
check(ND.lookup('0x1100').address == 0x1100, 'a plain address works without debug information')

# name table (port SIM)
SS = VarTable.from_symbols(SYMBOLS, SYMBOL_TYPES)
for n, typ in [('motor.speed', 's16'), ('motor.current_ref', 's16'), ('motor.ref_offset', 's16'), ('motor.run', 'u16'),
               ('startup.ramp_rate', 'u16'), ('tune_alive', 'u16'), ('counter', 's32'), ('toggle', 'u8')]:
    v = SS.lookup(n)
    check(v.address == SYMBOLS[n][0] and v.width == SYMBOLS[n][1] and v.type == typ, f'SIM {n}: 0x{v.address:04X} {v.width} {v.type}')
check(set(SYMBOL_TYPES) == set(SYMBOLS), 'SYMBOL_TYPES covers SYMBOLS')
check(SS.names() == sorted(SYMBOLS), 'SIM names()')
e = rule(SS, 'motor.stat', 'not_found')
check(e and e.suggestions[:1] == ['motor.state'] and 'did you mean motor.state' in e.message, f'SIM suggestion: {e}')
check(VarTable.from_symbols({'a': (0x1000, 2), 'b': (0x1002, 1), 'c': (0x1004, 4)}).lookup('c').type == 's32', 'name table without types: signed by width')
rule(VarTable.from_symbols({'w': (0x1000, 8)}), 'w', 'width')
rule(VarTable.from_symbols({'f': (0x9000, 2)}), 'f', 'not_ram')
check(SS.lookup('0x1100').address == 0x1100, 'SIM plain address')

# the ELF of test.X, configuration sim
if os.path.exists(SIM_ELF):
    t0 = time.perf_counter()
    X = VarTable.from_elf(SIM_ELF)
    X.lookup('motor.run')
    X.names()
    dt = time.perf_counter() - t0
    check(dt < 1.0, f'test.X sim ELF: load, one lookup and names() in {dt:.3f} s ({len(X.names())} names)')
    SX = symtab(SIM_ELF)
    OFF = {'motor.run': 0, 'motor.state': 2, 'motor.speed': 4, 'motor.speed_ref': 6, 'motor.iq': 8, 'motor.fault': 10,
           'motor.kp': 12, 'motor.ki': 14, 'motor.ref_offset': 16, 'motor.current_ref': 18, 'motor.current': 20,
           'motor.current_kp': 22, 'motor.current_ki': 24, 'startup.ramp_rate': 0, 'startup.align_current': 2,
           'startup.handover_speed': 4, 'tune_alive': 0}
    for n, off in OFF.items():
        v = X.lookup(n)
        check(v.address == SX[n.split('.')[0]][0] + off and v.width == 2 and v.type == SYMBOL_TYPES[n] and v.is_volatile,
              f'test.X {n}: 0x{v.address:04X} {v.type} (symbol + {off})')
    e = rule(X, 'motor.stat', 'not_found')
    check(e and e.suggestions[:1] == ['motor.state'], f'test.X suggestion: {e}')
    rule(X, 'motor', 'needs_member')
else:
    print('SKIP test.X sim ELF not built')

sys.exit(1 if getattr(check, 'failed', False) else 0)
