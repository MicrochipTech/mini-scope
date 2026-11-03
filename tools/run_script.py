"""run_script.py - runs a tuning script without the GUI and writes a report.

    .venv\\Scripts\\python tools\\run_script.py --port SIM scripts\\tune_startup.py
    .venv\\Scripts\\python tools\\run_script.py --port MDB --elf test.X\\dist\\sim\\production\\test.X.production.elf scripts\\tune_startup.py
    .venv\\Scripts\\python tools\\run_script.py --port COM5 --elf x.elf script.py
    .venv\\Scripts\\python tools\\run_script.py --check [--elf x.elf] script.py       stages 1 to 3, no port
    .venv\\Scripts\\python tools\\run_script.py --probe --port COM5 --elf x.elf script.py    stage 4 alone

The exit code is 0 only when the run ended with status ok. Ctrl+C aborts the run; it still ends in the safe end.
SIM runs against the PC model (tests/sim.dll), MDB against the firmware in the MPLAB X simulator (build the
configuration "sim" first, as start_sim.bat does). A COM port needs --elf: the variables are resolved from
its debug information.

Stages 1 to 3 (compile, ELF check, dry run) are printed with their rule text; an error stops the run before the
port is opened. The report goes to run_report.txt in the working directory. It holds the script base name, the port
kind, the status, the reason, the rule codes with counts, the check results and the abort time: no paths, no
variable names, no values. The run folder (run.txt, traces, results.csv, tuning.h) stays with the user.
"""
import argparse
import datetime
import os
import re
import sys
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path[:0] = [os.path.join(ROOT, 'gui'), os.path.join(ROOT, 'tests'), os.path.join(ROOT, 'tools')]
import scope_gui as g      # noqa: E402
import tune_api            # noqa: E402
import tune_check          # noqa: E402
from elf_vars import VarTable, VarError    # noqa: E402


def out(text=''):
    print(text, flush=True)


def port_kind(port):
    return 'none' if not port else port if port in ('SIM', 'MDB') else 'COM'


def redact(text, table):
    """The reason for the report: names, quoted text and numbers removed."""
    names = sorted(getattr(table, 'names', lambda: [])(), key=len, reverse=True)
    for n in names:
        text = text.replace(n, '<name>')
    text = re.sub(r"'[^']*'|\"[^\"]*\"", '<text>', text)
    text = re.sub(r'[A-Za-z_]\w*(?:[.\[]\w*\]?)+|\b\w*_\w*\b', '<name>', text)
    text = re.sub(r'[A-Za-z]:[\\/][^\s]*|[\\/][\w.\\/-]+', '<path>', text)
    return re.sub(r'-?\d+(?:\.\d+)?', '#', text)


def make_table(a):
    if a.port == 'SIM' or (a.check and not a.elf):
        from sim_serial import SYMBOLS, SYMBOL_TYPES
        return VarTable.from_symbols(SYMBOLS, SYMBOL_TYPES)
    if not a.elf or not os.path.isfile(a.elf):
        sys.exit('--elf: the ELF file of the programmed firmware is needed for this port')
    try:
        return VarTable.from_elf(a.elf)
    except VarError as e:
        sys.exit(f'--elf: {e}')


def print_problems(problems):
    for p in problems:
        out(f'  {p} [{p.code}]')
        out(f'      rule {p.code}: {tune_check.RULES[p.code][2]}')


class NoPort(Exception):
    """The simulator start-up failed: main() reports it."""


def open_scope(a):
    """Scope on the port, or exit (NoPort for a dead simulator). MDB: the simulator is started here (40 to 60 s)."""
    if a.port == 'MDB':
        from mdb_serial import MdbSerial
        last = [-1]

        def progress(frac, text):
            if int(frac * 10) != last[0]:
                last[0] = int(frac * 10)
                out(f'  MDB start {frac * 100:.0f} %  {text}')
        try:
            ser = MdbSerial(a.elf, progress=progress)
        except TimeoutError as e:       # MdbNoAnswer
            raise NoPort(str(e)) from None
        g.OPEN.append(ser)
    else:
        try:
            ser = g.open_port(a.port, a.baud)
        except Exception as e:      # noqa: BLE001 - serial.SerialException, OSError
            sys.exit(f'port {a.port}: {e}')
    sc = g.Scope(ser)
    if not sc.ping():
        g.close_port(ser)
        sys.exit('no answer from the target (ping): run hw_check.py first, see TROUBLESHOOTING.md')
    return sc


class Progress:
    """on_event receiver: prints progress, keeps the table and what the report needs."""

    def __init__(self, t0):
        self.t0 = t0
        self.columns, self.rows, self.states, self.traces = [], [], [], 0

    def __call__(self, e):
        t = time.perf_counter() - self.t0
        kind = e['type']
        if kind == 'state':
            self.states.append(e['state'])
            out(f'[{t:7.1f} s] state {e["state"]}')
        elif kind == 'log':
            out(f'[{t:7.1f} s] {e["text"]}')
        elif kind == 'table':
            self.columns = e['columns']
        elif kind == 'row':
            self.rows.append((e['values'], e['invalid']))
        elif kind == 'trace':
            self.traces += 1
            out(f'[{t:7.1f} s] trace {e["index"]}: {e["title"]}{" (failed)" if e["failed"] else ""}')

    def print_table(self):
        if not self.columns:
            return
        out()
        out('  '.join(f'{c:>12}' for c in self.columns))
        for values, invalid in self.rows:
            out('  '.join(f'{v:>12}' for v in values) + ('   invalid' if invalid else ''))


def write_report(a, status, reason, rule_codes, checks, abort, table, wall):
    codes = ', '.join(f'{k} x{v}' for k, v in sorted(rule_codes.items())) or 'none'
    rep = [f'run_script  {datetime.datetime.now():%Y-%m-%d %H:%M}',
           f'script {os.path.basename(a.script)}',
           f'port {port_kind(a.port)}',
           f'mode {"check" if a.check else "probe" if a.probe else "run"}',
           f'status {status}',
           f'reason {redact(reason, table) if reason else "none"}',
           f'rule codes {codes}']
    rep += [f'check {c}' for c in checks]
    rep.append(f'abort time {abort}')
    rep.append(f'wall clock {wall:.1f} s')
    with open(a.report, 'w', encoding='utf-8') as f:
        f.write('\n'.join(rep) + '\n')
    out(f'Report written to {os.path.abspath(a.report)}')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('script', help='the tuning script (.py)')
    p.add_argument('--port', help='COM port of the USB-UART adapter, SIM or MDB')
    p.add_argument('--baud', type=int, default=115200)
    p.add_argument('--elf', help='ELF of the programmed firmware (needed for MDB and COM ports)')
    p.add_argument('--out', default='runs', help='folder for the run folders (default: runs)')
    p.add_argument('--report', default='run_report.txt')
    p.add_argument('--check', action='store_true', help='stages 1 to 3 only, no port needed')
    p.add_argument('--probe', action='store_true', help='stage 4 alone (the target probe), then the safe end')
    a = p.parse_args()
    if not a.check and not a.port:
        p.error('--port is needed (except with --check)')
    if a.check and a.probe:
        p.error('--check and --probe exclude each other')
    if not os.path.isfile(a.script):
        sys.exit(f'{os.path.basename(a.script)} does not exist')

    t0 = time.perf_counter()
    table = make_table(a)
    out(f'mini-scope run_script  {datetime.datetime.now():%Y-%m-%d %H:%M}  '
        f'{os.path.basename(a.script)}, port {a.port or "none"}')

    problems = tune_check.check_script(a.script, table, baud=a.baud)
    rule_codes = {}
    for q in problems:
        rule_codes[q.code] = rule_codes.get(q.code, 0) + 1
    errors = [q for q in problems if q.severity == 'error']
    warnings = [q for q in problems if q.severity == 'warning']
    checks = [f'stages 1 to 3: {len(errors)} errors, {len(warnings)} warnings']
    out(f'Stages 1 to 3 (compile, ELF check, dry run): {len(errors)} errors, {len(warnings)} warnings')
    print_problems(problems)
    if errors or a.check:
        write_report(a, 'error' if errors else 'ok', f'{len(errors)} errors in the script' if errors else '',
                     rule_codes, checks, 'none', table, time.perf_counter() - t0)
        sys.exit(1 if errors else 0)

    try:
        sc = open_scope(a)
    except NoPort as e:
        out(f'ERROR: {e}')
        write_report(a, 'error', 'simulator did not answer', rule_codes, checks, 'none', table,
                     time.perf_counter() - t0)
        sys.exit(1)
    out(f'Connected after {time.perf_counter() - t0:.1f} s')
    prog = Progress(time.perf_counter())
    elf_name = os.path.basename(a.elf) if a.elf else 'model'
    t_run = time.perf_counter()
    result = None
    try:
        if a.probe:
            result = tune_api.probe(a.script, sc, table, on_event=prog, elf_name=elf_name)
            for r in result.get('rows', []):
                out(f'  {r["role"]:>10}  {r["name"]:<28} {r["value"]}')
        else:
            s = tune_api.run(a.script, sc, table, on_event=prog, out_dir=a.out, elf_name=elf_name, render_png=g.render_png)
            aborted = False
            while True:
                try:
                    if s.done.wait(0.3):
                        break
                except KeyboardInterrupt:
                    if not aborted:
                        out('Ctrl+C: aborting, the run ends in the safe end')
                        tune_api.abort('Ctrl+C')
                        aborted = True
            result = s.result
    except tune_api.RunRefused as e:
        out(f'Run refused: {e}')
        write_report(a, 'refused', str(e), rule_codes, checks, 'none', table, time.perf_counter() - t0)
        sys.exit(1)
    finally:
        g.close_ports()
    wall = time.perf_counter() - t_run

    for k, v in result.get('rule_codes', {}).items():
        rule_codes[k] = max(rule_codes.get(k, 0), v)
    reached = [x for x in prog.states if x in ('trial', 'running')]
    checks.append(f'target probe: {"passed" if reached else "stopped before the trial stream"}')
    checks.append(f'trial stream: {"passed" if "running" in prog.states else "not completed"}')
    ms, target = result.get('abort_ms'), result.get('abort_ms_target')
    abort = 'none' if ms is None else f'{ms:.1f} ms wall clock, {target:.1f} ms target time'

    prog.print_table()
    out()
    out(f'Status {result["status"]}: {result["reason"]}')
    for e in result.get('errors', []):
        out(f'  error: {e}')
    if result.get('summary_png'):
        out(f'Summary picture: {result["summary_png"]}')
    if result.get('folder'):
        out(f'Run folder: {result["folder"]}')
    out(f'Wall clock of the run: {wall:.1f} s ({time.perf_counter() - t0:.1f} s with start-up)')
    write_report(a, result['status'], result['reason'], rule_codes, checks, abort, table, wall)
    sys.exit(0 if result['status'] == 'ok' else 1)


if __name__ == '__main__':
    main()
