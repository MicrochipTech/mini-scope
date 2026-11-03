"""script_tab.py - the Script tab of the GUI: select a tuning script, check it, see its problems.

    handle = script_tab.build(get_table, get_baud, pick_file, host)      # inside the tab panel

scope_gui hands in what the tab needs (get_table: the VarTable of the loaded ELF or None, get_baud, pick_file, and
host, see build()); this module does not import scope_gui, because the GUI runs as __main__.
Stage 1 (compile) runs when a file is selected and when it is saved (the file's time stamp is watched).
Check runs stages 1 to 3 (tune_check.check_script): no connection, nothing is written.
Run and Probe start tune_api on its own threads. Their events are collected in RUN, one object for the whole
process: tune_api calls RUN.on_event on its delivery thread, which never touches the UI. Every page reads RUN
from its own ui.timer and draws what is new for it. So a second page, or a page after a reload, shows the same run,
its log so far included. Only one run exists at a time (tune_api refuses a second).
A run nobody watches is aborted: GRACE_S seconds without a connected page, and the abort goes to the safe end.
"""
import base64
import collections
import html
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import NamedTuple

import numpy as np
from nicegui import run, ui

import tune_api
import tune_check

SCRIPTS = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))
ROOT = os.path.dirname(SCRIPTS)
WATCH_S = 1.0               # how often the selected file's time stamp is looked at
MAX_LINES = 5000            # lines shown in the code view
LOG_LINES = 5000            # lines of the run log that are kept
KEEP_TRACES = 200           # traces whose samples stay in memory; older ones are named by their file
KEEP_BYTES = 256 << 20      # and at most this many bytes of samples (a trace can hold up to 200000 samples)
MAX_POINTS = 2000           # points per curve in the overlay chart
GRACE_S = 5.0               # a run without a connected page is aborted after this
SHUTDOWN_WAIT_S = 8.0       # shutdown() waits this long for the end of a run
HINT = 'Click rows of the table to overlay their curves.'
STATE_TEXT = {'starting': 'checking the script', 'probe': 'probing the target', 'trial': 'trial stream',
              'running': 'running', 'safe_end': 'safe end', 'done': 'done'}
PROBLEM_RE = re.compile(r'^(?P<file>[^:\s]+):(?P<line>\d+): (?P<sev>error|warning): (?P<text>.*?)(?: \[(?P<code>[EW]\d+)\])?$')
CSS = '''
.ms-code { font-family: monospace; font-size: 13px; white-space: pre; line-height: 1.35; }
.ms-code .cl { padding: 0 8px 0 0; min-height: 1.35em; }
.ms-code .ln { display: inline-block; width: 3.5em; text-align: right; margin-right: 1em; opacity: 0.5;
               user-select: none; }
.ms-code .hl { background: rgba(255, 193, 7, 0.28); }
.ms-row { cursor: pointer; }
.ms-row.ms-bad td { color: #ff8a80; }
.ms-row.ms-mark td { background: rgba(255, 193, 7, 0.30); font-weight: bold; }
.ms-row.ms-sel td { background: rgba(33, 150, 243, 0.40); }
.ms-row.ms-mark.ms-sel td { background: rgba(255, 193, 7, 0.45); }
.ms-report { white-space: pre-wrap; font-family: monospace; font-size: 13px; max-height: 24rem; overflow: auto; }
'''
SHOW_LINE_JS = '''(() => { const c = document.getElementById('ms-code'); if (!c) return;
    c.querySelectorAll('.hl').forEach(e => e.classList.remove('hl'));
    const l = document.getElementById('ms-cl-%d'); if (!l) return;
    l.classList.add('hl'); c.scrollTop = Math.max(0, l.offsetTop - c.clientHeight / 2); })()'''


class Handle(NamedTuple):
    settings: object        # () -> dict for mini-scope.json
    apply: object           # (dict) -> None, after mini-scope.json was loaded
    opened: object          # () -> None, when the tab is shown: reads the folder again


def list_scripts(folder=SCRIPTS):
    """All .py files of the folder, sorted by name."""
    try:
        return sorted(os.path.join(folder, n) for n in os.listdir(folder) if n.lower().endswith('.py'))
    except OSError:
        return []


def first_comment(path):
    """Text of the first comment line of the script ('' if there is none). A shebang line is skipped."""
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for _, line in zip(range(60), f):
                s = line.strip()
                if s.startswith('#') and not s.startswith('#!'):
                    return s.lstrip('#').strip()
                if s and not s.startswith('#'):
                    return ''
    except OSError:
        pass
    return ''


def read_lines(path):
    with open(path, 'rb') as f:
        return f.read().decode('utf-8', errors='replace').replace('\r\n', '\n').split('\n')[:MAX_LINES]


def code_html(lines):
    """One div per line: line number and text, escaped. White space is kept by the CSS."""
    return ''.join(f'<div class="cl" id="ms-cl-{i}"><span class="ln">{i}</span>'
                   f'{html.escape(s.expandtabs(4)) or " "}</div>' for i, s in enumerate(lines, 1))


def open_in_editor(path):
    """Starts the editor of the OS on the script. Never runs it: on Windows the default action of .py may be
    'run', so the verb 'edit' is used and Notepad is the fallback. Returns '' or an error text."""
    try:
        if sys.platform == 'win32':
            try:
                os.startfile(path, 'edit')              # noqa: S606 - the verb 'edit' opens, it does not run
            except OSError:
                subprocess.Popen(['notepad.exe', path])
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', '-t', path])
        else:
            subprocess.Popen(['xdg-open', path])
    except OSError as e:
        return f'Could not start an editor: {e.strerror or e}'
    return ''


def stored(path):
    """Path for mini-scope.json: relative to the project folder if inside it."""
    try:
        rel = os.path.relpath(path, ROOT)
        if not rel.startswith('..') and not os.path.isabs(rel):
            return rel.replace(os.sep, '/')
    except ValueError:                  # other drive
        pass
    return path


def resolved(text):
    return os.path.normpath(text if os.path.isabs(text) else os.path.join(ROOT, text))


def problem_row(p):
    """A Problem of tune_check as the row the list shows."""
    return {'sev': p.severity, 'code': p.code, 'line': p.line, 'text': str(p)}


def problem_row_from_text(text):
    """'file:line: error: text [E101]' (a result of tune_api) as a row; other text becomes an error without a line."""
    m = PROBLEM_RE.match(text.strip())
    if not m:
        return {'sev': 'error', 'code': '', 'line': None, 'text': text.strip()}
    return {'sev': m['sev'], 'code': m['code'] or '', 'line': int(m['line']),
            'text': f"{m['file']}:{m['line']}: {m['sev']}: {m['text']}"}


class RunState:
    """What the pages show about the current or last run. One object per process, written by tune_api's delivery
    thread (on_event) and by the click handlers, read by the ui.timer of every page. All fields change under lock;
    a counter next to each list lets a page see what is new for it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = False
        self.kind = None            # 'run' | 'probe'
        self.scope = None           # the Scope the run uses, for a page that attaches later
        self.session = None
        self.state = None           # last 'state' event; None until the session exists
        self.script = ''
        self.aborting = False
        self.lost = False           # the run ended because the port failed
        self.status = ''
        self.status_seq = 0
        self.log = collections.deque(maxlen=LOG_LINES)
        self.log_n = 0              # lines added since the last begin(): a page keeps its own count
        self.log_epoch = 0          # counts begin(): a page that sees a new epoch starts its log again
        self.problems, self.problems_seq = [], 0
        self.rows, self.rows_seq = [], 0
        self.channels, self.chan_seq = None, 0
        self.png, self.png_seq = None, 0
        self.result = None
        self.columns = None         # names of table(), None: the run has no table
        self.table_rows = []        # [{'values', 'invalid', 'traces': [trace indices]}] in the order of row()
        self.trace_meta = []        # every trace: index, title, file, flags, samples, time unit (small)
        self.trace_data = collections.OrderedDict()     # index -> (time, {name: raw values}); the last KEEP_TRACES only
        self.table_seq = 0
        self.mark = None            # summary_mark() of the finished run
        self.summary_chan = None

    # -- the click handlers -----------------------------------------------------------------------

    def begin(self, kind, scope, script):
        """False if a run is active. Otherwise everything of the last run is cleared and the run is 'starting'."""
        with self.lock:
            if self.active:
                return False
            self.active, self.kind, self.scope, self.session, self.state = True, kind, scope, None, None
            self.script, self.aborting, self.lost, self.result = script, False, False, None
            self.log.clear()
            self.log_n, self.log_epoch = 0, self.log_epoch + 1
            self.problems, self.rows, self.channels, self.png = [], [], None, None
            self.columns, self.table_rows, self.trace_meta = None, [], []
            self.trace_data = collections.OrderedDict()
            self.mark = self.summary_chan = None
            self.table_seq += 1
            self.problems_seq += 1
            self.chan_seq += 1
            self.rows_seq += 1
            self.png_seq += 1
            self._status(f'{self._what()}: checking the script')
        threading.Thread(target=self._watch, name='run-watch', daemon=True).start()
        return True

    def refused(self, exc):
        """run() or probe() raised RunRefused: nothing was sent."""
        rows = [problem_row(p) for p in getattr(exc, 'problems', [])]
        with self.lock:
            self.problems, self.problems_seq = rows, self.problems_seq + 1
            self._status(f'{self._what()} refused, nothing was sent: {str(exc).splitlines()[0]}')
            self.active = False

    def failed(self, text):
        with self.lock:
            self.problems = [{'sev': 'error', 'code': '', 'line': None, 'text': text}]
            self.problems_seq += 1
            self._status(f'{self._what()}: {text}')
            self.active = False

    def attach(self, session):
        with self.lock:
            self.session = session

    def end_if_open(self):
        """The session is over; make sure that the run is closed even if the 'done' event was lost."""
        with self.lock:
            if self.active and self.result is None:
                self._status(f'{self._what()}: ended without a result')
            self.active = False

    # -- tune_api events (delivery thread of the session) -----------------------------------------

    def on_event(self, ev):
        kind = ev['type']
        with self.lock:
            if kind == 'state':
                self.state = ev['state']
                self._add_log('-- ' + STATE_TEXT.get(ev['state'], ev['state']))
                if ev['state'] == 'done':
                    self.active = False
                else:
                    self._status(f'{self._what()}: {STATE_TEXT.get(ev["state"], ev["state"])}')
            elif kind == 'log':
                self._add_log(ev['text'])
                if PROBLEM_RE.match(ev['text']) and ev['text'].endswith(']'):     # warnings of stages 1 to 3
                    self._add_problem(problem_row_from_text(ev['text']))
            elif kind == 'variables':
                self.rows, self.rows_seq = ev['rows'], self.rows_seq + 1
            elif kind == 'channels':
                self.channels, self.chan_seq = ev, self.chan_seq + 1
            elif kind == 'table':
                self.columns, self.table_seq = list(ev['columns']), self.table_seq + 1
            elif kind == 'row':
                self.table_rows.append({'values': list(ev['values']), 'invalid': bool(ev['invalid']),
                                        'traces': list(ev.get('row_traces', []))})
                self.table_seq += 1
            elif kind == 'trace':
                self._add_log(f"trace {ev['index']}: {ev['title']}" + (' (failed)' if ev['failed'] else ''))
                self._add_trace(ev)
            elif kind == 'result':
                self._result(ev)

    def _add_trace(self, ev):
        """The samples are copied (an event array can be a view of the whole stream) and only the last KEEP_TRACES
        stay: about 8 bytes x (channels + 1) x samples each. The meta data of every trace stays, it is small."""
        x = ev['time_ms'] if 'time_ms' in ev else ev['time']
        unit = ev.get('time_unit', 'ms')
        self.trace_meta.append({'index': ev['index'], 'title': ev['title'], 'file': ev['file'], 'failed': bool(ev['failed']),
                                'invalid': bool(ev['invalid']), 'unfinished': bool(ev.get('unfinished')),
                                'n': len(x), 'time_unit': unit})
        self.trace_data[ev['index']] = (np.array(x, dtype=float), {k: np.array(v, dtype=float) for k, v in ev['values'].items()})
        while len(self.trace_data) > KEEP_TRACES or (len(self.trace_data) > 1 and sum(
                t.nbytes + sum(v.nbytes for v in d.values()) for t, d in self.trace_data.values()) > KEEP_BYTES):
            self.trace_data.popitem(last=False)
        self.table_seq += 1

    def _what(self):
        return f'{"Run" if self.kind == "run" else "Probe"} {self.script}'

    def _result(self, r):
        self.result = r
        for text in list(r.get('errors') or []) + list(r.get('problems') or []):
            self._add_problem(problem_row_from_text(text))
        if r['status'] == 'error' and PROBLEM_RE.match(r['reason']):    # a runtime error of the script, with its line
            self._add_problem(problem_row_from_text(r['reason']))
        self.mark, self.summary_chan = r.get('summary_mark'), r.get('summary_channel')
        self.table_seq += 1
        png = r.get('summary_png')
        if png:
            try:
                with open(png, 'rb') as f:
                    self.png, self.png_seq = f.read(), self.png_seq + 1
            except OSError:
                pass
        self.lost = (r['reason'].startswith('port error')
                     or (self.session is not None and not getattr(self.session, 'port_ok', True)))
        self._status(f'{self._what()}: {r["status"]}, {r["reason"]}' + (' (the port was lost)' if self.lost else ''))

    def _add_log(self, text):
        self.log.append(text)
        self.log_n += 1

    def _add_problem(self, row):
        if all(p['text'] != row['text'] for p in self.problems):
            self.problems = self.problems + [row]
            self.problems_seq += 1

    def _status(self, text):
        self.status, self.status_seq = text, self.status_seq + 1

    # -- abort ------------------------------------------------------------------------------------

    def abort(self, reason):
        """Aborts the run once its session exists. False if there is nothing to abort (yet)."""
        with self.lock:
            if not self.active:
                return False
            self.aborting = True
        return tune_api.abort(reason)

    def _watch(self):
        """A run nobody can abort must not go on: no connected page for GRACE_S seconds aborts it."""
        gone = None
        while self.active:
            if CONNECTED:
                gone = None
            elif gone is None:
                gone = time.monotonic()
            elif time.monotonic() - gone >= GRACE_S and self.abort(f'no browser page for {GRACE_S:g} s'):
                with self.lock:
                    self._add_log(f'no browser page for {GRACE_S:g} s: the run is aborted')
                return
            time.sleep(0.25)


RUN = RunState()
CONNECTED = set()               # ids of the clients (pages) that have a socket


def client_connected(client):
    CONNECTED.add(client.id)


def client_disconnected(client):
    CONNECTED.discard(client.id)


def shutdown(wait_s=SHUTDOWN_WAIT_S):
    """The GUI process ends (Ctrl+C, closed window): abort an active run and wait for its safe end, bounded."""
    end = time.monotonic() + wait_s
    while RUN.active and time.monotonic() < end:
        RUN.abort('GUI closed')
        time.sleep(0.1)
    return not RUN.active


def on_app_shutdown():
    """The hook for nicegui_app.on_shutdown. NiceGUI passes the app to a handler that has one parameter, so a handler
    with a default parameter (shutdown) would get the app as its wait time and fail before the abort."""
    shutdown()


def table_view(columns, table_rows, meta, mark, selected):
    """The comparison table as a QTable sees it: (columns, rows, info). Without table() the rows are the traces.
    Row ids count from 1 in the order of arrival. info[id] = {'traces': [indices], 'bad': bool}."""
    by = {m['index']: m for m in meta}
    if columns:
        names = list(columns)
        items = [(r['values'], r['invalid'], r['traces']) for r in table_rows]
    else:
        names = ['trace', 'samples']
        items = [([m['title'], m['n']], False, [m['index']]) for m in meta]
    cols = [{'name': 'id', 'label': '#', 'field': 'id', 'sortable': True, 'align': 'right'}]
    cols += [{'name': f'c{i}', 'label': n, 'field': f'c{i}', 'sortable': True, 'align': 'left'} for i, n in enumerate(names)]
    cols.append({'name': 'status', 'label': 'status', 'field': 'status', 'sortable': True, 'align': 'left'})
    rows, info, marked = [], {}, False
    for i, (values, invalid, traces) in enumerate(items, 1):
        status = tune_api.row_status(invalid, [by.get(t, {}) for t in traces])
        bad = status != 'ok'
        is_mark = bool(columns) and mark is not None and not marked and any(tune_api._same(v, mark) for v in values)
        marked = marked or is_mark
        cls = 'ms-row' + (' ms-bad' if bad else '') + (' ms-mark' if is_mark else '') + (' ms-sel' if i in selected else '')
        rows.append({'id': i, **{f'c{k}': v for k, v in enumerate(values)}, 'status': status, 'cls': cls,
                     'mark': is_mark})
        info[i] = {'traces': list(traces), 'bad': bad}
    return cols, rows, info


def overlay_series(selected, info, meta, data, chan, factor, mark_ids=()):
    """ECharts series of the selected rows for one channel, in physical units (raw x factor), from trace start.
    Failed and invalid traces are dashed. Returns (series, missing, time unit): missing = metas whose samples are no longer
    in memory (only the last KEEP_TRACES are kept, the others are in their csv file)."""
    by = {m['index']: m for m in meta}
    series, missing, tunit = [], [], None
    for rid in sorted(selected):
        for t in info.get(rid, {}).get('traces', []):
            m = by.get(t)
            if m is None:
                continue
            if t not in data:
                missing.append(m)
                continue
            x, vals = data[t]
            if chan not in vals:
                continue
            tunit = tunit or m['time_unit']
            step = max(1, -(-len(x) // MAX_POINTS))
            dashed = info[rid]['bad'] or m['failed'] or m['invalid'] or m['unfinished']
            flags = [k for k in ('failed', 'invalid', 'unfinished') if m[k]]
            series.append({'name': f'{m["index"]:03d} {m["title"]}' + (f' ({", ".join(flags)})' if flags else ''),
                           'type': 'line', 'showSymbol': False, 'data': np.column_stack((x[::step], vals[chan][::step] * factor)).tolist(),
                           'lineStyle': {'type': 'dashed' if dashed else 'solid', 'width': 3 if rid in mark_ids else 1.5}})
    return series, missing, tunit or 'ms'


def build(get_table, get_baud, pick_file, host):
    """host (scope_gui): scope() the connected Scope or None, adopt(scope) a page that comes while a run is on, busy(on)
    locks or frees the Scope tab, channels(event) shows the stream of the script, lost() the port failed,
    elf_name(), runs_dir, render_png."""
    st = {'sel': set(), 'chan': None, 'path': None, 'extra': [], 'stamp': None, 'problems': [], 'status': 'Select a script.', 'line': None}
    ui.add_css(CSS)

    def options():
        listed = {p: os.path.basename(p) for p in list_scripts()}
        for p in st['extra']:
            if os.path.isfile(p) and p not in listed:
                listed[p] = f'{os.path.basename(p)} (other folder)'
        return listed

    def refresh_options():
        opts = options()
        sel.set_options(opts, value=sel.value if sel.value in opts else None)

    def stamp(path):
        try:
            s = os.stat(path)
            return (s.st_mtime_ns, s.st_size)
        except OSError:
            return None

    def load_file(quiet=False):
        """Reads the script again, shows it and runs stage 1."""
        path = st['path']
        st['stamp'] = stamp(path) if path else None
        st['line'] = None
        if not path:
            comment.text, file_name.text = '', ''
            code.set_content('')
            set_problems([], 'Select a script.')
            return
        comment.text = first_comment(path)
        file_name.text = os.path.basename(path)
        try:
            code.set_content(code_html(read_lines(path)))
        except OSError:
            code.set_content('')
        probs = tune_check.check_compile(path)
        set_problems([problem_row(p) for p in probs], summary(probs, 'Stage 1 (compile)') + ' Press Check for stages 2 and 3.')

    def summary(probs, what):
        probs = [problem_row(p) if hasattr(p, 'severity') else p for p in probs]
        errors = sum(p['sev'] == 'error' for p in probs)
        warnings = len(probs) - errors
        if not probs:
            return f'{what}: no problems.'
        return f'{what}: {errors} error{"" if errors == 1 else "s"}, {warnings} warning{"" if warnings == 1 else "s"}.'

    def set_problems(probs, status):
        st['problems'], st['status'] = probs, status
        status_label.text = status
        problem_list.refresh()

    def on_select(e):
        st['path'] = e.value
        load_file()

    def watch():
        """A saved file is compiled again at once: no restart, no button."""
        path = st['path']
        if path and not RUN.active and stamp(path) != st['stamp']:      # the problems of a run stay
            load_file()

    async def show_line(line):
        st['line'] = line
        await ui.run_javascript(SHOW_LINE_JS % line)

    async def check():
        path = st['path']
        if not path:
            ui.notify('Select a script first', type='warning')
            return
        check_btn.disable()
        try:
            load_file()                         # the file as it is on disk now; stage 1 shown at once
            table = get_table()
            if table is None:
                set_problems(st['problems'], summary(st['problems'], 'Stage 1 (compile)')
                             + ' An ELF file is needed for stages 2 and 3: enter it above and press Load.')
                return
            probs = await run.io_bound(tune_check.check_script, path, table, get_baud())
            set_problems([problem_row(p) for p in probs], summary(probs, 'Stages 1 to 3'))
        finally:
            check_btn.enable()

    async def browse():
        start = os.path.dirname(st['path']) if st['path'] else SCRIPTS
        try:
            path = await run.io_bound(pick_file, start, 'Select a tuning script',
                                      [('Python files', '*.py'), ('All files', '*.*')])
        except Exception as e:      # noqa: BLE001 - e.g. no tkinter in this Python
            ui.notify(f'File dialog not available: {e}', type='warning')
            return
        if path:
            add_file(os.path.normpath(path))

    def add_file(path):
        if path not in st['extra'] and path not in list_scripts():
            st['extra'].append(path)
        refresh_options()
        sel.value = path

    def open_editor():
        if not st['path']:
            ui.notify('Select a script first', type='warning')
            return
        err = open_in_editor(st['path'])
        if err:
            ui.notify(err, type='negative')

    # ---- layout ----
    with ui.row().classes('items-center w-full'):
        sel = ui.select({}, label='Script', on_change=on_select).classes('w-72').props('data-testid=script-select')
        sel.on('popup-show', refresh_options)       # the folder is read again each time the list opens
        browse_btn = ui.button('Browse ...', on_click=browse).props('outline')
        ui.button('Open', on_click=open_editor).props('outline').tooltip('Open the script in your editor')
        check_btn = ui.button('Check', on_click=check).tooltip('Stages 1 to 3: no connection needed, '
                                                               'nothing is written')
        run_btn = ui.button('Run', on_click=lambda: start('run')).props('data-testid=run-btn') \
            .tooltip('Stages 1 to 4, then the script')
        abort_btn = ui.button('Abort', on_click=lambda: abort(), color='negative').props('data-testid=abort-btn') \
            .tooltip('End the run in the safe end')
        probe_btn = ui.button('Probe', on_click=lambda: start('probe')).props('outline data-testid=probe-btn') \
            .tooltip('Stage 4 alone: reads the variables, nothing is started')
        export_btn = ui.button('Export tuning.h', on_click=lambda: export(), color='positive') \
            .props('outline data-testid=export-btn').tooltip('Save a copy of the tuning.h of the last run')
        report_btn = ui.button('Report', on_click=lambda: report()).props('outline data-testid=report-btn') \
            .tooltip('Writes mini-scope_report.txt and shows it. Nothing is sent.')
        for b in (run_btn, abort_btn, probe_btn, export_btn):
            b.disable()
        comment = ui.label().classes('text-gray-400 italic')
    status_label = ui.label(st['status']).props('data-testid=script-status')

    with ui.row().classes('w-full no-wrap items-stretch'):
        with ui.card().classes('w-1/2'):
            ui.label('Problems').classes('font-bold')

            @ui.refreshable
            def problem_list():
                if not st['problems']:
                    ui.label('none').classes('text-gray-400')
                for p in st['problems']:
                    err = p['sev'] == 'error'
                    with ui.row().classes('problem-row items-center no-wrap w-full cursor-pointer') \
                            .props(f'data-sev={p["sev"]} data-code={p["code"]} data-line={p["line"] or ""}') \
                            .on('click', lambda p=p: show_line(p['line']) if p['line'] else None):
                        ui.icon('error' if err else 'warning', color='negative' if err else 'warning')
                        ui.label(p['text']).classes('grow')
                        if p['code']:
                            ui.badge(p['code'], color='negative' if err else 'warning').props('outline')
            problem_list()
        with ui.card().classes('w-1/2'):
            file_name = ui.label().classes('font-bold')
            with ui.element('div').props('id=ms-code').classes('ms-code w-full') \
                    .style('height: 24rem; overflow: auto; position: relative'):
                code = ui.html('', sanitize=False)

    with ui.row().classes('w-full no-wrap items-stretch'):
        with ui.card().classes('w-1/2'):
            table_title = ui.label('Comparison table').classes('font-bold').props('data-testid=table-title')
            res_table = ui.table(columns=[], rows=[], row_key='id', pagination={'rowsPerPage': 0}) \
                .props('dense flat hide-bottom data-testid=res-table').classes('w-full') \
                .style('max-height: 26rem; overflow: auto')
            res_table.add_slot('body', """
                <q-tr :props="props" :class="props.row.cls" :data-id="props.row.id" :data-status="props.row.status"
                      @click="() => $parent.$emit('pick', props.row.id)">
                    <q-td v-for="col in props.cols" :key="col.name" :props="props">{{ col.value }}</q-td>
                </q-tr>""")
            res_table.on('pick', lambda e: pick_row(e.args))
            table_note = ui.label().classes('text-gray-400 text-sm')
        with ui.card().classes('w-1/2'):
            with ui.row().classes('items-center w-full'):
                ui.label('Selected rows, from trace start').classes('font-bold')
                chan_sel = ui.select([], label='Channel', on_change=lambda e: pick_chan(e.value)) \
                    .props('dense data-testid=overlay-channel').classes('w-48')
            overlay = ui.echart({
                'backgroundColor': 'transparent', 'animation': False, 'tooltip': {'trigger': 'axis'},
                'legend': {'type': 'scroll', 'top': 0, 'textStyle': {'color': '#ddd'}},
                'grid': {'left': 80, 'right': 20, 'top': 50, 'bottom': 50},
                'xAxis': {'type': 'value', 'name': 'time [ms]', 'nameLocation': 'middle', 'nameGap': 28, 'scale': True,
                          'nameTextStyle': {'color': '#ddd'},
                          'splitLine': {'lineStyle': {'color': '#333'}}},
                'yAxis': {'type': 'value', 'name': '', 'nameLocation': 'middle', 'nameGap': 60, 'scale': True,
                          'nameTextStyle': {'color': '#ddd'},
                          'splitLine': {'lineStyle': {'color': '#333'}}},
                'dataZoom': [{'type': 'inside'}],
                'series': []}, theme='dark').classes('w-full h-80').props('data-testid=overlay')
            overlay_note = ui.label(HINT).classes('text-gray-400 text-sm')

    with ui.card().classes('w-full') as summary_card:
        ui.label('Summary').classes('font-bold')
        summary_img = ui.image().props('data-testid=summary-png').classes('w-full').style('max-width: 60rem')
    summary_card.visible = False

    with ui.row().classes('w-full no-wrap items-stretch'):
        with ui.card().classes('w-1/2'):
            ui.label('Variables').classes('font-bold')
            var_table = ui.table(columns=[{'name': k, 'label': lab, 'field': k, 'align': 'left' if k in ('name', 'role') else 'right'}
                                          for k, lab in (('name', 'Name'), ('role', 'Role'), ('address', 'Address'),
                                                         ('type', 'Type'), ('value', 'Value'))],
                                 rows=[], row_key='name').props('dense flat data-testid=var-table').classes('w-full')
        with ui.card().classes('w-1/2'):
            ui.label('Log').classes('font-bold')
            log = ui.log(max_lines=LOG_LINES).props('data-testid=run-log').classes('w-full h-64')
    cur = {'epoch': -1, 'log_n': 0, 'problems': None, 'rows': -1, 'png': -1, 'chan': None, 'status': None,
           'active': False, 'buttons': None, 'draw': None, 'epoch2': -1}

    def pick_chan(name):
        if name is not None and not cur.get('in_draw'):
            st['chan'] = name
            draw()

    def pick_row(rid):
        st['sel'] ^= {rid}
        draw()

    def draw():
        """The comparison table and the overlay chart, from RUN and the selection of this page."""
        R = RUN
        with R.lock:
            columns, trows, meta, data = R.columns, list(R.table_rows), list(R.trace_meta), dict(R.trace_data)
            mark, chans, want, seq, epoch = R.mark, R.channels, R.summary_chan, R.table_seq, R.log_epoch
        if epoch != cur['epoch2']:
            cur['epoch2'] = epoch
            st['sel'].clear()
            st['chan'] = None
        names = list(chans['names']) if chans else []
        cur['in_draw'] = True
        try:
            if names != list(chan_sel.options):
                chan_sel.set_options(names, value=None)
            if st['chan'] not in names:
                st['chan'] = want if want in names else (names[0] if names else None)
            if chan_sel.value != st['chan']:
                chan_sel.value = st['chan']
        finally:
            cur['in_draw'] = False
        key = (seq, epoch, frozenset(st['sel']), st['chan'], tuple(names))
        if key == cur['draw']:
            return
        cur['draw'] = key
        cols, rows, info = table_view(columns, trows, meta, mark, st['sel'])
        st['sel'] &= set(info)
        res_table.columns = cols
        res_table.rows = rows
        res_table.update()
        table_title.text = 'Comparison table' if columns or not meta else 'Traces'
        own = {t for r in trows for t in r['traces']}
        lone = sum(1 for m in meta if m['index'] not in own) if columns else 0
        table_note.text = (f'{lone} trace(s) belong to no row' if lone else
                           'This script has no table(): the traces are listed.' if meta and not columns else '')
        k = names.index(st['chan']) if st['chan'] in names else 0
        factor = chans['factors'][k] if names else 1.0
        unit_ = chans['units'][k] if names else ''
        marks = {r['id'] for r in rows if r['mark']}
        series, missing, tunit = overlay_series(st['sel'], info, meta, data, st['chan'], factor, marks)
        overlay.options['series'] = series
        overlay.options['xAxis']['name'] = f'time [{tunit}]'
        overlay.options['yAxis']['name'] = (f'{st["chan"]} [{unit_}]' if unit_ else f'{st["chan"]} (raw)') if st['chan'] else ''
        overlay.update()
        overlay_note.text = (HINT if not st['sel'] else
                             f'Not kept in memory (only the last {KEEP_TRACES} traces are): '
                             + ', '.join(f'{m["index"]:03d} in {os.path.basename(m["file"])}' for m in missing) if missing else '')

    def apply_busy(on):
        """The page follows the run: the Scope tab is locked, the script cannot change."""
        cur['active'] = on
        host.busy(on)
        sel.set_enabled(not on)
        browse_btn.set_enabled(not on)

    def buttons():
        """Run and Probe need a connection and a script and no run; Abort needs a run whose session exists."""
        R = RUN
        res = R.result
        want = (bool(host.scope() is not None and st['path']) and not R.active,
                R.active and R.state not in (None, 'done') and not R.aborting,
                bool(res and res.get('tuning_h') and os.path.isfile(res['tuning_h'])) and not R.active,
                not R.active)
        if want != cur['buttons']:
            cur['buttons'] = want
            for b in (run_btn, probe_btn):
                b.set_enabled(want[0])
            abort_btn.set_enabled(want[1])
            export_btn.set_enabled(want[2])
            report_btn.set_enabled(want[3])

    def tick():
        """Draws what the run produced since the last tick. The only reader of RUN; tune_api never calls the UI."""
        R = RUN
        with R.lock:
            active, scope = R.active, R.scope
            epoch, n = R.log_epoch, R.log_n
            fresh = epoch != cur['epoch']
            have = n - (0 if fresh else cur['log_n'])
            lines = list(R.log)[max(0, len(R.log) - have):] if have > 0 else []
            problems, p_seq, status, s_seq = R.problems, R.problems_seq, R.status, R.status_seq
            rows, r_seq, png, png_seq = R.rows, R.rows_seq, R.png, R.png_seq
            channels, c_seq, lost = R.channels, R.chan_seq, R.lost
        if cur['problems'] is None and not active:      # a page that starts while nothing runs keeps its own list
            cur['problems'], cur['status'] = p_seq, s_seq
        if active and not cur['active']:
            if host.scope() is None and scope is not None:      # a page that comes while a run is on (reload, second window)
                host.adopt(scope)
            apply_busy(True)
        if fresh:
            log.clear()
            cur['epoch'] = epoch
        for line in lines:
            log.push(line)
        cur['log_n'] = n
        if cur['problems'] != p_seq:
            cur['problems'] = p_seq
            st['problems'] = list(problems)
            problem_list.refresh()
        if cur['status'] != s_seq:
            cur['status'] = s_seq
            st['status'], status_label.text = status, status
        if cur['rows'] != r_seq:
            cur['rows'] = r_seq
            var_table.rows = [{**r, 'address': f"0x{r['address']:04X}" if isinstance(r['address'], int) else r['address'],
                               'value': str(r['value'])} for r in rows]
            var_table.update()
        if cur['png'] != png_seq:
            cur['png'] = png_seq
            summary_card.visible = png is not None
            if png is not None:
                summary_img.set_source('data:image/png;base64,' + base64.b64encode(png).decode())
        if active and channels is not None and cur['chan'] != c_seq:
            cur['chan'] = c_seq
            host.channels(channels)
        if not active and cur['active']:
            apply_busy(False)
            if lost:
                host.lost()
        draw()
        buttons()

    async def export():
        """A copy of tuning.h of the last run. MS_EXPORT_PATH replaces the dialog (tests)."""
        res = RUN.result
        src = res.get('tuning_h') if res else None
        if RUN.active or not src or not os.path.isfile(src):
            ui.notify('No tuning.h: the last run did not produce one', type='warning')
            return
        dest = os.environ.get('MS_EXPORT_PATH')
        if not dest:
            dest = await run.io_bound(pick_file, os.path.dirname(src), 'Save tuning.h as',
                                      [('C header', '*.h'), ('All files', '*.*')], True)
        if not dest:
            return
        try:
            shutil.copyfile(src, dest)
        except OSError as e:
            ui.notify(f'Not saved: {e}', type='negative')
            return
        ui.notify(f'Saved {dest}', type='positive')

    async def report():
        """Runs tools/report.py on the project folder and shows the file it wrote. Nothing is sent."""
        if RUN.active:
            return
        folder = os.environ.get('MS_REPORT_DIR') or ROOT
        out = os.path.join(folder, 'mini-scope_report.txt')
        cmd = [sys.executable, os.path.join(ROOT, 'tools', 'report.py'), '--out', out,
               '--doctor', os.path.join(folder, 'doctor_report.txt'), '--hw', os.path.join(folder, 'hw_report.txt'),
               '--run', os.path.join(folder, 'run_report.txt')]
        try:
            r = await run.io_bound(lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=folder))
            if r.returncode:
                raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else f'exit {r.returncode}')
            with open(out, encoding='utf-8') as f:
                text = f.read()
        except Exception as e:      # noqa: BLE001 - shown to the customer
            ui.notify(f'Report failed: {e}', type='negative')
            return
        with ui.dialog() as dlg, ui.card().classes('w-[48rem] max-w-full').props('data-testid=report-dialog'):
            ui.label('mini-scope_report.txt').classes('font-bold')
            ui.label(out).props('data-testid=report-path').classes('text-sm break-all')
            ui.label('Read it before you send it. Nothing has been sent; you decide whether to send this file.') \
                .classes('text-sm')
            ui.label(text).classes('ms-report w-full').props('data-testid=report-text')
            ui.button('Close', on_click=dlg.close)
        dlg.on('hide', dlg.delete)
        dlg.open()

    async def start(kind):
        path, scope, table = st['path'], host.scope(), get_table()
        if scope is None or not path:
            ui.notify('Connect and select a script first', type='warning')
            return
        if table is None:
            ui.notify('An ELF file is needed: enter it above and press Load', type='warning')
            return
        name = os.path.basename(path)
        if not RUN.begin(kind, scope, name):
            ui.notify('A run is active', type='warning')
            return
        apply_busy(True)
        tick()                      # this page shows the new run at once
        try:
            if kind == 'run':
                session = await run.io_bound(lambda: tune_api.run(path, scope, table, on_event=RUN.on_event,
                                                                  out_dir=host.runs_dir, elf_name=host.elf_name(),
                                                                  render_png=host.render_png))
                RUN.attach(session)
                threading.Thread(target=lambda: (session.wait(), RUN.end_if_open()), name='run-end', daemon=True).start()
            else:
                await run.io_bound(lambda: tune_api.probe(path, scope, table, on_event=RUN.on_event,
                                                          elf_name=host.elf_name()))
                RUN.end_if_open()
        except tune_api.RunRefused as e:
            RUN.refused(e)
        except Exception as e:      # noqa: BLE001 - whatever it is, the page must come back
            RUN.failed(f'{type(e).__name__}: {e}')

    def abort():
        if RUN.abort('Abort button'):
            with RUN.lock:
                RUN._add_log('Abort pressed: the run ends in the safe end')

    ui.timer(WATCH_S, watch)
    ui.timer(0.25, tick)

    def settings():
        return {'selected': stored(st['path']) if st['path'] else '', 'extra': [stored(p) for p in st['extra']]}

    def apply(cfg):
        if not isinstance(cfg, dict):
            return
        st['extra'] = [resolved(p) for p in cfg.get('extra', []) if isinstance(p, str)]
        refresh_options()
        want = resolved(cfg['selected']) if cfg.get('selected') else None
        if want and want in sel.options:
            sel.value = want

    refresh_options()
    default = os.path.join(SCRIPTS, 'tune_startup.py')
    if default in sel.options:
        sel.value = default
    return Handle(settings, apply, refresh_options)
