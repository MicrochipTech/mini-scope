"""report.py - writes mini-scope_report.txt, the one file to send when you ask for help.

    .venv\\Scripts\\python tools\\report.py

Reads doctor_report.txt, hw_report.json (or hw_report.txt) and run_report.txt, if they exist. The
output is built from an ALLOW LIST: fixed check names, fixed field formats, and numbers and codes
that pass a strict pattern. Nothing is copied through as text, and nothing is removed from the
inputs, so new data cannot slip in: no paths, no names, no free text, no ELF, no script, no traces,
no tuning values. A line that does not match is dropped, and the file counts how many were dropped.

Read mini-scope_report.txt, then decide whether to send it. This script sends nothing.
"""
import argparse
import datetime
import json
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
STATUS = ('OK', 'WARN', 'FAIL')
NUM = r'-?\d{1,9}(?:\.\d{1,3})?'
VER = r'v\d{1,3}\.\d{1,3}(?:\.\d{1,3})?'
VERS = rf'{VER}(?:, {VER}){{0,9}}'
CODE = r'[EW]\d{3}'

# doctor: check name -> (pattern for the whole detail text, names of its groups). The detail is never copied.
DOCTOR = {
    'Python': (r'(\d{1,2}\.\d{1,2}\.\d{1,3})', ('python',)),
    'MPLAB X': (rf'({VERS})', ('versions',)),
    'XC-DSC': (rf'({VERS}), test\.X asks for ({VER})', ('versions', 'wanted')),
    'device': (r'([A-Za-z0-9]{4,24})', ('device',)),
    'baud rate': (r'SCOPE_FP_HZ \d{1,9}, SCOPE_BAUD \d{1,7}: BRG (\d{1,5}), ([+-]\d{1,3}\.\d) %', ('brg', 'baud_error_pct')),
    'clock': (rf'example_main\.c: FRC 8 MHz -> FOSC ({NUM}) MHz, FP {NUM} MHz, SCOPE_FP_HZ ({NUM}) MHz',
              ('fosc_mhz', 'scope_fp_mhz')),
    'packages': None, 'device pack': None, 'build': None, 'debug build': None, 'pins': None, 'doctor': None,
    'COM ports': None,
}
VERSION_KEYS = {'Python': 'python', 'MPLAB X': 'mplab_x', 'XC-DSC': 'xc_dsc', 'device': 'device'}
# hw_check: check name -> the numeric fields that may appear (from hw_report.json)
HW = {
    'ping': ('fp_factor',), 'port': (), 'adapter loopback': ('wrong',),
    'stream': ('samples', 'lost'), 'clock': ('clock_dev_pct',), 'consistency': ('outliers', 'steps'),
    'stream rate': ('period_us', 'samples', 'lost', 'resyncs'), 'capture': ('samples', 'depth'),
    'capture trigger': ('pretrigger',), 'write': ('slope_ratio',), 'reset': ('answer_s',),
    'reset while streaming': ('answer_s',),
}
RUN_CHECKS = (r'stages 1 to 3: \d{1,4} errors, \d{1,4} warnings',
              r'target probe: (?:passed|stopped before the trial stream)',
              r'trial stream: (?:passed|not completed)')


def num(v):
    """A plain finite number in a fixed format; None for anything else."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) > 1e9:
        return None
    return f'{v:.3f}'.rstrip('0').rstrip('.') if isinstance(v, float) else str(v)


def read_lines(path):
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8', errors='replace') as f:
        return f.read().splitlines()


class Part:
    def __init__(self, title):
        self.lines, self.dropped = [f'[{title}]'], 0

    def add(self, text):
        self.lines.append(text)


def os_line(ln, ver):
    m = re.fullmatch(r'(Windows|Linux|Darwin|macOS)-(?:\d{1,3}-)?(\d{1,3}\.\d{1,3}(?:\.\d{1,6})?)?[\w.-]{0,80}', ln)
    if m:
        ver.setdefault('os', m[1] + (f' {m[2]}' if m[2] else ''))
    return bool(m)


def do_doctor(lines, ver):
    p = Part('doctor')
    for ln in lines:
        m = re.fullmatch(r'(OK|WARN|FAIL) {1,4}([A-Za-z -]{1,20}): (.{0,400})', ln)
        if m and m[2] in DOCTOR:
            fields = []
            if DOCTOR[m[2]]:
                pat, keys = DOCTOR[m[2]]
                f = re.fullmatch(pat, m[3])
                if f:
                    fields = [f'{k}={v}' for k, v in zip(keys, f.groups())]
                    if m[2] in VERSION_KEYS:
                        ver.setdefault(VERSION_KEYS[m[2]], f[1])
            p.add(' '.join([m[2].replace(' ', '_'), m[1]] + fields))
        elif (re.fullmatch(r'mini-scope doctor  \d{4}-\d\d-\d\d \d\d:\d\d', ln) or os_line(ln, ver)
              or re.fullmatch(r'Result: \d+ OK, \d+ WARN, \d+ FAIL', ln)):
            pass
        elif re.fullmatch(r'      COM port: (?:free|BUSY)', ln):
            p.add('com_port ' + ln.split()[-1].lower())
        elif ln.strip():
            p.dropped += 1
    return p


def do_hw_json(data, ver):
    p = Part('hw_check')
    if not isinstance(data, dict):
        p.dropped += 1
        return p
    if data.get('port') in ('SIM', 'COM'):
        p.add(f'port_kind {data["port"]}')
    py = data.get('python')
    if isinstance(py, str) and re.fullmatch(r'\d{1,2}\.\d{1,2}\.\d{1,3}', py):
        ver.setdefault('python', py)
    b = num(data.get('baud'))
    if b and re.fullmatch(r'\d{3,7}', b):
        p.add(f'baud {b}')
    checks = data.get('checks')
    for c in checks if isinstance(checks, list) else []:
        name = c.get('name') if isinstance(c, dict) else None
        if isinstance(name, str) and re.fullmatch(r'stream \d{1,6} us', name):
            name = 'stream rate'
        if name not in HW or c.get('status') not in STATUS:
            p.dropped += 1
            continue
        fields = []
        for key in HW[name]:
            if key in c:
                v = num(c[key])
                if v is None:
                    p.dropped += 1
                else:
                    fields.append(f'{key}={v}')
        p.add(' '.join([name.replace(' ', '_'), c['status']] + fields))
        p.dropped += len([k for k in c if k not in HW[name] and k not in ('name', 'status')])
    return p


def do_hw_text(lines):
    p = Part('hw_check')
    for ln in lines:
        m = re.fullmatch(r'(OK|WARN|FAIL) {1,4}(stream \d{1,6} us|[a-z ]{1,30}): .{0,400}', ln)
        name = 'stream rate' if m and re.fullmatch(r'stream \d{1,6} us', m[2]) else m and m[2]
        if m and name in HW:
            p.add(f'{name.replace(" ", "_")} {m[1]}')
        elif re.fullmatch(r'Result: \d+ OK, \d+ WARN, \d+ FAIL', ln):
            pass
        elif ln.strip():
            p.dropped += 1
    return p


def do_run(lines):
    p = Part('run_script')
    for ln in lines:
        m = (re.fullmatch(r'(port) (SIM|MDB|COM|none)', ln) or re.fullmatch(r'(mode) (check|probe|run)', ln)
             or re.fullmatch(r'(status) (ok|error|refused|aborted)', ln))
        a = re.fullmatch(rf'abort time ({NUM}) ms wall clock, ({NUM}) ms target time', ln)
        if m:
            p.add(f'{m[1]} {m[2]}')
        elif re.fullmatch(rf'rule codes (?:none|{CODE} x\d{{1,4}}(?:, {CODE} x\d{{1,4}})*)', ln):
            found = re.findall(rf'({CODE}) x(\d+)', ln)
            for code, n in found:
                p.add(f'rule {code} x{n}')
            if not found:
                p.add('rule none')
        elif any(re.fullmatch('check ' + c, ln) for c in RUN_CHECKS):
            p.add('check ' + re.sub(r'[^a-z0-9:,.]+', '_', ln[6:]))
        elif ln == 'abort time none':
            p.add('abort none')
        elif a:
            p.add(f'abort wall_ms={a[1]} target_ms={a[2]}')
        elif re.fullmatch(rf'wall clock {NUM} s', ln):
            p.add(f'wall_clock_s {ln.split()[2]}')
        elif re.fullmatch(r'run_script  \d{4}-\d\d-\d\d \d\d:\d\d', ln):
            pass
        elif ln.strip():
            p.dropped += 1
    return p


def build(doctor, hw_json, hw_txt, run, now=None):
    """The report text from the input lines (a list each, or None) and the parsed hw json (or None)."""
    ver, parts, got = {}, [], []
    if doctor is not None:
        parts.append(do_doctor(doctor, ver))
        got.append('doctor')
    if hw_json is not None:
        parts.append(do_hw_json(hw_json, ver))
        got.append('hw_check')
    elif hw_txt is not None:
        parts.append(do_hw_text(hw_txt))
        got.append('hw_check')
    if run is not None:
        parts.append(do_run(run))
        got.append('run_script')
    out = [f'mini-scope report  {now or datetime.datetime.now():%Y-%m-%d %H:%M}',
           'Made from an allow list: fixed names, numbers and codes only. No path, name, free text,',
           'ELF, script, trace or tuning value. Read it, then decide whether to send it.', '',
           '[versions]'] + [f'{k} {ver[k]}' for k in sorted(ver)]
    for part in parts:
        out += [''] + part.lines
    out += ['', f'[dropped] lines left out: {sum(x.dropped for x in parts)}',
            '[inputs] ' + (', '.join(got) or 'none found')]
    return '\n'.join(out) + '\n'


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--doctor', default=os.path.join(ROOT, 'doctor_report.txt'))
    p.add_argument('--hw', default=os.path.join(ROOT, 'hw_report.txt'), help='hw_report.json next to it is used if present')
    p.add_argument('--run', default=next((x for x in ('run_report.txt', os.path.join(ROOT, 'run_report.txt'))
                                          if os.path.isfile(x)), None))
    p.add_argument('--out', default=os.path.join(ROOT, 'mini-scope_report.txt'))
    a = p.parse_args()
    hw_json = None
    jpath = os.path.splitext(a.hw)[0] + '.json'
    if os.path.isfile(jpath):
        try:
            with open(jpath, encoding='utf-8') as f:
                hw_json = json.load(f)
        except (OSError, ValueError):
            hw_json = {}
    text = build(read_lines(a.doctor), hw_json, read_lines(a.hw), read_lines(a.run))
    with open(a.out, 'w', encoding='utf-8') as f:
        f.write(text)
    print(text)
    print(f'Written to {os.path.basename(a.out)} in the folder you named (or the current folder)')
    print('Read this file. It was built from an allow list and holds no path, name or free text.')
    print('This script sends nothing: you decide whether to send the file.')


if __name__ == '__main__':
    main()
