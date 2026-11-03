"""test_block.py - firmware/tune_block.h: the build id (CRC-16 of the text tune_build) and the heartbeat.

    python tests/test_block.py              all tests
    python tests/test_block.py crc elf       only the tests with these names (see TESTS at the end)

Needs tests/sim.dll (see test_protocol.py) and gcc for the heartbeat comparison. The test of the ELF of
test.X is skipped when that build output (configuration sim) does not exist.
A mutation test (mut_*) breaks the code under test on purpose and asserts that the same scenario fails.
"""
import ctypes
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
sys.path[:0] = [HERE, os.path.join(ROOT, 'gui'), os.path.join(ROOT, 'tools')]
import tune_api                                   # noqa: E402
from elf_vars import VarTable, VarError           # noqa: E402
from scope_gui import Scope                       # noqa: E402
from sim_serial import SimSerial, SYMBOLS, SYMBOL_TYPES, build_text   # noqa: E402

TMP = tempfile.mkdtemp(prefix='tune_block_')
STARTUP = os.path.join(ROOT, 'scripts', 'tune_startup.py')
ELF = os.path.join(ROOT, 'test.X', 'dist', 'sim', 'production', 'test.X.production.elf')
ID_ADDR = SYMBOLS['tune_build_id'][0]
ALIVE = SYMBOLS['tune_alive'][0]
tune_api.TRIAL_MS = 300


def check(cond, text):
    print(('OK   ' if cond else 'FAIL ') + text)
    if not cond:
        check.failed = True


lib = ctypes.CDLL(os.path.join(HERE, 'sim.dll'))
lib.sim_crc16.restype = ctypes.c_uint16
lib.sim_crc16.argtypes = [ctypes.c_char_p]


def c_crc(text):
    return lib.sim_crc16(text.encode('latin-1'))


# -- the checksum -------------------------------------------------------------------------------------------------

def test_crc():
    check(c_crc('123456789') == 0x29B1 and tune_api.build_id('123456789') == 0x29B1,
          f'CRC-16/CCITT-FALSE check value 0x29B1: C 0x{c_crc("123456789"):04X}, Python 0x{tune_api.build_id("123456789"):04X}')
    rnd = random.Random(1)
    alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz 0123456789:'
    texts = ['', 'a', 'Nov  1 2026 13:14:32', 'Jan  1 2000 00:00:00', 'Dec 31 2099 23:59:59', build_text()]
    texts += [''.join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 40))) for _ in range(300)]
    bad = [t for t in texts if c_crc(t) != tune_api.build_id(t)]
    check(not bad, f'C and Python agree on {len(texts)} texts (first difference: {bad[:1]})')
    ids = {c_crc(t) for t in texts}
    check(len(ids) >= len(set(texts)) - 1, f'{len(set(texts))} different texts give {len(ids)} different ids')


def test_transposition():
    text = 'Nov  1 2026 13:14:32'
    base = c_crc(text)
    swaps = [text[:i] + text[i + 1] + text[i] + text[i + 2:] for i in range(len(text) - 1) if text[i] != text[i + 1]]
    check(swaps and all(c_crc(t) != base for t in swaps), f'all {len(swaps)} swaps of two neighbours change the id')
    flips = [text[:i] + chr(ord(text[i]) ^ (1 << b)) + text[i + 1:] for i in range(len(text)) for b in range(8)]
    check(all(c_crc(t) != base for t in flips), f'all {len(flips)} single bit changes change the id')
    check({sum(t.encode()) for t in swaps} == {sum(text.encode())}, 'a plain sum of the bytes would not see any of these swaps')


def test_start():
    sc = Scope(SimSerial())
    sc.ping()
    err = sc.start([(ID_ADDR, 'u16')], 0, 1000)
    time.sleep(0.1)
    _, cols = sc.snapshot()
    sc.stop()
    got, want = int(cols[0][-1]), tune_api.build_id(build_text())
    check(not err and got == want, f'tune_build_id after start is 0x{got:04X}, CRC of "{build_text()}" is 0x{want:04X}')


# -- the heartbeat, host harness ----------------------------------------------------------------------------------

def test_heartbeat():
    exe = os.path.join(TMP, 'model_check.exe')
    m1 = os.path.join(ROOT, 'docs', 'evidence', 'm1')
    r = subprocess.run(['gcc', '-O2', '-Wall', '-I' + os.path.join(ROOT, 'firmware'), '-o', exe,
                        os.path.join(m1, 'model_check.c')], capture_output=True, text=True)
    check(r.returncode == 0 and not r.stderr.strip(), f'model_check.c builds without warnings: {r.stderr.strip()[:100]!r}')
    out = subprocess.run([exe], capture_output=True, text=True).stdout
    with open(os.path.join(m1, 'model_check.txt'), encoding='utf-8') as f:
        want = f.read()
    check(out.replace('\r\n', '\n') == want.replace('\r\n', '\n'), f'model_check prints the committed numbers ({len(out.splitlines())} lines)')


# -- reading the text from the ELF of test.X ---------------------------------------------------------------------

def test_elf():
    if not os.path.isfile(ELF):
        print('SKIP elf: build test.X, configuration sim, first')
        return
    t = VarTable.from_elf(ELF)
    text = t.read_string(tune_api.BUILD_TEXT)
    check(len(text) == 20 and text[3:5].strip() != '' or len(text) == 20, f'tune_build from the ELF: {text!r}')
    v = t.lookup(tune_api.BUILD_ID)
    check(v.width == 2 and v.in_ram, f'tune_build_id is a 16 bit variable in RAM at 0x{v.address:04X}')
    for name, rule in (('tune_alive', 'not_string'), ('table', 'not_string'), ('nothing_here', 'not_found')):
        try:
            t.read_string(name)
            check(False, f'read_string({name}) raises {rule}')
        except VarError as e:
            check(e.rule == rule, f'read_string({name}): {e.rule}')
    try:
        VarTable.from_elf(os.path.join(HERE, 'fixtures', 'vars_nodebug.elf')).read_string('x')
        check(False, 'ELF without debug information raises')
    except VarError as e:
        check(e.rule == 'no_debug', f'ELF without debug information: {e.rule}')


# -- the check in a run, SIM -------------------------------------------------------------------------------------

class LogSerial(SimSerial):
    """Logs what the target receives: the command bytes, and the writes as ('W', address, value)."""

    def __init__(self):
        super().__init__()
        self.log = []

    def write(self, data):
        b = bytes(data)
        if b[:1] == b'W':
            for i in range(0, len(b), 8):
                addr, width = struct.unpack('<HB', b[i + 1:i + 4])
                self.log.append(('W', addr, int.from_bytes(b[i + 4:i + 4 + width], 'little')))
        else:
            self.log.append((chr(b[0]),))
        return super().write(b)


def table(strings=None, drop=()):
    """The name table of sim.dll; strings: what read_string() gives (default: the text of the DLL)."""
    syms = {k: v for k, v in SYMBOLS.items() if k not in drop}
    return VarTable.from_symbols(syms, SYMBOL_TYPES, strings=SYMBOLS.strings if strings is None else strings)


def start(tab, target_id=None, probe_only=True):
    """tune_startup.py through the probe (probe_only) or through run() without the probe, on a fresh SIM.
    -> (result, serial log, run.txt)."""
    ser = LogSerial()
    sc = Scope(ser)
    sc.ping()
    if target_id is not None:               # the firmware reports another build
        sc.write(ID_ADDR, 'u16', target_id)
    ser.log.clear()
    out = tempfile.mkdtemp(dir=TMP)
    if probe_only:
        r = tune_api.probe(STARTUP, sc, tab, out_dir=out)
    else:
        r = tune_api.run(STARTUP, sc, tab, out_dir=out, hooks={'skip_probe': True}).wait(60)
    assert r is not None
    with open(os.path.join(r['folder'], 'run.txt'), encoding='utf-8') as f:
        return r, ser.log, f.read()


def writes(log):
    return [x for x in log if x[0] == 'W']


def refused(tab, target_id=None, probe_only=True):
    """True if the run ended with E402 and nothing was written: no heartbeat, no safe values."""
    r, log, txt = start(tab, target_id, probe_only)
    ok = r['status'] == 'error' and r['rule_codes'].get('E402') == 1 and not writes(log)
    return ok, f'{r["status"]} {r["rule_codes"]}, writes {writes(log)[:3]}, reason {r["reason"][:90]!r}'


WRONG = {'tune_build': build_text()[:-1] + ('0' if build_text()[-1] != '0' else '1')}      # one character off the text of the DLL


def test_pass():
    for probe_only in (True, False):
        r, log, _ = start(table(), probe_only=probe_only)
        first = next((i for i, x in enumerate(log) if x[0] == 'W'), None)
        check('E402' not in r['rule_codes'] and first is not None and log[first][1] == ALIVE and log[:first].count(('C',)) == (1 if probe_only else 2),
              f'the right ELF passes ({"probe" if probe_only else "stream_start, probe skipped"}): {r["status"]}, '
              f'before the first write {[x[0] for x in log[:first]]}: the build id stream{"" if probe_only else " and the trial stream"}, then the heartbeat')


def test_wrong_elf():
    for probe_only in (True, False):
        where = 'probe' if probe_only else 'stream_start, probe skipped'
        ok, txt = refused(table(WRONG), probe_only=probe_only)
        check(ok, f'string of the ELF is another build ({where}): E402, nothing written: {txt}')
    swapped = build_text()
    swapped = swapped[:-2] + swapped[-1] + swapped[-2] if swapped[-1] != swapped[-2] else swapped[:-3] + swapped[-2] + swapped[-3] + swapped[-1]
    ok, txt = refused(table({'tune_build': swapped}))
    check(ok, f'two neighbouring characters swapped: E402, nothing written: {txt}')
    ok, txt = refused(table(), target_id=tune_api.build_id(build_text()) ^ 1)
    check(ok, f'the target reports another id: E402, nothing written: {txt}')
    r, log, txt = start(table(WRONG))
    check('program the matching build' in r['reason'] and 'select the ELF' in r['reason'] and 'safe values not written' in txt,
          f'the text names both remedies; no safe values: {r["reason"][-150:]!r}')


def test_missing():
    for probe_only in (True, False):
        where = 'probe' if probe_only else 'stream_start'
        ok, txt = refused(table(strings={}), probe_only=probe_only)
        check(ok, f'no tune_build in the table (a plain address table, {where}): E402, nothing written: {txt}')
        ok, txt = refused(table(drop=('tune_build_id',)), probe_only=probe_only)
        check(ok, f'no tune_build_id in the table ({where}): E402, nothing written: {txt}')


def test_mut_disabled():
    """With the check disabled the wrong ELF gets through: the heartbeat and the writes of the probe reach the target."""
    with mock.patch.object(tune_api, 'build_id_check', lambda s: None):
        ok, txt = refused(table(WRONG))
        r, log, _ = start(table(WRONG))
    check(not ok, f'check disabled: the refusal scenario fails: {txt}')
    check(len(writes(log)) > 5, f'check disabled: {len(writes(log))} writes reached the target, the first to 0x{writes(log)[0][1]:04X}'
          if writes(log) else 'check disabled: nothing was written (unexpected)')


TESTS = {'crc': test_crc, 'transposition': test_transposition, 'start': test_start, 'heartbeat': test_heartbeat,
         'elf': test_elf, 'pass': test_pass, 'wrong': test_wrong_elf, 'missing': test_missing, 'mut_disabled': test_mut_disabled}

for name, fn in TESTS.items():
    if len(sys.argv) < 2 or name in sys.argv[1:]:
        print(f'--- {name}')
        fn()
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if getattr(check, 'failed', False) else 0)
