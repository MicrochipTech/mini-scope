"""doctor.py - checks the PC and the project settings, no board needed.

    .venv\\Scripts\\python tools\\doctor.py          (or any Python 3.9+)

Looks at: Python and packages, MPLAB X / XC-DSC / device pack, the test.X
build, the effective settings of scope.h (pins, clock, baud rate) and the COM
ports. Writes the report to the console and to doctor_report.txt.
"""
import datetime
import glob
import importlib
import os
import platform
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
MCHP = r'C:\Program Files\Microchip'
PGX = {1: (40, 41), 2: (35, 36), 3: (37, 38)}       # ICSP channel -> RP of PGDx/PGCx on the dsPIC33CK64MC10x
FAMILY = re.compile(r'DSPIC33CK(32|64)MC10[2-6]$', re.I)
lines, results = [], []


def out(text=''):
    print(text, flush=True)
    lines.append(text)


def result(status, name, detail, hint=''):
    results.append(status)
    out(f'{status:4}  {name}: {detail}')
    if hint and status != 'OK':
        out(f'      -> {hint}')


def read(path):
    with open(path, encoding='utf-8', errors='replace') as f:
        return f.read()


def check_python():
    v = sys.version_info
    result('OK' if v >= (3, 9) else 'FAIL', 'Python', f'{platform.python_version()} ({sys.executable})',
           'Python 3.9 or newer is needed')
    missing = []
    for mod in ('numpy', 'serial', 'nicegui', 'elftools'):
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    venv = os.path.exists(os.path.join(os.environ.get('MINISCOPE_VENV') or os.path.join(ROOT, '.venv'),
                                       'Scripts', 'python.exe'))
    result('OK' if not missing else 'WARN', 'packages', f'missing: {", ".join(missing)}' if missing else 'all there',
           'start_gui.bat installs them into .venv; run this script with .venv\\Scripts\\python')
    out(f'      .venv {"exists" if venv else "not created yet (start_gui.bat creates it)"}')


def conf_xml():
    path = os.path.join(ROOT, 'test.X', 'nbproject', 'configurations.xml')
    xml = read(path)
    confs = {}
    for m in re.finditer(r'<conf name="(\w+)".*?</conf>', xml, re.S):
        c = m.group(0)
        get = lambda pat: (re.search(pat, c) or [None, ''])[1]          # noqa: E731
        confs[m.group(1)] = {
            'device': get(r'<targetDevice>([^<]+)'), 'tool': get(r'<platformTool>([^<]+)'),
            'xc': get(r'<languageToolchainVersion>([^<]+)'),
            'macros': get(r'<C30>.*?key="preprocessor-macros" value="([^"]*)"'),
        }
    return confs


def check_tools(conf):
    mplab = sorted(glob.glob(os.path.join(MCHP, 'MPLABX', 'v*')))
    xc = sorted(glob.glob(os.path.join(MCHP, 'xc-dsc', 'v*')))
    result('OK' if mplab else 'FAIL', 'MPLAB X', ', '.join(os.path.basename(p) for p in mplab) or 'not found',
           f'install MPLAB X (looked in {MCHP}\\MPLABX)')
    want = 'v' + conf['xc']
    have = [os.path.basename(p) for p in xc]
    status = 'OK' if want in have else 'WARN' if have else 'FAIL'
    result(status, 'XC-DSC', f'{", ".join(have) or "not found"}, test.X asks for {want}',
           'MPLAB X uses another installed version (Project Properties > Compiler Toolchain); '
           'without any XC-DSC nothing builds')
    dev = conf['device']
    pics = (glob.glob(os.path.join(MCHP, 'MPLABX', 'v*', 'packs', 'Microchip', '*', '*', 'edc', dev + '.PIC'))
            + glob.glob(os.path.join(os.path.expanduser('~'), '.mchp_packs', 'Microchip', '*', '*', 'edc',
                                     dev + '.PIC')))
    packs = sorted({os.sep.join(p.split(os.sep)[-4:-2]) for p in pics})
    result('OK' if packs else 'FAIL', 'device pack', f'{dev}: {", ".join(packs) or "no pack found"}',
           'install the DFP in MPLAB X (Tools > Packs)')
    result('OK' if FAMILY.match(dev) else 'WARN', 'device', dev,
           'scope.c is written for the dsPIC33CK64MC10x family (RP32..53 on RB/RC, PPS codes); '
           'another device needs a port, see CLAUDE.md')
    out(f'      programmer of configuration "default": {conf["tool"]}')


def check_build():
    src = max(os.path.getmtime(p) for p in glob.glob(os.path.join(ROOT, 'firmware', '*.[ch]')))
    prod = os.path.join(ROOT, 'test.X', 'dist', 'default', 'production', 'test.X.production.elf')
    dbg = os.path.join(ROOT, 'test.X', 'dist', 'default', 'debug', 'test.X.debug.elf')
    when = lambda p: f'{datetime.datetime.fromtimestamp(os.path.getmtime(p)):%Y-%m-%d %H:%M}'    # noqa: E731
    if not os.path.exists(prod):
        result('WARN', 'build', 'no production build of test.X yet',
               'in MPLAB X: Make and Program Device (production), not Debug')
    else:
        result('OK' if os.path.getmtime(prod) >= src else 'WARN', 'build', f'production ELF from {when(prod)}',
               'firmware sources are newer than the build: build and program again')
    if os.path.exists(dbg) and (not os.path.exists(prod) or os.path.getmtime(dbg) > os.path.getmtime(prod)):
        result('WARN', 'debug build', f'newer than the production build ({when(dbg)})',
               'a debug build occupies the ICSP pins; program the production build: TROUBLESHOOTING.md 1')


def defines(text):
    d = {}
    for name, val in re.findall(r'^\s*#\s*define\s+(SCOPE_\w+)\s+([^\s/]+)', text, re.M):
        d.setdefault(name, val)                 # first definition = the #ifndef default
    return d


def num(v):
    return int(re.sub(r'[uUlL]+$', '', v), 0)


def rp_name(rp):
    return f'RB{rp - 32}' if rp < 48 else f'RC{rp - 48}'


def check_settings(conf):
    d = defines(read(os.path.join(ROOT, 'firmware', 'scope.h')))
    for m in re.findall(r'(SCOPE_\w+)=([^;\s]+)', conf['macros']):
        d[m[0]] = m[1]                          # -D in the project overrides scope.h
    fp, baud = num(d['SCOPE_FP_HZ']), num(d['SCOPE_BAUD'])
    brg = (fp + 2 * baud) // (4 * baud) - 1
    err = (fp / (4 * (brg + 1)) / baud - 1) * 100
    result('OK' if abs(err) < 2 else 'FAIL', 'baud rate', f'SCOPE_FP_HZ {fp}, SCOPE_BAUD {baud}: '
           f'BRG {brg}, {err:+.1f} %', 'pick a baud rate that divides FP/4 better')
    tx, rx = num(d['SCOPE_TX_RP']), num(d['SCOPE_RX_RP'])
    out(f'      UART{d["SCOPE_UART"]}, timer {d["SCOPE_TIMER"]}, TX RP{tx}/{rp_name(tx)}, RX RP{rx}/{rp_name(rx)}'
        + (f', project macros: {conf["macros"]}' if conf['macros'] else ''))

    main = read(os.path.join(ROOT, 'firmware', 'example_main.c'))
    ics = re.search(r'#pragma config ICS\s*=\s*PGD(\d)', main)
    if ics and {tx, rx} & set(PGX[int(ics[1])]):
        result('WARN', 'pins', f'UART pins on ICSP channel {ics[1]} (ICS = PGD{ics[1]})',
               'only with a production build and the PICkit disconnected: TROUBLESHOOTING.md 1')
    v = {k: int(x) for k, x in re.findall(r'_(PLLPRE|PLLFBDIV|POST1DIV|POST2DIV)\s*=\s*(\d+)', main)}
    if len(v) == 4:
        fosc = 8e6 / v['PLLPRE'] * v['PLLFBDIV'] / v['POST1DIV'] / v['POST2DIV']
        result('OK' if fosc / 2 == fp else 'FAIL', 'clock', f'example_main.c: FRC 8 MHz -> FOSC {fosc / 1e6:g} MHz, '
               f'FP {fosc / 2e6:g} MHz, SCOPE_FP_HZ {fp / 1e6:g} MHz', 'SCOPE_FP_HZ must be FOSC/2')


def check_ports():
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        result('WARN', 'COM ports', 'pyserial missing, cannot list them')
        return
    ports = list(list_ports.comports())
    if not ports:
        result('WARN', 'COM ports', 'none found', 'adapter plugged in? driver installed (Device Manager)?')
    for p in ports:
        try:
            serial.Serial(p.device).close()
            state = 'free'
        except serial.SerialException:
            state = 'BUSY (GUI, terminal or MPLAB Data Visualizer?)'
        out(f'      {p.device}: {p.description} [{p.hwid}] {state}')


def main():
    out(f'mini-scope doctor  {datetime.datetime.now():%Y-%m-%d %H:%M}  {ROOT}')
    out(f'{platform.platform()}')
    out()
    confs = conf_xml()
    for step in (check_python, lambda: check_tools(confs['default']), check_build,
                 lambda: check_settings(confs['default']), check_ports):
        try:
            step()
        except Exception as e:      # noqa: BLE001 - one broken check must not hide the others
            result('FAIL', 'doctor', f'check failed: {e!r}')
    out()
    out(f'Result: {results.count("OK")} OK, {results.count("WARN")} WARN, {results.count("FAIL")} FAIL')
    path = os.path.join(ROOT, 'doctor_report.txt')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print(f'Report written to {path}')


if __name__ == '__main__':
    main()
