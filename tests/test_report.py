"""test_report.py - customer data must not reach the report files.

    python tests/test_report.py

Marker strings sit in every field a customer's text could land in (ELF path, user name, variable
names, reason texts, error lines, comments). None of them may appear in the output of
tools/report.py, in hw_report.txt/.json or in doctor_report.txt. With MARKER_LEAK=1 report.py copies
one line through: the test must then fail (it shows that the test can fail).
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
sys.path[:0] = [HERE, os.path.join(ROOT, 'gui'), os.path.join(ROOT, 'tools')]
import report      # noqa: E402

M = 'ZQMARK'


def check(cond, text):
    print(('OK   ' if cond else 'FAIL ') + text)
    if not cond:
        check.failed = True


def clean(text):
    return M.lower() not in text.lower()


def rd(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


if os.environ.get('MARKER_LEAK'):           # the deliberate hole, to see the test fail
    _build = report.build

    def build(doctor, hw_json, hw_txt, run, now=None):
        return _build(doctor, hw_json, hw_txt, run, now) + (run or ['', ''])[1] + '\n'
    report.build = build

# 1. report.build on inputs with markers everywhere
doctor = [
    f'mini-scope doctor  2026-10-30 12:00  C:\\Users\\{M}_user\\{M}_project',
    f'Windows-11-10.0.26100-SP0-{M}',
    f'OK    Python: 3.12.1 (C:\\Users\\{M}_user\\python.exe)',
    'OK    Python: 3.12.1', 'OK    MPLAB X: v6.35', f'OK    MPLAB X: v6.35, {M}_v9.99',
    f'OK    XC-DSC: v3.30, test.X asks for {M}',
    f'OK    device pack: dsPIC33CK64MC103: {M}\\1.0',
    f'OK    device: {M}_device',
    f'WARN  baud rate: SCOPE_FP_HZ 32000000, SCOPE_BAUD 115200: BRG 68, +0.6 % {M}',
    f'OK    clock: example_main.c: FRC 8 MHz -> FOSC 64 MHz, FP 32 MHz, SCOPE_FP_HZ 32 MHz {M}',
    f'FAIL  {M}_check: detail {M}',
    f'FAIL  doctor: check failed: KeyError({M!r})',
    f'      -> hint {M}', f'      UART1, timer SCOPE_T1, project macros: {M}=1',
    f'      COM7: USB Serial [{M}] free', '      COM port: free', f'      COM port: {M}',
    f'FAIL  build: {M}',
]
hw_json = {'port': M, 'baud': 115200, 'python': f'3.12.1 {M}', 'secret': M, 'checks': [
    {'name': 'clock', 'status': 'OK', 'clock_dev_pct': 0.06, 'note': M},
    {'name': 'stream', 'status': 'FAIL', 'samples': M, 'lost': 3},
    {'name': f'{M}_var', 'status': 'OK', 'samples': 5},
    {'name': 'stream 772 us', 'status': 'OK', 'period_us': 772, 'lost': 0, 'resyncs': 0, 'samples': 6000},
    {'name': 'write', 'status': M, 'slope_ratio': 2.0},
    {'name': f'stream {M} us', 'status': 'OK'}, 'text ' + M,
    {'name': 'reset', 'status': 'OK', 'answer_s': float('nan')},
]}
hw_txt = [f'mini-scope hw_check  2026-10-30 12:00  port {M}, 115200 baud', f'ELF: C:\\{M}\\proj.elf, built x',
          f'OK    ping: SCP1 {M}', f'FAIL  {M}: x', f'FAIL  clock: {M}', f'FAIL  stream 772 us: {M}',
          f'      -> {M}', f'FAIL  port: could not open port {M}']
run = ['run_script  2026-10-30 12:00', f'script {M}.py', 'port COM', f'port {M}', 'mode run', 'status error',
       f'status {M}', f'reason undefined name {M}_gain at line 4', 'rule codes E102 x2, W201 x1',
       f'rule codes E102 x2, {M} x1', f'rule codes {M}', 'check stages 1 to 3: 2 errors, 1 warnings',
       f'check stages 1 to 3: 2 errors, {M} warnings', f'check target probe: {M}', 'check trial stream: not completed',
       'abort time 12.5 ms wall clock, 3.1 ms target time', f'abort time {M} ms wall clock, 3.1 ms target time',
       f'wall clock {M} s', 'wall clock 4.2 s', f'Traceback in {M}']

full = report.build(doctor, hw_json, None, run)
check(clean(full), 'markers in doctor, hw json and run inputs: none in the report')
for want in ('python=3.12.1', 'mplab_x v6.35', 'clock OK clock_dev_pct=0.06', 'stream FAIL lost=3',
             'stream_rate OK period_us=772 samples=6000 lost=0 resyncs=0', 'rule E102 x2', 'rule W201 x1',
             'status error', 'port COM', 'abort wall_ms=12.5 target_ms=3.1', 'wall_clock_s 4.2',
             'check stages_1_to_3:_2_errors,_1_warnings', 'check trial_stream:_not_completed', 'com_port free'):
    check(want in full, f'kept: {want}')
dropped = int(full.split('lines left out: ')[1].split()[0])
check(dropped >= 20, f'dropped lines are counted: {dropped}')
check('rule none' not in full, 'a bad rule code line is dropped as a whole')
txt = report.build(None, None, hw_txt, None)
check(clean(txt), 'hw_report.txt as the only source: no marker')
check('ping OK' in txt and 'stream_rate FAIL' in txt, 'hw_report.txt: names and status kept')
check(clean(report.build(doctor, {}, None, [])), 'empty json and empty files: no marker')
check(clean(report.build(None, ['x'], None, None)), 'json that is not an object: no marker')

# 2. hw_check and doctor write files without the markers; report.py reads them
tmp = tempfile.mkdtemp(prefix=M + '_')
try:
    proj = os.path.join(tmp, f'{M}_project')
    os.makedirs(proj)
    elf = os.path.join(proj, f'{M}_motor.elf')
    open(elf, 'wb').close()
    env = dict(os.environ, PATH=os.path.join(tmp, f'{M}_bin') + os.pathsep + os.environ.get('PATH', ''),
               USERPROFILE=proj, HOME=proj, MINISCOPE_VENV=proj, MARKER_VAR=M)

    # hw_check --port SIM with an --elf in a marker folder (the PC model takes no ELF)
    rep = os.path.join(proj, 'hw_report.txt')
    r = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'hw_check.py'), '--port', 'SIM', '--secs', '3',
                        '--elf', elf, '--report', rep], capture_output=True, text=True, env=env)
    check(r.returncode == 0, f'hw_check --port SIM exit code {r.returncode}')
    check(clean(rd(rep)) and clean(rd(rep[:-4] + '.json')),
          'hw_check --port SIM --elf <marker folder>: no marker in hw_report.txt/.json')

    # the ELF branch (real port path) in-process: the port is the PC model, the ELF has marker variable names
    import hw_check
    import scope_gui as g
    real_open = g.open_port
    g.open_port = lambda name, baud, progress=None: real_open('SIM', baud)
    g.load_symbols = lambda path: dict(g.sim_symbols(), **{f'{M}_gain': (0x1100, 2), f'{M}.phase[1].current': (0x1102, 2)})
    rep2 = os.path.join(proj, 'hw2_report.txt')
    old = sys.argv
    sys.argv = ['hw_check', '--port', f'{M}_COM', '--secs', '3', '--elf', elf, '--report', rep2]
    try:
        hw_check.main()
    except SystemExit:
        pass
    sys.argv = old
    t2, j2 = rd(rep2), rd(rep2[:-4] + '.json')
    check('custom ELF' in t2, 'hw_check with a customer ELF: the file says "custom ELF"')
    check(clean(t2) and clean(j2), 'hw_check with a customer ELF, marker in path, port and variable names: no marker')
    check(len(hw_check.checks) >= 8, f'{len(hw_check.checks)} check results in the json')

    # doctor in a copy of the tree under a marker folder, markers in PATH, HOME and an env var
    tree = os.path.join(proj, 'tree')
    for sub in ('tools', 'firmware', os.path.join('test.X', 'nbproject')):
        os.makedirs(os.path.join(tree, sub))
    shutil.copy(os.path.join(ROOT, 'tools', 'doctor.py'), os.path.join(tree, 'tools'))
    for f in ('scope.h', 'example_main.c'):
        shutil.copy(os.path.join(ROOT, 'firmware', f), os.path.join(tree, 'firmware'))
    shutil.copy(os.path.join(ROOT, 'test.X', 'nbproject', 'configurations.xml'), os.path.join(tree, 'test.X', 'nbproject'))
    r = subprocess.run([sys.executable, os.path.join(tree, 'tools', 'doctor.py')], capture_output=True, text=True, env=env)
    d = rd(os.path.join(tree, 'doctor_report.txt'))
    check(M in r.stdout, 'doctor console shows the marker path (the marker does reach the console)')
    check(clean(d), 'doctor_report.txt from a tree in a marker folder: no marker')
    check(sys.executable.lower() not in d.lower(), 'doctor_report.txt: no Python path')
    out = os.path.join(proj, 'mini-scope_report.txt')
    r = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'report.py'), '--doctor', os.path.join(tree, 'doctor_report.txt'),
                        '--hw', rep2, '--run', os.devnull, '--out', out], capture_output=True, text=True, env=env, cwd=proj)
    rt = rd(out)
    check(r.returncode == 0 and clean(rt), 'report.py on the real doctor and hw files: no marker')
    check('[doctor]' in rt and 'clock_dev_pct' in rt and 'python=' in rt, 'report.py on the real files: content is there')
    user = os.environ.get('USERNAME', '')
    for what in (ROOT, os.path.expanduser('~'), sys.executable) + ((user,) if len(user) > 3 else ()):
        for name, text in (('report', rt), ('doctor_report', d), ('hw_report', t2)):
            check(what.lower() not in text.lower(), f'{name}: no path or user name of this machine')
finally:
    shutil.rmtree(tmp, ignore_errors=True)

sys.exit(1 if getattr(check, 'failed', False) else 0)
