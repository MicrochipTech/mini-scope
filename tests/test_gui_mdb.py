"""test_gui_mdb.py - the GUI in a real browser (Edge, headless) against MDB: the real firmware in the MPLAB X simulator.

    python tests/test_gui_mdb.py [main cli close lost ...]       (names as below, none: all)

Needs MPLAB X, the ELF of test.X, configuration "sim" (build it first, as start_sim.bat does), playwright (see
tests/requirements-dev.txt) and tests/sim.dll for the comparison run on SIM. It takes about 20 minutes: one MDB
start is 40 to 60 s and tune_startup.py needs 3.5 minutes on MDB. Only one MDB runs at a time, so do not run it
twice in parallel. The exit code is not 0 when a check failed.

  main   one GUI session: Scope tab write of motor.run, tune_startup.py to the end with the curves in the Scope
         tab, Abort in the middle of a step
  cli    tools/run_script.py on MDB and on SIM; the tables are compared with the one of the GUI run
  close  the browser is closed during a run (abort after 5 s)
  guiclose  the GUI process is closed during a run (Ctrl+Break)
  lost   the simulator process dies during a run

Every wait is an event (a state of the page, a line in run.txt, a process that is gone) with a timeout that is
derived from time_scale of the port. Every GUI, MDB and Java process this test started is looked up by PID and
checked at the end; what is left is ended by PID (never by image name) and counts as a failure.
"""
import csv
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_gui as tg                       # noqa: E402  (also puts gui/ and tools/ on the path)
from test_gui import (CHART_JS, FINAL, ROOT, SCRIPTS, SHOTS, Gui, check, click_run, log_text, newest_run,   # noqa: E402
                      pick, read_file, res_rows, states, to_script_tab, wait_final)
from playwright.sync_api import sync_playwright     # noqa: E402
import script_tab                           # noqa: E402
import tune_api                             # noqa: E402
from mdb_serial import MdbSerial            # noqa: E402

ELF = os.path.join(ROOT, 'test.X', 'dist', 'sim', 'production', 'test.X.production.elf')
SCALE = MdbSerial.time_scale                # wall clock seconds per second of target time
RUN_TARGET_S = 20                           # Run: the probe (about 4 s of target time) and tune_startup.py (about 9 s)
RUN_WAIT_S = 2 * RUN_TARGET_S * SCALE       # a whole run
PROBE_WAIT_S = 2 * 8 * SCALE                # from Run to the first start: stages 1 to 4, the probe reads and writes the target
STEP_WAIT_S = 2 * SCALE                     # one start of the motor (0.4 s of target time) and a safe end, with room
CONNECT_WAIT_S = 180                        # MDB start-up: 40 to 60 s
LOST_WAIT_S = 2 * tune_api.PORT_TIMEOUT_S + 2 * SCALE + 30
TIME_TOL_MS = 61                            # time_ms of one row: the model 21 ms + one chunk 30 ms + one poll step 10 ms (card 5.2)
START_RETRIES = []                          # runs that had to be started a second time (see start_run)
SHOT_RUN, SHOT_DONE, SHOT_WRITE = 'mdb_run.png', 'mdb_done.png', 'mdb_write.png'

# the last value / the maximum of series n of the chart, and the series (name, points, last, maximum)
LAST_GT = """([n, v]) => { const e = document.querySelector('.nicegui-echart'); if (!e) return false;
    const s = getElement(+e.id.slice(1)).chart.getOption().series[n]; return !!s && s.data.length > 0 && s.data[s.data.length - 1][1] > v; }"""
MAX_GT = """([n, v]) => { const e = document.querySelector('.nicegui-echart'); if (!e) return false;
    const s = getElement(+e.id.slice(1)).chart.getOption().series[n]; return !!s && s.data.some(p => p[1] > v); }"""
SERIES_JS = """() => { const e = document.querySelector('.nicegui-echart');
    return getElement(+e.id.slice(1)).chart.getOption().series.map(s => [s.name, s.data.length,
        s.data.length ? s.data[s.data.length - 1][1] : null, s.data.reduce((m, p) => Math.max(m, p[1]), -1e9)]); }"""
SCOPE_CHANNELS = [{'name': 'motor.speed', 'addr': '0x1000', 'type': 's16', 'factor': 1},
                  {'name': 'motor.state', 'addr': '0x1000', 'type': 'u16', 'factor': 1},
                  {'name': 'motor.run', 'addr': '0x1000', 'type': 'u16', 'factor': 1}]    # addresses follow the names (Load)


# ---- processes ----------------------------------------------------------------------------------------------------

def procs():
    """{pid: (parent pid, image name, command line)} of all processes."""
    out = subprocess.run(['powershell', '-NoProfile', '-Command',
                          '[Console]::OutputEncoding = [Text.Encoding]::UTF8; '
                          'Get-CimInstance Win32_Process | Select ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json'],
                         capture_output=True).stdout.decode('utf-8', errors='replace')     # a command line can hold any byte
    data = json.loads(out)
    data = data if isinstance(data, list) else [data]
    return {x['ProcessId']: (x['ParentProcessId'], x['Name'], x['CommandLine'] or '') for x in data}


def mdb_java(table=None):
    return sorted(p for p, (_, name, cmd) in (table or procs()).items() if name.lower() == 'java.exe' and 'mdb.jar' in cmd)


def tree(root, table=None):
    t = table or procs()
    found, todo = set(), [root]
    while todo:
        p = todo.pop()
        for pid, (parent, _, _) in t.items():
            if parent == p and pid not in found:
                found.add(pid)
                todo.append(pid)
    return found


def kill(pids):
    for p in pids:
        subprocess.run(['taskkill', '/F', '/PID', str(p)], capture_output=True)


class Watch:
    """The processes one test part started (GUI, cmd, Java), by PID. Nothing is ended by name."""

    def __init__(self, gui=None):
        self.gui, self.seen = gui, {}           # pid -> image name

    def sample(self):
        t = procs()
        if self.gui:
            for p in tree(self.gui.proc.pid, t) | {self.gui.proc.pid}:
                self.seen[p] = t[p][1].lower()
        return t

    def java(self):
        """The Java processes of MDB that belong to this GUI."""
        t = self.sample()
        return [p for p in mdb_java(t) if p in self.seen]

    def gone(self, label, wait_s=20, only=None):
        """Waits until none of the recorded processes exists and no Java of MDB is left, then checks it. only: image names
        to look at (the GUI itself may still be up)."""
        end = time.time() + wait_s
        while True:
            t = procs()
            left = sorted(p for p, name in self.seen.items() if p in t and (only is None or name in only))
            java = mdb_java(t)
            if (not left and not java) or time.time() > end:
                break
            time.sleep(0.5)
        kill(left + java)
        check(not left and not java, f'{label}: no GUI, cmd or Java process left (recorded {len(self.seen)}, left {left}, java {java})')


def foreign_mdb():
    """An MDB that was not started here must not be touched: stop the test."""
    j = mdb_java()
    if j:
        sys.exit(f'an MDB Java process (PID {j}) is running already; not started by this test, not touched. Only one MDB at a time.')


# ---- the GUI on MDB -------------------------------------------------------------------------------------------------

def write_cfg_mdb(path, extra=None):
    cfg = {'channels': SCOPE_CHANNELS, 'period': 1000, 'writes': [{'name': '', 'addr': '', 'type': 'u16', 'value': 0}]}
    cfg.update(extra or {})
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(cfg, f)


def start_gui(tmp, name, **kw):
    cfg = os.path.join(tmp, f'settings_{name}.json')
    write_cfg_mdb(cfg)
    runs = os.path.join(tmp, f'runs_{name}')
    gui = Gui(cfg, '--mdb', ELF, '--runs', runs, **kw)
    return gui, runs


def connect_mdb(page, watch):
    page.get_by_role('button', name='Connect').click()
    page.get_by_text('connected, target responds').wait_for(timeout=CONNECT_WAIT_S * 1000)
    watch.sample()
    check(len(watch.java()) == 1, f'connected: the simulator runs as one Java process of this GUI {watch.java()}')


def csv_rows(folder):
    with open(os.path.join(folder, 'results.csv'), newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def show(name, rows):
    return f'{name}: ' + ', '.join(f"{r['ramp_rate']}/{r['good_starts']}/{r['time_ms']}" for r in rows)


def compare(a_name, a, b_name, b):
    """good_starts and status exact, time_ms within one chunk (-1 means failed and must match)."""
    same = [(x['ramp_rate'], x['good_starts'], x['status']) for x in a] == [(x['ramp_rate'], x['good_starts'], x['status']) for x in b]
    check(same, f'{a_name} and {b_name}: the same ramp rates, good_starts and status')
    diffs = []
    for x, y in zip(a, b):
        tx, ty = int(x['time_ms']), int(y['time_ms'])
        diffs.append(0 if tx == ty == -1 else (abs(tx - ty) if tx >= 0 and ty >= 0 else 10 ** 6))
    check(len(a) == len(b) and max(diffs, default=0) <= TIME_TOL_MS, f'{a_name} and {b_name}: time_ms differs by {diffs} ms (limit {TIME_TOL_MS})')


def series_by_name(page):
    return {s[0].split('  [')[0]: s for s in page.evaluate(SERIES_JS)}


# ---- main -------------------------------------------------------------------------------------------------------------

def shot(page, name):
    """A screenshot from the tab row down: the rows above hold the ELF path of this machine."""
    os.makedirs(SHOTS, exist_ok=True)
    page.screenshot(path=os.path.join(SHOTS, name), clip={'x': 0, 'y': 205, 'width': 1500, 'height': 1395})


def start_run(page, runs):
    """Run, then waits until the stream of the script runs. A run that ends before that fails the test at once with its
    reason, not after the timeout. The only exceptions: the probe's ping unanswered (E401) and a stream stop in the probe,
    before main() starts. Run is pressed one more time and the repeat is counted in the output (`retries: n` at the end),
    because the simulator loses a mailbox write now and then (SIMULATOR.md) and 4.4b did not find the cause of the stop.
    Any other failure, and any difference in a result, is never repeated."""
    for attempt in (1, 2):
        before = set(os.listdir(runs)) if os.path.isdir(runs) else set()
        click_run(page)
        end, rt = time.time() + PROBE_WAIT_S, ''
        while time.time() < end:
            if page.evaluate("() => { const t = document.querySelector('[data-testid=run-log]')?.innerText ?? '';"
                             " return t.includes('-- running') && !t.includes('-- done'); }"):
                return
            new = sorted(set(os.listdir(runs)) - before) if os.path.isdir(runs) else []
            rt = read_file(os.path.join(runs, new[-1], 'run.txt')) if new else ''
            if 'result' in rt:
                break
            time.sleep(1)
        else:
            raise TimeoutError(f'the stream of the script did not start within {PROBE_WAIT_S} s')
        reason = [l for l in rt.splitlines() if l.startswith('reason')]
        if attempt == 1 and ('E401' in rt or 'during the probe' in rt):
            START_RETRIES.append(reason)
            print(f'NOTE the probe failed before main() ({"no ping answer" if "E401" in rt else "stream stop"}), Run pressed again: {reason}', flush=True)
            wait_final(page, 30000)
            continue
        check(False, f'Run ended before the stream started: {reason}')
        raise RuntimeError('run refused')


def new_page(browser, height=1100):
    """A page that reports what could take it away from the GUI: a crash, a navigation (a reload), a closed websocket."""
    page = browser.new_page(viewport={'width': 1500, 'height': height})
    stamp = lambda: time.strftime('%H:%M:%S')     # noqa: E731
    page.on('crash', lambda *_: print(f'PAGE {stamp()} the page crashed', flush=True))
    page.on('pageerror', lambda e: print(f'PAGE {stamp()} script error: {str(e)[:200]}', flush=True))
    page.on('framenavigated', lambda f: print(f'PAGE {stamp()} navigated: {f.url[-40:]}', flush=True))
    page.on('console', lambda m: m.type in ('error', 'warning') and print(f'PAGE {stamp()} console {m.type}: {m.text[:200]}', flush=True))
    page.on('websocket', lambda ws: (print(f'PAGE {stamp()} websocket opened', flush=True),
                                     ws.on('close', lambda *_: print(f'PAGE {stamp()} websocket closed', flush=True))))
    return page


def dump(page, folder):
    """What a failing wait leaves for the diagnosis: the status line, the end of the log and of run.txt."""
    print('status:', page.locator('[data-testid=script-status]').inner_text(), flush=True)
    print('log:', log_text(page).splitlines()[-6:], flush=True)
    print('run.txt:', read_file(os.path.join(folder, 'run.txt')).splitlines()[-8:], flush=True)


def wait_end(page, gui, folder, timeout_s, what):
    """wait_final with a second eye: every 2 s the socket of the page, the answer time of the GUI process and run.txt.
    A run that ends in run.txt while the page still shows nothing for 20 s is a failure at once (the page lost its
    connection, or the GUI did not tell it), not a wait for the timeout. Returns the status line."""
    end, seen_end, worst, down = time.time() + timeout_s, None, 0.0, None
    while time.time() < end:
        try:
            ok = page.evaluate("() => [window.socket ? window.socket.connected : null, document.visibilityState]")
            t = time.time()
            urllib.request.urlopen(gui.url, timeout=30).read()
            worst = max(worst, time.time() - t)
            text = page.locator('[data-testid=script-status]').inner_text(timeout=5000)
        except Exception as e:                  # noqa: BLE001
            print(f'{what}: the page cannot be read: {type(e).__name__}', flush=True)
            text, ok = '', [None, None]
        if ok[0] is False and down is None:
            down = time.time()
            print(f'{what}: the socket of the page is down at {time.strftime("%H:%M:%S")} (page {ok[1]})', flush=True)
        if re.search(FINAL, text):
            print(f'{what}: slowest answer of the GUI to a page request {worst:.2f} s', flush=True)
            return text
        if 'result' in read_file(os.path.join(folder, 'run.txt')):
            seen_end = seen_end or time.time()
            if time.time() - seen_end > 20:
                print(f'{what}: run.txt has a result, the page shows {text!r}; socket {ok}; slowest GUI answer {worst:.2f} s', flush=True)
                dump(page, folder)
                check(False, f'{what}: the run ended but the page was not told (socket {ok}, folder kept)')
                raise RuntimeError('page lost')
        time.sleep(2)
    dump(page, folder)
    raise TimeoutError(f'{what}: no end of the run within {timeout_s:.0f} s')


def test_main(tmp, parts=('write', 'full', 'abort')):
    """Items 4, 1, 5 and 2 in one session: the Write field, a whole run with its curves, an abort in the middle of a step."""
    gui, runs = start_gui(tmp, 'main')
    watch = Watch(gui)
    result = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            page = new_page(browser, 1600)
            page.goto(gui.url)
            page.get_by_role('tab', name='Scope').wait_for()
            connect_mdb(page, watch)

            if 'write' in parts:
                # ---- item 4: start by writing motor.run in the Write field, Scope tab ----
                page.get_by_role('button', name='Start').click()
                page.wait_for_function(LAST_GT, arg=[0, -1], timeout=STEP_WAIT_S * 1000)
                names = [s[0] for s in page.evaluate(SERIES_JS)]
                check(names == ['motor.speed', 'motor.state', 'motor.run'], f'Scope tab: the stream of the motor variables runs on MDB {names}')
                s0 = series_by_name(page)
                check(s0['motor.speed'][2] == 0 and s0['motor.run'][2] == 0, f"before the write: speed {s0['motor.speed'][2]}, run {s0['motor.run'][2]}")
                card = page.locator('.q-card', has_text='Write variable')
                card.locator('.q-select').first.click()
                page.keyboard.type('motor.run')
                page.locator('.q-menu .q-item', has_text='motor.run (').first.click()
                card.locator('input[type=number]').fill('1')
                card.locator('button', has_text='send').click()
                page.get_by_text('motor.run = 1').wait_for(timeout=10000)
                page.wait_for_function(LAST_GT, arg=[0, 1000], timeout=STEP_WAIT_S * 1000)       # closed loop reference is 1500 counts
                s1 = series_by_name(page)
                check(s1['motor.run'][2] == 1 and s1['motor.state'][2] == 3 and s1['motor.speed'][2] > 1000,
                      f"Write field motor.run = 1: run {s1['motor.run'][2]}, state {s1['motor.state'][2]} (3 = closed loop), "
                      f"speed {s1['motor.speed'][2]} counts (max {s1['motor.speed'][3]})")
                os.makedirs(SHOTS, exist_ok=True)
                shot(page, SHOT_WRITE)
                card.locator('input[type=number]').fill('0')
                card.locator('button', has_text='send').click()
                page.get_by_text('motor.run = 0').wait_for(timeout=10000)
                page.wait_for_function(LAST_GT, arg=[1, -1], timeout=STEP_WAIT_S * 1000)
                page.wait_for_function("""() => { const e = document.querySelector('.nicegui-echart');
                    const s = getElement(+e.id.slice(1)).chart.getOption().series[2]; return s.data[s.data.length - 1][1] === 0; }""",
                                       timeout=STEP_WAIT_S * 1000)
                check(True, 'Write field motor.run = 0: the stream shows run 0 again')
                page.get_by_role('button', name='Stop').click()
                page.get_by_role('button', name='Start').wait_for()

            if 'full' in parts:
                # ---- item 1 and 5: tune_startup.py from the GUI to the end, the curves in the Scope tab ----
                to_script_tab(page)
                pick(page, 'tune_startup.py')
                t0 = time.time()
                start_run(page, runs)
                watch.sample()
                page.get_by_role('tab', name='Scope').click()
                page.wait_for_function(MAX_GT, arg=[0, 0], timeout=STEP_WAIT_S * 1000)      # the speed curve of the first start
                page.wait_for_function(MAX_GT, arg=[0, 300], timeout=STEP_WAIT_S * 1000)    # a start that reached closed loop (750 rpm)
                pts = page.evaluate(CHART_JS)
                check(len(pts) == 4 and pts[0][0].startswith('motor.speed') and pts[0][1] > 100,
                      f'running on MDB: the Scope tab shows the 4 curves of the script {pts}')
                ser = page.evaluate(SERIES_JS)
                check(ser[0][3] > 0 and ser[1][3] > 0 and ser[2][3] > 0, f'running on MDB: speed, iq and state leave zero (maxima {[round(s[3], 1) for s in ser]})')
                shot(page, SHOT_RUN)
                to_script_tab(page)
                final = wait_end(page, gui, newest_run(runs, 'tune_startup_'), RUN_WAIT_S, 'whole run')
                wall = time.time() - t0
                check('ok, script ended' in final, f'tune_startup.py on MDB through the GUI: {final!r} after {wall:.0f} s')
                for _ in range(80):
                    if '-- done' in log_text(page):
                        break
                    page.wait_for_timeout(100)
                check(not tg.rows(page, 'error'), f'Problems holds no error after the run: {tg.rows(page)}')
                folder = newest_run(runs, 'tune_startup_')
                gui_rows = csv_rows(folder)
                cells = [r[3][1:4] for r in res_rows(page)]
                check(cells == [[x['ramp_rate'], x['good_starts'], x['time_ms']] for x in gui_rows],
                      f'the comparison table of the page equals results.csv {cells}')
                check(os.path.isfile(os.path.join(folder, 'tuning.h')) and os.path.isfile(os.path.join(folder, 'summary.png')),
                      'run folder holds tuning.h and summary.png')
                shot(page, SHOT_DONE)
                print(show('GUI on MDB', gui_rows), flush=True)
                result['gui'] = gui_rows
                result['gui_wall'] = wall
                check(not any(states(page)[k] for k in ('Disconnect', 'Start', 'Stop', 'Reset', 'Run', 'Probe')), 'after the run: the buttons are back')

            if 'abort' in parts:
                # ---- item 2: Abort in the middle of a step ----
                to_script_tab(page)
                pick(page, 'tune_startup.py')
                start_run(page, runs)
                page.get_by_role('tab', name='Scope').click()
                page.wait_for_function(LAST_GT, arg=[0, 0], timeout=STEP_WAIT_S * 1000)       # motor.speed is above 0: a step is under way
                ser = page.evaluate(SERIES_JS)
                to_script_tab(page)
                page.wait_for_function("() => !document.querySelector('[data-testid=abort-btn]').disabled", timeout=5000)
                t0 = time.time()
                page.get_by_role('button', name='Abort').click()
                final = wait_end(page, gui, newest_run(runs, 'tune_startup_'), STEP_WAIT_S + 60, 'abort')
                t_abort = time.time() - t0
                check('aborted' in final and 'Abort button' in final, f'abort: {final!r} after {t_abort:.1f} s wall clock '
                      f'(at the click: state {ser[2][2]}, speed {ser[0][2]})')
                folder = newest_run(runs, 'tune_startup_')
                rt = read_file(os.path.join(folder, 'run.txt'))
                for _ in range(80):                 # the log gets the last lines one event after the status
                    if '-- done' in log_text(page):
                        break
                    page.wait_for_timeout(100)
                lines = [l for l in rt.splitlines() if 'safe values' in l or l.startswith(('result', 'abort')) or 'aborted' in l]
                check('result aborted' in rt and 'safe values read back: 1 of 1 correct' in rt, f'abort: run.txt holds {lines}')
                check('safe values read back: 1 of 1 correct' in log_text(page), 'abort: the log in the page shows the read-back of the safe value')
                s = states(page)
                check(not any(s[k] for k in ('Disconnect', 'Start', 'Stop', 'Reset', 'Run', 'Probe')), f'abort: the buttons are back {s}')
                # what the target holds, read through the GUI: the Scope tab streams the motor variables again
                page.get_by_role('tab', name='Scope').click()
                page.get_by_role('button', name='Start').click()
                page.wait_for_function(LAST_GT, arg=[2, -1], timeout=STEP_WAIT_S * 1000)
                page.wait_for_function("""() => { const e = document.querySelector('.nicegui-echart');
                    const s = getElement(+e.id.slice(1)).chart.getOption().series[2]; return s.data.length > 20; }""", timeout=STEP_WAIT_S * 1000)
                ser = series_by_name(page)
                check(ser['motor.run'][2] == 0 and ser['motor.run'][3] == 0 and ser['motor.state'][2] == 0,
                      f"abort: the target reports motor.run {ser['motor.run'][2]} (max {ser['motor.run'][3]}), motor.state {ser['motor.state'][2]}, "
                      f"motor.speed {ser['motor.speed'][2]}, {ser['motor.run'][1]} samples")
                page.get_by_role('button', name='Stop').click()
                page.get_by_role('button', name='Start').wait_for()
                page.get_by_role('button', name='Disconnect').click()
                page.get_by_text('not connected').first.wait_for(timeout=60000)
            browser.close()
    finally:
        gui.stop()
        watch.gone('main')
    return result


def run_cli(port, out, extra):
    cmd = [sys.executable, os.path.join(ROOT, 'tools', 'run_script.py'), '--port', port, '--out', out,
           '--report', os.path.join(out, 'report.txt'), *extra, os.path.join(SCRIPTS, 'tune_startup.py')]
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=RUN_WAIT_S + CONNECT_WAIT_S, cwd=ROOT)
    return p.returncode, time.time() - t0, p.stdout[-300:]


def test_cli(tmp, gui_rows):
    """tools/run_script.py on MDB and on SIM; compared with the table of the GUI run."""
    foreign_mdb()
    rc_sim, t_sim, _ = run_cli('SIM', os.path.join(tmp, 'runs_cli_sim'), [])
    sim = csv_rows(newest_run(os.path.join(tmp, 'runs_cli_sim'), 'tune_startup_'))
    check(rc_sim == 0, f'run_script on SIM: exit {rc_sim} after {t_sim:.0f} s')
    rc, t_mdb, tail = run_cli('MDB', os.path.join(tmp, 'runs_cli_mdb'), ['--elf', ELF])
    check(rc == 0, f'run_script on MDB: exit {rc} after {t_mdb:.0f} s' + ('' if rc == 0 else ' ' + tail))
    mdb = csv_rows(newest_run(os.path.join(tmp, 'runs_cli_mdb'), 'tune_startup_'))
    foreign_mdb()           # run_script closed its simulator: none may be left
    check(True, 'run_script on MDB: no MDB Java process left')
    print(show('run_script on SIM', sim), flush=True)
    print(show('run_script on MDB', mdb), flush=True)
    if gui_rows:
        compare('GUI on MDB', gui_rows, 'run_script on MDB', mdb)
        compare('GUI on MDB', gui_rows, 'run_script on SIM', sim)
    compare('run_script on MDB', mdb, 'run_script on SIM', sim)


# ---- closing ----------------------------------------------------------------------------------------------------------

def test_close(tmp):
    """The browser is closed during a run, then the GUI process: no hang, the safe end, no Java left."""
    foreign_mdb()
    gui, runs = start_gui(tmp, 'close', flags=subprocess.CREATE_NEW_PROCESS_GROUP)
    watch = Watch(gui)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            page = new_page(browser, 1100)
            page.goto(gui.url)
            connect_mdb(page, watch)
            to_script_tab(page)
            start_run(page, runs)
            folder = newest_run(runs, 'tune_startup_')
            t0 = time.time()
            browser.close()
            end = time.time() + script_tab.GRACE_S + STEP_WAIT_S + 60
            while time.time() < end and 'result' not in read_file(os.path.join(folder, 'run.txt')):
                time.sleep(0.2)
            t_end = time.time() - t0
            rt = read_file(os.path.join(folder, 'run.txt'))
            check('result aborted' in rt and 'no browser page' in rt and t_end >= script_tab.GRACE_S - 1,
                  f'browser closed: run.txt holds "result aborted, no browser page" after {t_end:.1f} s (not before {script_tab.GRACE_S} s)')
            check('safe values read back: 1 of 1 correct' in rt, 'browser closed: the safe value was written and read back')
            check(gui.proc.poll() is None and len(watch.java()) == 1, 'browser closed: the GUI and its simulator are still there (a new page can come)')

            # a new page sees the end of the run and the buttons, then the next run is started from it
            browser = pw.chromium.launch(channel='msedge')
            page = new_page(browser, 1100)
            page.goto(gui.url)
            to_script_tab(page)
            tg.wait_log(page, '-- done', 15000)
            check(not tg.rows(page, 'error'), 'a new page shows the log of the aborted run')
            browser.close()
    finally:
        gui.stop()
        watch.gone('close')


def test_guiclose(tmp):
    """The GUI process is closed (Ctrl+Break, as a closed console window) during a run: safe end, no Java left."""
    foreign_mdb()
    gui, runs = start_gui(tmp, 'guiclose', flags=subprocess.CREATE_NEW_PROCESS_GROUP)
    watch = Watch(gui)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            page = new_page(browser, 1100)
            page.goto(gui.url)
            connect_mdb(page, watch)
            to_script_tab(page)
            start_run(page, runs)
            watch.sample()
            folder = newest_run(runs, 'tune_startup_')
            t0 = time.time()
            os.kill(gui.proc.pid, signal.CTRL_BREAK_EVENT)
            try:
                gui.proc.wait(STEP_WAIT_S + 60)
            except subprocess.TimeoutExpired:
                pass
            t_gui = time.time() - t0
            check(gui.proc.poll() is not None, f'GUI process closed during a run: it ended {t_gui:.1f} s after Ctrl+Break')
            rt = read_file(os.path.join(folder, 'run.txt'))
            lines = [l for l in rt.splitlines() if 'safe values' in l or 'GUI closed' in l or l.startswith('result')]
            print(f'GUI closed during a run, run.txt: {lines}', flush=True)
            check('aborted: GUI closed' in rt, 'GUI closed: run.txt holds "aborted: GUI closed"')
            # on MDB the read-back of the safe end needs about 30 s, the shutdown waits 4 s (the grace of a closed console
            # window): the read-back is not expected here. It is reported, not required.
            print('GUI closed: read-back of the safe value ' + ('done' if 'safe values read back: 1 of 1 correct' in rt else 'not reached before the process ended'), flush=True)
            browser.close()
    finally:
        gui.stop()
        watch.gone('guiclose')


def test_lost(tmp):
    """The simulator process dies in the middle of a run: the run ends with the reason, the GUI says not connected, nothing hangs."""
    foreign_mdb()
    gui, runs = start_gui(tmp, 'lost')
    watch = Watch(gui)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            page = new_page(browser, 1100)
            page.goto(gui.url)
            connect_mdb(page, watch)
            to_script_tab(page)
            start_run(page, runs)
            java = watch.java()
            kill(java)                                  # the simulator dies, the same as a USB adapter pulled out
            t0 = time.time()
            final = wait_end(page, gui, newest_run(runs, 'tune_startup_'), LOST_WAIT_S, 'port lost')
            check('port error' in final, f'simulator killed (PID {java}): {final!r} after {time.time() - t0:.1f} s')
            page.get_by_text('not connected').first.wait_for(timeout=60000)
            s = states(page)
            check(not s['Connect'] and not s['Start'] and s['Run'] and s['Probe'],
                  f'port lost: the page says not connected, Connect enabled, Run disabled {s}')
            rt = read_file(os.path.join(newest_run(runs, 'tune_startup_'), 'run.txt'))
            check('result error' in rt or 'result aborted' in rt, f"port lost: run.txt has a result {[l for l in rt.splitlines() if l.startswith('result')]}")
            watch.gone('port lost, GUI still running', 30, only=('java.exe', 'cmd.exe'))
            check(gui.proc.poll() is None, 'port lost: the GUI itself is still up')
            browser.close()
    finally:
        gui.stop()
        watch.gone('lost')


if __name__ == '__main__':
    tmp = tempfile.mkdtemp(prefix='ms_gui_mdb_')
    want = sys.argv[1:]
    check.failed = False
    try:
        if not os.path.isfile(ELF):
            sys.exit('the ELF of test.X, configuration "sim", is missing: build it as start_sim.bat does')
        foreign_mdb()
        result = {}
        if not want or 'main' in want:
            result = test_main(tmp)
        elif any(w.startswith('main=') for w in want):      # parts of main, for working on it: main=full,abort
            test_main(tmp, next(w for w in want if w.startswith('main=')).split('=')[1].split(','))
        if not want or 'cli' in want:
            test_cli(tmp, result.get('gui'))
        if not want or 'close' in want:
            test_close(tmp)
        if not want or 'guiclose' in want:
            test_guiclose(tmp)
        if not want or 'lost' in want:
            test_lost(tmp)
        print(f'retries: {len(START_RETRIES)}', flush=True)
        left = mdb_java()
        kill(left)
        check(not left, f'at the end: no MDB Java process left {left}')
    finally:
        import shutil
        if check.failed or sys.exc_info()[0]:
            print(f'kept for the diagnosis: {tmp}', flush=True)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    print('FAILED' if check.failed else 'all passed')
    sys.exit(1 if check.failed else 0)
