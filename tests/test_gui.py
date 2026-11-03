"""test_gui.py - the GUI in a real browser (Edge, headless) against SIM, without the user.

    pip install -r tests/requirements-dev.txt        (once; Edge is used, no browser download)
    python tests/test_gui.py

Starts gui/scope_gui.py --sim on a free port with a settings file in a temp folder (mini-scope.json is not
touched), drives it with playwright and stops the server it started, by its handle. Screenshots of the Script tab
go to docs/evidence/m4/. Needs tests/sim.dll (see the baseline commands in README.md).
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SCRIPTS = os.path.join(ROOT, 'scripts')
SHOTS = os.path.join(ROOT, 'docs', 'evidence', 'm4')
sys.path[:0] = [HERE, os.path.join(ROOT, 'gui'), os.path.join(ROOT, 'tools')]

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit('playwright is missing: pip install -r tests/requirements-dev.txt')

NEW_SCRIPT = os.path.join(SCRIPTS, 'zz_gui_test_new.py')       # removed again in the end
CHANNELS = [{'name': 'counter', 'addr': '0x1004', 'type': 's32', 'factor': 1}]


def check(cond, text):
    print(('OK   ' if cond else 'FAIL ') + text)
    if not cond:
        check.failed = True


class Gui:
    """The GUI as a subprocess. stop() ends exactly this process."""

    def __init__(self, cfg, *args, env=None, flags=0):
        self.log = tempfile.TemporaryFile()
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            self.port = s.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}/'
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, 'gui', 'scope_gui.py'), '--no-browser',
                                      '--port', str(self.port), '--config', cfg, *args],
                                     stdout=self.log, stderr=subprocess.STDOUT, cwd=ROOT,
                                     env={**os.environ, **(env or {})}, creationflags=flags)
        end = time.time() + 30
        while time.time() < end:
            try:
                urllib.request.urlopen(self.url, timeout=1).read()
                return
            except OSError:
                if self.proc.poll() is not None:
                    break
                time.sleep(0.2)
        self.stop()
        raise RuntimeError('GUI did not start')

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if os.environ.get('MS_TEST_GUILOG'):        # keep what the GUI process printed, for a failing run
            self.log.seek(0)
            with open(os.environ['MS_TEST_GUILOG'], 'ab') as f:
                f.write(self.log.read())
        self.log.close()


def write_cfg(path, script=None, channels=CHANNELS):
    cfg = {'channels': channels, 'period': 2000}
    if script is not None:
        cfg['script'] = script
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(cfg, f)


def chart_points(page):
    return page.evaluate("""() => { const e = document.querySelector('.nicegui-echart');
        return getElement(+e.id.slice(1)).chart.getOption().series.map(s => s.data.length); }""")


def open_list(page):
    page.locator('[data-testid=script-select]').click()
    page.wait_for_selector('.q-menu .q-item')


def list_items(page):
    return [t.strip() for t in page.locator('.q-menu .q-item').all_inner_texts()]


def pick(page, text):
    open_list(page)
    page.locator('.q-menu .q-item', has_text=text).first.click()


def selected_text(page):
    return page.locator('[data-testid=script-select]').inner_text().strip()


def rows(page, sev=None):
    sel = '.problem-row' + (f'[data-sev={sev}]' if sev else '')
    return page.eval_on_selector_all(sel, "els => els.map(e => e.querySelector('.grow').innerText + ' ' + e.dataset.code)")


def press_check(page, start_text):
    page.get_by_role('button', name='Check').click()
    page.locator('[data-testid=script-status]', has_text=start_text).wait_for(timeout=15000)


def test_editor_start():
    """Open never runs the script: verb 'edit' first, Notepad if .py has no edit verb."""
    import script_tab
    if sys.platform != 'win32':
        return
    calls = []
    orig = (os.startfile, subprocess.Popen)
    try:
        os.startfile = lambda path, verb='open': calls.append(('startfile', verb))
        subprocess.Popen = lambda cmd, *a, **k: calls.append(('popen', cmd[0]))
        check(script_tab.open_in_editor('x.py') == '' and calls == [('startfile', 'edit')],
              f'Open: os.startfile with the verb edit {calls}')

        def no_verb(path, verb='open'):
            calls.append(('startfile', verb))
            raise OSError('no edit verb for .py')
        calls.clear()
        os.startfile = no_verb
        check(script_tab.open_in_editor('x.py') == '' and calls == [('startfile', 'edit'), ('popen', 'notepad.exe')],
              f'Open: no edit verb, Notepad is the fallback {calls}')
    finally:
        os.startfile, subprocess.Popen = orig


def test_shutdown_hook():
    """The shutdown hook is called the way NiceGUI calls it (app.stop): a handler with one parameter gets the app."""
    import inspect
    import script_tab

    def call(handler, app_obj=object()):
        return handler(app_obj) if len(inspect.signature(handler).parameters) == 1 else handler()
    try:
        call(script_tab.shutdown)
        old_ok = True
    except TypeError:
        old_ok = False
    check(not old_ok, 'shutdown(wait_s) called with the app fails (why the hook is a separate function)')
    try:
        call(script_tab.on_app_shutdown)
        ok = True
    except TypeError:
        ok = False
    check(ok, 'the registered shutdown hook runs when NiceGUI calls it')
    src = open(os.path.join(ROOT, 'gui', 'scope_gui.py'), encoding='utf-8').read()
    check('on_shutdown(script_tab.on_app_shutdown)' in src, 'scope_gui registers on_app_shutdown')


def test_elf_list():
    """The ELF list of the Scope tab takes struct members from the DWARF data; no DWARF: symbol table."""
    import scope_gui
    syms, types, table = scope_gui.load_vars(os.path.join(HERE, 'fixtures', 'vars.elf'))
    check('motor.run' in syms and types.get('motor.run') == 'u16' and syms['motor.run'][1] == 2,
          f"ELF list has motor.run: {syms.get('motor.run')} {types.get('motor.run')}")
    check(any('[' in n for n in syms) and table.has_debug, 'ELF list has array elements and members')
    syms, types, table = scope_gui.load_vars(os.path.join(HERE, 'fixtures', 'vars_nodebug.elf'))
    check(len(syms) > 0 and not types and not table.has_debug, f'ELF without DWARF: symbol table ({len(syms)} names)')


def test_main(tmp):
    cfg = os.path.join(tmp, 'settings.json')
    copies = os.path.join(tmp, 'script_copies')
    os.makedirs(copies)
    typo = os.path.join(copies, 'tune_startup.py')
    with open(os.path.join(SCRIPTS, 'tune_startup.py'), encoding='utf-8') as f:
        text = f.read()
    typo_line = text[:text.index('wait_ms(')].count('\n') + 1
    with open(typo, 'w', encoding='utf-8', newline='') as f:
        f.write(text.replace('wait_ms(', 'wait_mss(', 1))
    write_cfg(cfg)
    gui = Gui(cfg, '--sim')
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            page = browser.new_page(viewport={'width': 1500, 'height': 1000})
            page.goto(gui.url)
            page.get_by_role('tab', name='Scope').wait_for()
            check(page.get_by_role('tab', name='Script').is_visible(), 'both tabs show: Scope and Script')

            # ---- Scope tab as before ----
            page.get_by_role('button', name='Connect').click()
            page.get_by_text('connected, target responds').wait_for(timeout=10000)
            page.get_by_role('button', name='Start').click()
            end = time.time() + 8
            while time.time() < end and not (chart_points(page) or [0])[0] > 100:
                time.sleep(0.3)
            pts = chart_points(page)
            check(pts and pts[0] > 100, f'Scope tab, SIM: the stream gives samples ({pts})')
            page.get_by_role('button', name='Stop').click()
            # a capture period below the floor is refused with a message, nothing is sent
            page.get_by_role('button', name='Capture', exact=True).click()
            page.get_by_label('Sample period µs').fill('5')
            page.get_by_role('button', name='Start').click()
            try:
                page.get_by_text('Capture period 5 µs is too short').wait_for(timeout=5000)
                refused = True
            except Exception:
                refused = False
            check(refused, 'Scope tab: a capture period of 5 us is refused with a message')
            page.get_by_role('button', name='Disconnect').click()
            page.get_by_text('not connected').first.wait_for(timeout=10000)

            # ---- Script tab ----
            page.get_by_role('tab', name='Script').click()
            page.get_by_text('Problems', exact=True).wait_for()
            check('tune_startup.py' in selected_text(page), f'tune_startup.py is selected: {selected_text(page)!r}')
            open_list(page)
            items = list_items(page)
            check('tune_startup.py' in items and 'zz_gui_test_new.py' not in items, f'the list holds scripts/: {items}')
            page.keyboard.press('Escape')
            page.wait_for_selector('.q-menu', state='detached')
            with open(NEW_SCRIPT, 'w', encoding='utf-8') as f:
                f.write('# a copy a customer put in scripts/\n')
            end = time.time() + 8           # the page looks at scripts/ on a timer: open the list until the new file is in it
            while True:
                open_list(page)
                if 'zz_gui_test_new.py' in list_items(page) or time.time() > end:
                    break
                page.keyboard.press('Escape')
                page.wait_for_selector('.q-menu', state='detached')
                time.sleep(0.5)
            check('zz_gui_test_new.py' in list_items(page), f'new file in scripts/ appears: {list_items(page)}')
            page.locator('.q-menu .q-item', has_text='zz_gui_test_new.py').click()
            page.locator('.italic', has_text='a copy a customer put in scripts/').wait_for(timeout=5000)
            check(True, 'first comment line of the script is shown next to the list')
            pick(page, 'tune_startup.py')
            page.locator('.italic', has_text='finds the startup parameters').wait_for(timeout=5000)

            # Check on the good script, no connection
            check(page.get_by_text('not connected').first.is_visible(), 'not connected')
            press_check(page, 'Stages 1 to 3: ')
            warn, err = rows(page, 'warning'), rows(page, 'error')
            codes = sorted(r.split()[-1] for r in warn)
            check(len(warn) == 5 and not err, f'Check on tune_startup.py: {len(warn)} warnings, {len(err)} errors')
            check(codes == ['W204'] * 3 + ['W205'] * 2, f'rule codes shown: {codes}')
            check(warn[0].startswith('tune_startup.py:') and ': warning: ' in warn[0], f'row form: {warn[0]!r}')
            page.locator('.problem-row').nth(2).click()
            line = int(page.locator('.problem-row').nth(2).get_attribute('data-line'))
            page.wait_for_selector('#ms-code .hl')
            check(page.locator('#ms-code .hl .ln').inner_text().strip() == str(line),
                  f'a click on a warning shows script line {line}')
            os.makedirs(SHOTS, exist_ok=True)
            page.screenshot(path=os.path.join(SHOTS, 'script_tab.png'))

            # a copy with a mistyped name, from another folder (added through the settings file)
            write_cfg(cfg, {'selected': '', 'extra': [typo]})
            page.reload()
            page.get_by_role('tab', name='Script').click()
            pick(page, 'tune_startup.py (other folder)')
            page.locator('[data-testid=script-status]', has_text='Stage 1 (compile): 1 error').wait_for(timeout=5000)
            check(True, 'stage 1 runs on selecting the file')
            press_check(page, 'Stages 1 to 3: ')
            err = rows(page, 'error')
            want = f'tune_startup.py:{typo_line}: error:'
            check(len(err) == 1 and err[0].startswith(want) and err[0].endswith('E102'),
                  f'mistyped name: {err}, expected line {typo_line}')
            page.locator('.problem-row[data-sev=error]').click()
            page.wait_for_selector('#ms-code .hl')
            hl = page.locator('#ms-code .hl')
            box, cbox = hl.bounding_box(), page.locator('#ms-code').bounding_box()
            check('wait_mss' in hl.inner_text() and hl.locator('.ln').inner_text().strip() == str(typo_line)
                  and cbox['y'] <= box['y'] and box['y'] + box['height'] <= cbox['y'] + cbox['height'],
                  'a click on the error shows that line, highlighted and scrolled into view')
            page.screenshot(path=os.path.join(SHOTS, 'script_tab_error.png'))

            # a broken save shows up on its own
            with open(typo, 'a', encoding='utf-8', newline='') as f:
                f.write('\nx = (\n')
            t0 = time.time()
            page.wait_for_selector('.problem-row[data-code=E101]', timeout=3000)
            check(True, f'a broken file saved: the syntax error appeared after {time.time() - t0:.1f} s (limit 3 s)')
            with open(typo, 'w', encoding='utf-8', newline='') as f:
                f.write(text)
            page.wait_for_function("() => !document.querySelector('.problem-row[data-sev=error]')", timeout=3000)
            check(True, 'the repaired file saved: the error is gone')

            # the selection is saved with the settings and is back after a reload
            page.get_by_role('tab', name='Scope').click()
            page.get_by_role('button', name='Save', exact=True).click()
            page.get_by_text('Settings saved to').wait_for()
            with open(cfg, encoding='utf-8') as f:
                saved = json.load(f).get('script', {})
            check(saved.get('selected') == typo and saved.get('extra') == [typo], 'mini-scope.json holds the selection and the added folder')
            page.reload()
            page.get_by_role('tab', name='Script').click()
            page.get_by_text('Problems', exact=True).wait_for()
            page.wait_for_timeout(500)
            check('tune_startup.py (other folder)' in selected_text(page),
                  f'after a reload the selection is back: {selected_text(page)!r}')
            browser.close()
    finally:
        gui.stop()
        check(gui.proc.poll() is not None, 'the GUI process started by the test is stopped')


def test_no_elf(tmp):
    """Without --sim there is no ELF: Check says so, and stage 1 still runs."""
    cfg = os.path.join(tmp, 'settings_noelf.json')
    with open(cfg, 'w', encoding='utf-8') as f:
        json.dump({'script': {'selected': 'scripts/tune_startup.py'}}, f)
    gui = Gui(cfg)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            page = browser.new_page(viewport={'width': 1500, 'height': 1000})
            page.goto(gui.url)
            page.get_by_role('tab', name='Script').click()
            page.locator('[data-testid=script-status]', has_text='Stage 1 (compile): no problems').wait_for(timeout=5000)
            page.get_by_role('button', name='Check').click()
            page.locator('[data-testid=script-status]', has_text='An ELF file is needed for stages 2 and 3').wait_for(timeout=5000)
            check(not rows(page), 'no ELF: Check says an ELF is needed, stage 1 is clean')
            browser.close()
    finally:
        gui.stop()

# ---- card 4.2: Run, Abort, Probe, Variables, Log ------------------------------------------------------------------

MOTOR_RUN = 0x1100
SINE_GAIN = 0x100A
FINAL = r'(: (ok|aborted|error), |refused)'      # the status line at the end of a run
STATE_JS = """() => { const out = {}, names = ['Connect', 'Disconnect', 'Start', 'Stop', 'Reset', 'Run', 'Abort', 'Probe'];
    for (const b of document.querySelectorAll('button')) { const t = names.find(n => n.toLowerCase() === b.innerText.trim().toLowerCase()); if (t) out[t] = b.disabled; }
    out.lists = {};
    for (const c of document.querySelectorAll('.q-card')) { const h = c.querySelector('.font-bold');
        if (h && ['Channels', 'Write variable'].includes(h.innerText.trim())) {
            const els = [...c.querySelectorAll('input, button')];
            out.lists[h.innerText.trim()] = [els.length, els.filter(e => !e.disabled).length]; } }
    return out; }"""
LOG_JS = "() => document.querySelector('[data-testid=run-log]')?.innerText ?? ''"
CHART_JS = """() => { const e = document.querySelector('.nicegui-echart');
    return getElement(+e.id.slice(1)).chart.getOption().series.map(s => [s.name, s.data.length]); }"""

# the copy with a runtime error: the state is not 0 only on the model, the dry run reads 0
BOOM = '''    write(VAR_RUN, 1)
    wait_ms(100)
    if read(VAR_STATE) != 0:
        raise ValueError("boom")
'''


def states(page, back='Script'):
    """Disabled state of the buttons of both tabs: a tab that is not shown is not in the DOM, so look at each in turn."""
    out = {}
    for tab in ('Scope', 'Script'):
        page.get_by_role('tab', name=tab).click()
        page.locator('.nicegui-echart >> nth=0' if tab == 'Scope' else '[data-testid=run-btn]').wait_for()
        if tab == 'Scope':          # the cards of the tab come after the chart: wait for the last one (a race seen under load)
            page.get_by_text('Write variable', exact=True).wait_for()
            page.get_by_role('button', name='Reset').wait_for()
        page.wait_for_timeout(150)
        got = page.evaluate(STATE_JS)
        out['lists'] = {**out.get('lists', {}), **got.pop('lists')}       # the lists are only in the Scope tab
        out.update(got)
    page.get_by_role('tab', name=back).click()
    return out


def log_text(page):
    page.locator('[data-testid=run-log]').wait_for()
    return page.evaluate(LOG_JS)


def wait_log(page, text, timeout=60000):
    page.wait_for_function("t => (document.querySelector('[data-testid=run-log]')?.innerText ?? '').includes(t)", arg=text, timeout=timeout)


def click_run(page, name='Run'):
    """Presses Run or Probe and waits until the page shows the new run (the status line changed)."""
    old = page.locator('[data-testid=script-status]').inner_text()
    page.get_by_role('button', name=name).click()
    page.wait_for_function("t => (document.querySelector('[data-testid=script-status]')?.innerText ?? '') !== t", arg=old, timeout=10000)


def wait_running(page, timeout=60000):
    """The stream of the script runs: '-- running' in the log of this run (the log of the last run ends with '-- done')."""
    page.wait_for_function("() => { const t = document.querySelector('[data-testid=run-log]')?.innerText ?? '';"
                           " return t.includes('-- running') && !t.includes('-- done'); }", timeout=timeout)


def wait_final(page, timeout=120000):
    page.locator('[data-testid=script-status]').wait_for()
    page.wait_for_function("re => new RegExp(re).test((document.querySelector('[data-testid=script-status]')?.innerText ?? ''))",
                           arg=FINAL, timeout=timeout)
    return page.locator('[data-testid=script-status]').inner_text()


def to_script_tab(page):
    page.get_by_role('tab', name='Script').click()
    page.get_by_text('Problems', exact=True).wait_for()


def connect(page):
    page.get_by_role('button', name='Connect').click()
    page.get_by_text('connected, target responds').wait_for(timeout=10000)


def sim_writes(path):
    """[(addr, size, value)] of all 'W' commands in the write log of SimSerial, in order, and the other commands."""
    w, other = [], []
    if not os.path.exists(path):
        return w, other
    with open(path) as f:
        for line in f:
            data = bytes.fromhex(line.split()[1])
            if data[:1] == b'W' and len(data) % 8 == 0:
                for i in range(0, len(data), 8):
                    size = data[i + 3]
                    w.append((int.from_bytes(data[i + 1:i + 3], 'little'), size, int.from_bytes(data[i + 4:i + 4 + size], 'little')))
            else:
                other.append(data[:1])
    return w, other


def newest_run(runs, stem='tune_'):
    found = sorted(d for d in os.listdir(runs) if d.startswith(stem)) if os.path.isdir(runs) else []
    return os.path.join(runs, found[-1]) if found else None


def read_file(path):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except OSError:
        return ''


def test_scripts(tmp):
    """Run, Abort, Probe, Variables, Log, the locks and the stream in the Scope tab, all on SIM."""
    copies = os.path.join(tmp, 'script_copies')
    os.makedirs(copies, exist_ok=True)
    with open(os.path.join(SCRIPTS, 'tune_startup.py'), encoding='utf-8') as f:
        text = f.read()
    typo, boom = os.path.join(copies, 'tune_typo.py'), os.path.join(copies, 'tune_boom.py')
    with open(typo, 'w', encoding='utf-8', newline='') as f:
        f.write(text.replace('wait_ms(', 'wait_mss(', 1))
    at = text.index('    stream_start()\n') + len('    stream_start()\n')
    with open(boom, 'w', encoding='utf-8', newline='') as f:
        f.write(text[:at] + BOOM + text[at:])
    boom_line = next(i for i, l in enumerate(open(boom, encoding='utf-8'), 1) if 'raise ValueError' in l)
    cfg = os.path.join(tmp, 'settings_run.json')
    write_cfg(cfg, {'selected': '', 'extra': [typo, boom]},
              channels=[{'name': 'motor.run', 'addr': '0x1100', 'type': 'u16', 'factor': 1}])
    with open(cfg, encoding='utf-8') as f:
        c = json.load(f)
    c['writes'] = [{'name': 'sine_gain', 'addr': '0x100A', 'type': 's16', 'value': 7}]
    with open(cfg, 'w', encoding='utf-8') as f:
        json.dump(c, f)
    runs, wlog = os.path.join(tmp, 'runs'), os.path.join(tmp, 'writes.log')
    gui = Gui(cfg, '--sim', '--runs', runs, env={'MS_SIM_WRITELOG': wlog})
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            page = browser.new_page(viewport={'width': 1500, 'height': 1100})
            page.goto(gui.url)
            to_script_tab(page)

            # ---- buttons follow the state: not connected ----
            s = states(page)
            check(s['Run'] and s['Probe'] and s['Abort'], f'not connected: Run, Probe and Abort are disabled {s}')
            connect(page)
            page.wait_for_timeout(600)
            s = states(page)
            check(not s['Run'] and not s['Probe'] and s['Abort'], f'connected: Run and Probe enabled, Abort disabled {s}')

            # ---- Probe: the Variables list ----
            click_run(page, 'Probe')
            page.locator('[data-testid=script-status]', has_text='Probe tune_startup.py: ok').wait_for(timeout=60000)
            rows_ = page.eval_on_selector_all('[data-testid=var-table] tbody tr', 'els => els.map(e => [...e.querySelectorAll("td")].map(t => t.innerText.trim()))')
            names = [r[0] for r in rows_]
            check(len(rows_) >= 8 and all(len(r) == 5 for r in rows_) and 'motor.speed' in names and 'motor.run' in names,
                  f'Probe: Variables shows {len(rows_)} rows of 5 cells: {rows_[:2]}')
            check(all(r[2].startswith('0x') and r[4].lstrip('-').isdigit() for r in rows_),
                  'Probe: address in hex and a raw value in every row')
            check('probe passed' in log_text(page), 'Probe: the log says "probe passed"')
            s = states(page)
            check(not s['Run'] and s['Abort'] and not s['Disconnect'] and not s['Start'], f'Probe over: buttons are back {s}')

            # ---- a wrong script: nothing is sent ----
            pick(page, 'tune_typo.py (other folder)')
            n_before = len(open(wlog).read().splitlines())
            click_run(page)
            page.locator('[data-testid=script-status]', has_text='refused').wait_for(timeout=30000)
            err = rows(page, 'error')
            check(len(err) == 1 and err[0].endswith('E102') and 'wait_mss' in err[0], f'wrong script: Problems shows {err}')
            n_after = len(open(wlog).read().splitlines())
            check(n_after == n_before, f'wrong script: nothing reached the port (write log {n_before} -> {n_after} entries)')
            check(not states(page)['Run'], 'wrong script: Run is enabled again')

            # ---- a runtime error of the script, with its line ----
            pick(page, 'tune_boom.py (other folder)')
            click_run(page)
            final = wait_final(page)
            err = rows(page, 'error')
            want = f'tune_boom.py:{boom_line}: error: ValueError: boom'
            check(any(e.startswith(want) for e in err), f'runtime error in Problems with its line {boom_line}: {err}')
            check('error' in final, f'status line: {final!r}')
            w, _ = sim_writes(wlog)
            check([x for x in w if x[0] == MOTOR_RUN][-1:] == [(MOTOR_RUN, 2, 0)], 'runtime error: the last write to motor.run is the safe value 0')
            check(not states(page)['Run'] and states(page)['Abort'], 'runtime error: buttons are back')

            # ---- a full run of tune_startup.py ----
            pick(page, 'tune_startup.py')
            click_run(page)
            wait_running(page)
            s = states(page)
            locked = [k for k in ('Disconnect', 'Start', 'Stop', 'Reset', 'Run', 'Probe') if s.get(k)]
            check(locked == ['Disconnect', 'Start', 'Stop', 'Reset', 'Run', 'Probe'] and not s['Abort'],
                  f'running: Connect, Start, Stop, Reset, Run, Probe disabled, Abort enabled {s}')
            check(s['lists']['Channels'][1] == 0 and s['lists']['Write variable'][1] == 0 and s['lists']['Channels'][0] > 2,
                  f'running: no input of the channel list and the write list is enabled {s["lists"]}')
            # the forced clicks: a disabled button cannot be clicked, so call the click handlers of the page directly
            # (NiceGUI drops the events of a disabled element on the server; the guards in the handlers are a second layer)
            wl0 = len(open(wlog).read().splitlines())
            page.get_by_role('tab', name='Scope').click()
            page.get_by_text('Write variable', exact=True).wait_for()
            page.get_by_role('button', name='Reset').wait_for()
            # all five, not one: the panel of the tab comes in over a few frames
            page.wait_for_function("""() => [...document.querySelectorAll('button')].filter(b =>
                ['disconnect', 'start', 'stop', 'reset'].includes(b.innerText.trim().toLowerCase())
                || b.innerHTML.includes('>send<')).length == 5""")
            n = page.evaluate("""() => { let n = 0; for (const b of document.querySelectorAll('button')) {
                const t = b.innerText.trim().toLowerCase();
                if (['disconnect', 'start', 'stop', 'reset'].includes(t) || b.innerHTML.includes('>send<')) {
                    const h = getElement(+b.id.slice(1)).$.vnode.props.onClick; (Array.isArray(h) ? h : [h]).forEach(f => f({})); n++; } }
                return n; }""")
            check(n == 5, f'running: the click handlers of Disconnect, Start, Stop, Reset and Write were called ({n})')
            page.wait_for_timeout(1500)
            w, other = sim_writes(wlog)
            new = [bytes.fromhex(l.split()[1])[:1] for l in open(wlog).read().splitlines()[wl0:]]
            check(all(c == b'W' for c in new) and not any(x[0] == SINE_GAIN for x in w),
                  f'running: forced clicks on Disconnect, Start, Stop, Reset, Write sent nothing new ({len(new)} heartbeat/script writes, '
                  f'commands {sorted(set(new))})')
            # the stream of the script in the Scope tab
            end = time.time() + 20
            pts = []
            while time.time() < end:
                pts = page.evaluate(CHART_JS)
                if len(pts) == 4 and pts[0][1] > 100:
                    break
                time.sleep(0.5)
            check(len(pts) == 4 and pts[0][0].startswith('motor.speed') and pts[0][1] > 100,
                  f'running: the Scope tab shows the 4 curves of the script {pts}')
            os.makedirs(SHOTS, exist_ok=True)
            page.screenshot(path=os.path.join(SHOTS, 'scope_during_run.png'))
            page.get_by_role('tab', name='Script').click()
            final = wait_final(page)
            check('ok, script ended' in final, f'status line at the end: {final!r}')
            lines = [l for l in log_text(page).splitlines() if re.match(r'ramp \d+: \d+ of \d+ starts good', l)]
            check(len(lines) == 4, f'the log holds the 4 result lines: {lines}')
            for _ in range(40):             # the status line comes with 'result', '-- done' one event later
                if '-- done' in log_text(page):
                    break
                page.wait_for_timeout(100)
            check('-- done' in log_text(page) and 'Suggested ramp rate' in log_text(page), 'the log holds the states and the printf text')
            folder = newest_run(runs, 'tune_startup_')
            check(folder and os.path.isfile(os.path.join(folder, 'tuning.h')) and os.path.isfile(os.path.join(folder, 'summary.png')),
                  f'run folder holds tuning.h and summary.png ({sorted(os.listdir(folder))[:3]} ...)')
            page.wait_for_function("() => { const i = document.querySelector('[data-testid=summary-png] img'); return i && i.naturalWidth > 0; }", timeout=10000)
            img = page.evaluate("() => { const i = document.querySelector('[data-testid=summary-png] img'); return i ? [i.naturalWidth, i.naturalHeight] : null; }")
            check(img and img[0] > 500, f'summary.png is shown in the Script tab {img}')
            page.screenshot(path=os.path.join(SHOTS, 'script_run_done.png'))
            check(not rows(page, 'error'), f'Problems holds no error after the run: {rows(page)}')
            s = states(page)
            check(not any(s[k] for k in ('Disconnect', 'Start', 'Stop', 'Reset', 'Run', 'Probe')) and s['Abort']
                  and s['lists']['Channels'][1] == s['lists']['Channels'][0], f'after the run: the buttons and lists are back {s}')
            page.get_by_role('tab', name='Scope').click()
            pts = page.evaluate(CHART_JS)
            check(pts == [], f'after the run: the Scope tab is as before, the chart is empty {pts}')

            # ---- Start in the Scope tab works as before, on motor.run ----
            page.get_by_role('button', name='Start').click()
            end = time.time() + 10
            while time.time() < end and not (page.evaluate(CHART_JS) or [[0, 0]])[0][1] > 50:
                time.sleep(0.3)
            pts = page.evaluate(CHART_JS)
            check(len(pts) == 1 and pts[0][0] == 'motor.run', f'after the run: Start in the Scope tab streams motor.run {pts}')
            page.get_by_role('button', name='Stop').click()
            page.get_by_role('tab', name='Script').click()

            # ---- Abort in the middle of a step ----
            pick(page, 'tune_startup.py')
            wl0 = len(sim_writes(wlog)[0])
            click_run(page)
            end = time.time() + 60
            while time.time() < end:
                w, _ = sim_writes(wlog)
                if (MOTOR_RUN, 2, 1) in w[wl0:]:
                    break
                time.sleep(0.1)
            check((MOTOR_RUN, 2, 1) in sim_writes(wlog)[0][wl0:], 'abort test: the script has started the motor')
            page.wait_for_function("() => !document.querySelector('[data-testid=abort-btn]').disabled", timeout=5000)
            t0 = time.time()
            wl1 = len(sim_writes(wlog)[0])
            page.get_by_role('button', name='Abort').click()
            final = wait_final(page, 30000)
            t_end = time.time() - t0
            check('aborted' in final and 'Abort button' in final, f'abort test: {final!r} after {t_end:.1f} s')
            w, _ = sim_writes(wlog)
            last_run = [x for x in w[wl1:] if x[0] == MOTOR_RUN]
            check(last_run and last_run[-1] == (MOTOR_RUN, 2, 0), f'abort test: after the click the last write to motor.run is 0 {last_run[-3:]}')
            folder = newest_run(runs, 'tune_startup_')
            rt = read_file(os.path.join(folder, 'run.txt'))
            check('result aborted' in rt and 'safe values read back: 1 of 1 correct' in rt, 'abort test: run.txt holds "result aborted" and the read back of the safe value')
            s = states(page)
            check(not any(s[k] for k in ('Disconnect', 'Start', 'Stop', 'Reset', 'Run', 'Probe')), f'abort test: the buttons are back {s}')
            # the model through the GUI's own means: the Scope tab streams motor.run
            page.get_by_role('tab', name='Scope').click()
            page.get_by_role('button', name='Start').click()
            end = time.time() + 10
            while time.time() < end and not (page.evaluate(CHART_JS) or [[0, 0]])[0][1] > 50:
                time.sleep(0.3)
            last = page.evaluate("""() => { const e = document.querySelector('.nicegui-echart');
                const d = getElement(+e.id.slice(1)).chart.getOption().series[0].data; return [d.length, d[d.length - 1][1], Math.max(...d.map(p => p[1]))]; }""")
            check(last[0] > 50 and last[1] == 0 and last[2] == 0, f'abort test: the model reports motor.run = 0 [samples, last, max] = {last}')
            page.get_by_role('button', name='Stop').click()
            page.get_by_role('tab', name='Script').click()
            browser.close()
    finally:
        gui.stop()


def test_page_lifetime(tmp):
    """Closing the browser aborts the run after 5 s; a reload within that time keeps it; a second window sees the same run."""
    copies = os.path.join(tmp, 'script_copies')
    os.makedirs(copies, exist_ok=True)
    long_script = os.path.join(copies, 'tune_long.py')       # the same script, 12 s of waiting after the stream starts
    with open(os.path.join(SCRIPTS, 'tune_startup.py'), encoding='utf-8') as f:
        text = f.read()
    at = text.index('    stream_start()\n') + len('    stream_start()\n')
    with open(long_script, 'w', encoding='utf-8', newline='') as f:
        f.write(text[:at] + '    wait_ms(12000)\n' + text[at:])
    cfg = os.path.join(tmp, 'settings_life.json')
    write_cfg(cfg, {'selected': long_script, 'extra': [long_script]})
    runs = os.path.join(tmp, 'runs_life')
    gui = Gui(cfg, '--sim', '--runs', runs)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            page = browser.new_page(viewport={'width': 1500, 'height': 1100})
            page.goto(gui.url)
            connect(page)
            to_script_tab(page)
            page.wait_for_timeout(600)
            click_run(page)
            wait_running(page)

            # a second window shows the same run
            page2 = browser.new_page(viewport={'width': 1500, 'height': 1100})
            page2.goto(gui.url)
            to_script_tab(page2)
            wait_running(page2)
            s2 = states(page2)
            check(s2['Run'] and s2['Probe'] and not s2['Abort'] and s2['Disconnect'],
                  f'second window: sees the run, Run and Probe disabled, Abort enabled {s2}')
            check('-- probing the target' in log_text(page2), 'second window: the log so far is there')
            page2.close()

            # reload within 5 s
            t0 = time.time()
            page.reload()
            to_script_tab(page)
            wait_running(page)
            t_re = time.time() - t0
            s = states(page)
            check(t_re < 5 and s['Run'] and not s['Abort'] and s['Disconnect'] and 'probe passed' in log_text(page),
                  f'reload after {t_re:.1f} s: the run goes on, its log so far is shown {s}')
            final = wait_final(page)
            check('ok, script ended' in final and 'tune_long.py' in final, f'reload: the run finished normally: {final!r}')
            s = states(page)
            check(not s['Run'] and not s['Start'] and not s['Disconnect'], f'reload: buttons are back after the run {s}')

            # the browser is closed in the middle of a run
            click_run(page)
            wait_running(page)
            folder = newest_run(runs, 'tune_long_')
            t0 = time.time()
            browser.close()
            end = time.time() + 30
            while time.time() < end and 'result' not in read_file(os.path.join(folder, 'run.txt')):
                time.sleep(0.2)
            t_end = time.time() - t0
            rt = read_file(os.path.join(folder, 'run.txt'))
            check('result aborted' in rt and 'no browser page' in rt and t_end < 10, f'browser closed: run.txt holds "result aborted" after {t_end:.1f} s (limit 10 s)')
            check('safe values read back: 1 of 1 correct' in rt, 'browser closed: the safe value was written and read back')
            browser = pw.chromium.launch(channel='msedge')
            page = browser.new_page(viewport={'width': 1500, 'height': 1100})
            page.goto(gui.url)
            to_script_tab(page)
            wait_log(page, '-- done', 5000)
            check(True, 'a new page shows the log of the aborted run')
            browser.close()
    finally:
        gui.stop()


def test_port_lost(tmp):
    """The port fails in the middle of the run: the run ends with the reason, the GUI is not connected, nothing hangs."""
    cfg = os.path.join(tmp, 'settings_lost.json')
    write_cfg(cfg)
    runs = os.path.join(tmp, 'runs_lost')
    gui = Gui(cfg, '--sim', '--runs', runs, env={'MS_SIM_LOST_S': '5'})
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            page = browser.new_page(viewport={'width': 1500, 'height': 1100})
            page.goto(gui.url)
            connect(page)
            to_script_tab(page)
            page.wait_for_timeout(600)
            t0 = time.time()
            click_run(page)
            final = wait_final(page, 60000)
            check('port error' in final and 'the port was lost' in final, f'port lost: {final!r} after {time.time() - t0:.1f} s')
            page.get_by_text('not connected').first.wait_for(timeout=10000)
            s = states(page)
            check(not s['Connect'] and not s['Start'] and s['Run'] and s['Probe'], f'port lost: not connected, Connect enabled, Run disabled {s}')
            rt = read_file(os.path.join(newest_run(runs, 'tune_startup_'), 'run.txt'))
            check('result error' in rt or 'result aborted' in rt, 'port lost: run.txt has a result')
            browser.close()
    finally:
        gui.stop()
        check(gui.proc.poll() is not None, 'port lost: the GUI process is stopped by the test')


def test_ctrl_break(tmp):
    """Windows only: a console close event (Ctrl+Break here) aborts the run in the GUI process before it ends."""
    if sys.platform != 'win32':
        return
    import signal
    cfg = os.path.join(tmp, 'settings_break.json')
    write_cfg(cfg)
    runs = os.path.join(tmp, 'runs_break')
    gui = Gui(cfg, '--sim', '--runs', runs, flags=subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            page = browser.new_page(viewport={'width': 1500, 'height': 1100})
            page.goto(gui.url)
            connect(page)
            to_script_tab(page)
            page.wait_for_timeout(600)
            click_run(page)
            wait_running(page)
            folder = newest_run(runs, 'tune_startup_')
            os.kill(gui.proc.pid, signal.CTRL_BREAK_EVENT)
            try:
                gui.proc.wait(15)
            except subprocess.TimeoutExpired:
                pass
            rt = read_file(os.path.join(folder, 'run.txt'))
            check('result aborted' in rt and 'GUI closed' in rt and 'safe values read back: 1 of 1 correct' in rt,
                  'Ctrl+Break: run.txt holds "aborted: GUI closed" and the read back of the safe value')
            browser.close()
    finally:
        gui.stop()



# ---- card 4.3: comparison table, overlay, export, report -------------------------------------------------------------

ROWS_JS = """() => [...document.querySelectorAll('[data-testid=res-table] tbody tr')].map(e =>
    [+e.dataset.id, e.dataset.status, e.className, [...e.querySelectorAll('td')].map(t => t.innerText.trim()),
     getComputedStyle(e.querySelector('td')).backgroundColor])"""
OVERLAY_JS = """() => { const e = document.querySelector('[data-testid=overlay]');
    const o = getElement(+e.id.slice(1)).chart.getOption();
    return {series: o.series.map(s => ({name: s.name, data: s.data, dash: s.lineStyle && s.lineStyle.type})),
            x: o.xAxis[0].name, y: o.yAxis[0].name}; }"""
MARKER = 'ZZMARKER-4711'


def res_rows(page):
    return page.evaluate(ROWS_JS)


def csv_cols(path):
    import csv
    with open(path, newline='', encoding='utf-8') as f:
        r = list(csv.reader(f))
    return r[0], [[float(x) for x in row] for row in r[1:]]


def test_results(tmp):
    """Table, sorting, overlay in physical units, marked row, Export and Report, on SIM."""
    cfg = os.path.join(tmp, 'settings_res.json')
    notable = os.path.join(tmp, 'script_copies', 'tune_notable.py')       # the same script without table() and row()
    os.makedirs(os.path.dirname(notable), exist_ok=True)
    with open(os.path.join(SCRIPTS, 'tune_startup.py'), encoding='utf-8') as f:
        lines = [l for l in f.read().splitlines(keepends=True) if not l.strip().startswith(('table(', 'row('))]
    with open(notable, 'w', encoding='utf-8', newline='') as f:
        f.writelines(lines)
    write_cfg(cfg, {'selected': '', 'extra': [notable]},
              channels=[{'name': 'motor.run', 'addr': '0x1100', 'type': 'u16', 'factor': 1}])
    runs, rep_dir, out_h = os.path.join(tmp, 'runs_res'), os.path.join(tmp, 'report_dir'), os.path.join(tmp, 'export', 'saved_tuning.h')
    os.makedirs(rep_dir)
    os.makedirs(os.path.dirname(out_h))
    with open(os.path.join(rep_dir, 'doctor_report.txt'), 'w', encoding='utf-8') as f:
        f.write(f'OK    Python: 3.12.1\nWARN  clock: {MARKER} C:\\Users\\somebody\\file\n{MARKER} free text line\n')
    gui = Gui(cfg, '--sim', '--runs', runs, env={'MS_EXPORT_PATH': out_h, 'MS_REPORT_DIR': rep_dir})
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            page = browser.new_page(viewport={'width': 1500, 'height': 1300})
            page.goto(gui.url)
            to_script_tab(page)
            connect(page)
            page.wait_for_timeout(600)
            b = lambda t: page.locator(f'[data-testid={t}]')
            check(b('export-btn').is_disabled(), 'before a run: Export tuning.h is disabled')
            check(not b('report-btn').is_disabled(), 'before a run: Report is enabled')
            pick(page, 'tune_startup.py')

            # ---- a run: the Scope tab names the unit, the table fills ----
            click_run(page)
            wait_running(page)
            check(b('report-btn').is_disabled() and b('export-btn').is_disabled(), 'during a run: Report and Export are disabled')
            page.get_by_role('tab', name='Scope').click()
            end, pts = time.time() + 20, []
            while time.time() < end:
                pts = page.evaluate(CHART_JS)
                if len(pts) == 4 and pts[0][1] > 100:
                    break
                time.sleep(0.5)
            names = [p[0] for p in pts]
            check(names[:2] == ['motor.speed  [rpm]', 'motor.iq  [A]'], f'during a run: the Scope tab names the unit in the legend {names}')
            top = page.evaluate("""() => { const e = document.querySelector('.nicegui-echart');
                const s = getElement(+e.id.slice(1)).chart.getOption().series[0].data; return Math.max(...s.map(p => Math.abs(p[1]))); }""")
            page.get_by_role('tab', name='Script').click()
            final = wait_final(page)
            check('ok, script ended' in final, f'status line at the end: {final!r}')
            folder = newest_run(runs, 'tune_startup_')
            tuning = read_file(os.path.join(folder, 'tuning.h'))
            check(re.search(r'#define RAMP_RATE 100\b', tuning) is not None,
                  'the result uses raw values (the unit factor is display only): tuning.h has RAMP_RATE 100')
            check(0 < top < 20000, f'during a run: the live chart shows physical values (largest |motor.speed| {top:g} rpm)')

            # ---- the table ----
            page.locator('[data-testid=res-table] tbody tr').first.wait_for(timeout=10000)
            rs = res_rows(page)
            check(len(rs) == 4, f'table: 4 rows {[r[3] for r in rs]}')
            head = page.eval_on_selector_all('[data-testid=res-table] th', 'els => els.map(e => e.innerText.trim().toLowerCase())')
            head = [h.replace('arrow_upward', '') for h in head]       # the icon name is part of the text
            check(head == ['#', 'ramp_rate', 'good_starts', 'time_ms', 'status'], f'table: header {head}')
            check(all(r[1].split(', ')[0] in ('ok', 'failed', 'invalid', 'unfinished') and r[3][-1] == r[1] for r in rs),
                  f'table: the status column {[r[1] for r in rs]}')
            bad = [r for r in rs if r[1] != 'ok']
            check(all('ms-bad' in r[2] for r in bad) and all('ms-bad' not in r[2] for r in rs if r[1] == 'ok'),
                  f'table: failed and invalid rows are marked ({len(bad)} of {len(rs)})')
            marked = [r for r in rs if 'ms-mark' in r[2]]
            plain = [r for r in rs if 'ms-mark' not in r[2]]
            check(len(marked) == 1 and marked[0][3][1] == '100', f'table: the summary_mark row (ramp_rate 100) is marked {marked[:1]}')
            check(marked[0][4] != 'rgba(0, 0, 0, 0)' and marked[0][4] != plain[0][4],
                  f'table: the marked row has its own background {marked[0][4]} against {plain[0][4]}')
            order0 = [r[0] for r in rs]

            # ---- sorting ----
            th = page.locator('[data-testid=res-table] th', has_text='ramp_rate')
            th.click()
            page.wait_for_timeout(300)
            up = [r[3][1] for r in res_rows(page)]
            th.click()
            page.wait_for_timeout(300)
            down = [r[3][1] for r in res_rows(page)]
            nums = lambda l: [float(x) for x in l]
            check(nums(up) == sorted(nums(up)) and nums(down) == sorted(nums(down), reverse=True) and up != down,
                  f'sorting: ramp_rate ascending {up}, then descending {down}; arrival order of the ids was {order0}')

            # ---- a click on two rows ----
            ramps = [r[3][1] for r in res_rows(page)][:2]
            for rv in ramps:
                page.locator('[data-testid=res-table] tbody tr', has=page.locator(f'td:text-is("{rv}")')).first.click()
                page.wait_for_timeout(300)
            ov = page.evaluate(OVERLAY_JS)
            want = [f'ramp {rv}, start 1' for rv in ramps]
            names = [s['name'] for s in ov['series']]
            check(len(names) == 2 and all(any(w in n for n in names) for w in want) and all(re.match(r'\d{3} ', n) for n in names),
                  f'overlay: two rows give two series with the right names {names}, expected {want}')
            sel_rows = [r for r in res_rows(page) if 'ms-sel' in r[2]]
            check(len(sel_rows) == 2, 'overlay: the two clicked rows are highlighted')
            check('rpm' in ov['y'] and ov['x'] == 'time [ms]', f'overlay: axis names {ov["x"]!r}, {ov["y"]!r}')
            # one number against the CSV: physical = raw x 0.5, from trace start
            s0 = ov['series'][0]
            idx = int(s0['name'][:3])
            hdr, data_ = csv_cols(os.path.join(folder, f'trace_{idx:03d}.csv'))
            col, step = hdr.index('motor.speed [rpm]'), max(1, -(-len(data_) // 2000))
            k = min(range(len(s0['data'])), key=lambda i: -abs(s0['data'][i][1]))      # the point with the largest value
            xv, yv = s0['data'][k]
            ref = data_[k * step]
            raw = ref[hdr.index('motor.speed')]
            check(abs(yv - ref[col]) <= 1e-3 * max(1.0, abs(ref[col])) and abs(xv - ref[0]) < 1e-6 and raw != 0 and abs(yv - raw) > 1e-6,
                  f'overlay: trace {idx:03d} point {k}: chart {xv:g} ms, {yv:g} rpm; csv {ref[0]:g} ms, {ref[col]:g} rpm (raw {raw:g})')
            check(0 < s0['data'][0][0] <= 2 and s0['data'][-1][0] < 5000, f'overlay: the curve starts at trace start (first x = {s0["data"][0][0]:g} ms, last {s0["data"][-1][0]:g} ms)')
            bad_sel = [s for s in ov['series'] if s['dash'] == 'dashed']
            check(len(bad_sel) == len([r for r in sel_rows if r[1] != 'ok']), f'overlay: dashed lines are the failed or invalid ones ({len(bad_sel)})')
            os.makedirs(SHOTS, exist_ok=True)
            page.screenshot(path=os.path.join(SHOTS, 'script_results.png'), full_page=True)
            for r in sel_rows:
                page.locator(f'[data-testid=res-table] tbody tr[data-id="{r[0]}"]').click()
            page.wait_for_timeout(300)
            check(len(page.evaluate(OVERLAY_JS)['series']) == 0, 'overlay: a second click removes the series again')

            # ---- Export ----
            check(not b('export-btn').is_disabled(), 'after the run: Export tuning.h is enabled')
            b('export-btn').click()
            end = time.time() + 10
            while time.time() < end and not os.path.isfile(out_h):
                time.sleep(0.2)
            saved = read_file(out_h)
            check(re.search(r'#define RAMP_RATE 100\b', saved) is not None and saved == tuning,
                  f'export: the saved file holds RAMP_RATE 100 and equals the one of the run folder ({len(saved)} bytes)')

            # ---- Report ----
            b('report-btn').click()
            b('report-text').wait_for(timeout=20000)
            shown = b('report-text').inner_text()
            path = b('report-path').inner_text().strip()
            on_disk = read_file(path)
            check(os.path.normcase(path) == os.path.normcase(os.path.join(rep_dir, 'mini-scope_report.txt')) and on_disk != '',
                  f'report: the dialog names the file and it exists ({os.path.basename(path)}, {len(on_disk)} bytes)')
            check(shown.strip() == on_disk.strip() and 'Python' in shown, 'report: the dialog shows the text of the file')
            check(MARKER not in shown and MARKER not in on_disk and 'somebody' not in on_disk,
                  'report: none of the marker lines of the test input is in the file')
            page.screenshot(path=os.path.join(SHOTS, 'report_dialog.png'))
            page.get_by_role('button', name='Close').click()

            # ---- Export after a failed run ----
            click_run(page)
            wait_running(page)
            page.wait_for_function("() => !document.querySelector('[data-testid=abort-btn]').disabled", timeout=5000)
            page.get_by_role('button', name='Abort').click()
            final = wait_final(page, 30000)
            check('aborted' in final, f'second run: {final!r}')
            check(b('export-btn').is_disabled(), 'after an aborted run: Export tuning.h is disabled (no tuning.h)')

            # ---- a script without table(): the traces are listed ----
            pick(page, 'tune_notable.py')
            click_run(page)
            wait_running(page)
            wait_log(page, 'trace 2:')
            page.get_by_role('button', name='Abort').click()
            wait_final(page, 30000)
            rs = res_rows(page)
            title = page.locator('[data-testid=table-title]').inner_text()
            check(title == 'Traces' and len(rs) >= 2 and any(r[3][1].startswith('ramp') for r in rs),
                  f'no table(): the table lists the traces ({title!r}, {len(rs)} rows, {[r[3][:2] for r in rs]})')
            browser.close()
    finally:
        gui.stop()


def test_real_scripts(tmp):
    """tune_speed.py and tune_current.py from the list of scripts/, one run each on SIM: the run ends ok, the table has one
    row for each row of results.csv, a click on a row draws its trace(s). The capture of tune_current.py is in
    microseconds in its CSV: the overlay has to show the same times in ms, from the pretrigger on."""
    cfg = os.path.join(tmp, 'settings_real.json')
    write_cfg(cfg, {'selected': '', 'extra': []}, channels=[{'name': 'motor.run', 'addr': '0x1100', 'type': 'u16', 'factor': 1}])
    runs = os.path.join(tmp, 'runs_real')
    gui = Gui(cfg, '--sim', '--runs', runs)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge')
            page = browser.new_page(viewport={'width': 1500, 'height': 1300})
            page.goto(gui.url)
            to_script_tab(page)
            connect(page)
            page.wait_for_timeout(600)
            open_list(page)
            items = list_items(page)
            check('tune_speed.py' in items and 'tune_current.py' in items, f'the list holds both scripts: {items}')
            page.keyboard.press('Escape')
            for name, head in (('tune_speed.py', ['kp', 'ki', 'offset', 'overshoot_pct', 'settle_ms']),
                               ('tune_current.py', ['kp', 'rise_us', 'overshoot_percent'])):
                stem = name[:-3] + '_'
                pick(page, name)
                click_run(page)
                final = wait_final(page, 240000)
                check('ok, script ended' in final, f'{name}: status line {final!r}')
                folder = newest_run(runs, stem)
                with open(os.path.join(folder, 'results.csv'), newline='', encoding='utf-8') as f:
                    csv_rows = list(__import__('csv').reader(f))
                page.locator('[data-testid=res-table] tbody tr').first.wait_for(timeout=10000)
                rs = res_rows(page)
                th = page.eval_on_selector_all('[data-testid=res-table] th', 'els => els.map(e => e.innerText.trim().toLowerCase())')
                th = [h.replace('arrow_upward', '') for h in th]
                check(th == ['#'] + head + ['status'] and csv_rows[0] == head + ['status'], f'{name}: table header {th}')
                check(len(rs) == len(csv_rows) - 1 > 0, f'{name}: {len(rs)} table rows, results.csv has {len(csv_rows) - 1}')
                check([r[1] for r in rs] == [c[-1] for c in csv_rows[1:]], f'{name}: the status column of the table is the one of results.csv')
                check(all(r[1] in ('ok', 'failed', 'invalid', 'unfinished') for r in rs),
                      f'{name}: row status of the table {sorted(set(r[1] for r in rs))}, of the CSV {sorted(set(r[-1] for r in csv_rows[1:]))}')
                page.locator('[data-testid=res-table] tbody tr').first.click()
                page.wait_for_timeout(400)
                ov = page.evaluate(OVERLAY_JS)
                check(len(ov['series']) >= 1 and all(len(s['data']) > 10 for s in ov['series']), f'{name}: overlay of the first row has {len(ov["series"])} series')
                check(ov['x'] == 'time [ms]' and ov['y'], f'{name}: overlay axes {ov["x"]!r}, {ov["y"]!r}')
                if name == 'tune_current.py':
                    s0 = ov['series'][0]
                    idx = int(s0['name'][:3])
                    with open(os.path.join(folder, f'trace_{idx:03d}.csv'), newline='', encoding='utf-8') as f:
                        tr = list(__import__('csv').reader(f))
                    check(tr[0][0] == 'time_us', f'tune_current.py: the CSV of a capture has time_us ({tr[0][0]})')
                    us0, us1 = float(tr[1][0]), float(tr[-1][0])
                    check(abs(s0['data'][0][0] * 1000 - us0) < 1e-6 and us0 < 0 and abs(s0['data'][-1][0] * 1000 - us1) < 1e-3 * max(1.0, abs(us1)) + 50,
                          f'tune_current.py: overlay from {s0["data"][0][0]:g} to {s0["data"][-1][0]:g} ms, the CSV from {us0:g} to {us1:g} us')
            browser.close()
    finally:
        gui.stop()


if __name__ == '__main__':
    tmp = tempfile.mkdtemp(prefix='ms_gui_')
    try:
        want = sys.argv[1:]         # names of tests to run, none: all
        for name, fn in (('editor', lambda: test_editor_start()), ('hook', lambda: test_shutdown_hook()), ('elf', lambda: test_elf_list()),
                         ('main', lambda: test_main(tmp)), ('noelf', lambda: test_no_elf(tmp)),
                         ('scripts', lambda: test_scripts(tmp)), ('results', lambda: test_results(tmp)), ('real', lambda: test_real_scripts(tmp)), ('life', lambda: test_page_lifetime(tmp)),
                         ('lost', lambda: test_port_lost(tmp)), ('break', lambda: test_ctrl_break(tmp))):
            if not want or name in want:
                fn()
    finally:
        if os.path.exists(NEW_SCRIPT):
            os.remove(NEW_SCRIPT)
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if getattr(check, 'failed', False) else 0)
