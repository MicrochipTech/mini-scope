"""scope_gui.py - PC side of mini-scope (NiceGUI in the browser).

    python scope_gui.py          pick the COM port in the browser
    python scope_gui.py --sim    no hardware: runs firmware/scope.c as tests/sim.dll
    python scope_gui.py --mdb test.X/dist/sim/production/test.X.production.elf
                                 no hardware: real firmware in the MPLAB X simulator (slow)

Protocol: see the comment block at the top of firmware/scope.c.
"""
import argparse
import io
import json
import math
import os
import struct
import sys
import threading
import time

import numpy as np
import serial
import serial.tools.list_ports
from elftools.elf.elffile import ELFFile
from nicegui import app as nicegui_app
from nicegui import run, ui

ACK = 0x06
SYNC_STREAM, SYNC_CAPTURE = 0xA5, 0xC5
TYPES = {'s8': 'i1', 'u8': 'u1', 's16': '<i2', 'u16': '<u2',
         's32': '<i4', 'u32': '<u4', 'f32': '<f4'}
DEFAULT_TYPE = {1: 's8', 2: 's16', 4: 's32'}
TRIGGERS = {0: 'none (immediate)', 1: 'rising', 2: 'falling'}
KEEP = 200_000         # stream samples kept for display / CSV
MAX_POINTS = 4000       # points per series sent to the browser
CHART_CLICK_JS = '''
        (function attach() {
            const el = getElement(%d);
            if (!el || !el.chart) { setTimeout(attach, 200); return; }
            const c = el.chart;
            c.getZr().on('click', e => {
                if (!c.containPixel({gridIndex: 0}, [e.offsetX, e.offsetY])) return;
                const p = c.convertFromPixel({gridIndex: 0}, [e.offsetX, e.offsetY]);
                emitEvent('chart_click', {x: p[0], y: p[1]});
            });
        })();'''   # chart id -> emits chart_click with the data coordinates
PNG_ZOOM_JS = '''(() => { const o = getElement(%d).chart.getOption().dataZoom[0];
    return [o.startValue, o.endValue, o.start, o.end]; })()'''   # chart id -> visible x range
CONFIG = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'mini-scope.json'))


def load_symbols(path):
    """name -> (address, size) of all 1/2/4 byte variables in data RAM."""
    syms = {}
    with open(path, 'rb') as f:
        for s in ELFFile(f).get_section_by_name('.symtab').iter_symbols():
            if (s['st_info']['type'] == 'STT_OBJECT' and s['st_size'] in (1, 2, 4)
                    and s['st_value'] < 0x8000 and s.name):
                name = s.name[1:] if s.name.startswith('_') else s.name
                syms[name] = (s['st_value'], s['st_size'])
    return dict(sorted(syms.items()))


class Scope:
    """Talks to firmware/scope.c over a serial port (or SimSerial)."""

    def __init__(self, ser):
        self.ser = ser
        self.lock = threading.Lock()
        self.thread = None
        self.running = False
        self.t = np.zeros(0)
        self.cols = []
        self.lost = self.captures = 0

    capbuf = max_ch = 0         # from the ping reply: SCOPE_CAPBUF / SCOPE_MAX_CH of the firmware

    def ping(self):
        self.ser.reset_input_buffer()
        self.ser.write(b'P')
        r = self.ser.read(7)
        if len(r) != 7 or r[:4] != b'SCP1':
            return False
        self.capbuf, self.max_ch = struct.unpack('<HB', r[4:])
        return True

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()
            self.thread = None
        self.ser.write(b'S')
        quiet = getattr(self.ser, 'quiet', 0.05)    # wait until no late bytes arrive any more
        last, seen = time.time(), self.ser.in_waiting
        while time.time() - last < quiet:
            time.sleep(0.02)
            if self.ser.in_waiting != seen:
                last, seen = time.time(), self.ser.in_waiting
        self.ser.reset_input_buffer()

    def reset(self):
        """Restarts the target firmware ('X'). Returns an error text or None."""
        self.stop()
        self.ser.write(b'X')
        end = time.time() + getattr(self.ser, 'reset_wait', 2.0)
        while time.time() < end:
            if self.ping():
                return None
        if hasattr(self.ser, 'reset_target'):     # MDB: firmware hangs, reset the simulated device
            self.ser.reset_target()
            end = time.time() + self.ser.reset_wait
            while time.time() < end:
                if self.ping():
                    return None
        return 'Target does not answer after the reset'

    def write(self, addr, typ, value):
        """Writes value into the variable at addr ('W', no reply). Returns an error text or None."""
        dt = np.dtype(TYPES[typ])
        if not 0 <= addr < 0x8000 or (dt.itemsize > 1 and addr & 1):
            return 'The address must be below 0x8000, and even for 16- and 32-bit variables'
        if dt.kind in 'iu':
            lim = np.iinfo(dt)
            if value != int(value) or not lim.min <= value <= lim.max:
                return f'{typ} takes whole numbers from {lim.min} to {lim.max}'
        raw = np.array([value], dtype=dt).tobytes()
        self.ser.write(struct.pack('<cHB', b'W', addr, dt.itemsize) + (raw + bytes(4))[:4])
        return None

    def start(self, chans, mode, period_us, tch=0, ttype=0, level=0, pre_pct=0, auto=True):
        """chans: list of (address, type), pre_pct: pretrigger in %. Returns an error text or None."""
        self.stop()
        if not self.ping():                 # buffer size / max. channels may have changed (reflashed)
            return 'Target does not respond'
        if len(chans) > self.max_ch:
            return f'The firmware supports at most {self.max_ch} channels (SCOPE_MAX_CH)'
        rec = sum(np.dtype(TYPES[t]).itemsize for _, t in chans)
        pre = int(self.capbuf // rec * pre_pct / 100)
        msg = struct.pack('<cBBHBBiH', b'C', len(chans), mode, period_us, tch, ttype, level, pre)
        for addr, typ in chans:
            msg += struct.pack('<HB', addr, np.dtype(TYPES[typ]).itemsize | (0x80 if typ[0] in 'sf' else 0))
        self.ser.write(msg)
        if self.ser.read(1) != bytes([ACK]):
            return 'Target rejected the configuration (too many channels, odd address or no answer)'
        self.dtype = np.dtype([(f'c{i}', TYPES[t]) for i, (_, t) in enumerate(chans)])
        self.auto = auto
        with self.lock:
            self.t, self.cols = np.zeros(0), [np.zeros(0) for _ in chans]
            self.lost = self.captures = 0
        self.ser.write(b'R')
        self.running = True
        self.thread = threading.Thread(target=self._capture if mode else self._stream, daemon=True)
        self.thread.start()
        return None

    def snapshot(self):
        with self.lock:
            return self.t, list(self.cols)

    def _read_exact(self, n):
        data = bytearray()
        while len(data) < n and self.running:
            data += self.ser.read(n - len(data))
        return bytes(data) if len(data) == n else None

    def _stream(self):
        fr = np.dtype([('sync', 'u1'), ('seq', 'u1')] + self.dtype.descr)
        F, buf, last, idx = fr.itemsize, bytearray(), None, 0
        while self.running:
            buf += self.ser.read(max(1, self.ser.in_waiting))
            if last is None:            # find two frames in a row with seq + 1
                start = next((i for i in range(len(buf) - F - 1)
                              if buf[i] == SYNC_STREAM and buf[i + F] == SYNC_STREAM
                              and buf[i + F + 1] == (buf[i + 1] + 1) & 0xFF), None)
                if start is None:
                    del buf[:max(0, len(buf) - F - 1)]
                    continue
                del buf[:start]
            a = np.frombuffer(bytes(buf[:len(buf) // F * F]), dtype=fr)
            bad = np.flatnonzero(a['sync'] != SYNC_STREAM)
            if len(bad):
                a = a[:bad[0]]
            del buf[:len(a) * F + (1 if len(bad) else 0)]
            if len(a):
                seq = a['seq'].astype(np.int64)
                prev = np.concatenate(([seq[0] - 1 if last is None else last], seq[:-1]))
                step = (seq - prev) & 0xFF
                step[step == 0] = 256
                t = idx + np.cumsum(step)
                idx, last = int(t[-1]), int(seq[-1])
                with self.lock:
                    self.lost += int((step - 1).sum())
                    self.t = np.concatenate((self.t, t))[-KEEP:]
                    self.cols = [np.concatenate((c, a[f'c{i}']))[-KEEP:] for i, c in enumerate(self.cols)]
            if len(bad):
                last = None

    def _capture(self):
        while self.running:
            b = self.ser.read(1)
            if not b or b[0] != SYNC_CAPTURE:
                continue
            hdr = self._read_exact(4)
            depth, pre = struct.unpack('<HH', hdr) if hdr else (0, 0)
            if hdr and depth != self.capbuf // self.dtype.itemsize:
                continue                # 0xC5 inside other data, not a header
            data = self._read_exact(depth * self.dtype.itemsize) if hdr else None
            if data is None:
                break
            a = np.frombuffer(data, dtype=self.dtype)
            with self.lock:
                self.t = np.arange(depth) - pre
                self.cols = [a[f] for f in self.dtype.names]
                self.captures += 1
            if self.auto and self.running:
                self.ser.write(b'R')
            else:
                self.running = False


OPEN = []      # ports to close on shutdown (MDB would otherwise keep running)


def open_port(name, baud, progress=None):
    ser = _open_port(name, baud, progress)
    OPEN.append(ser)
    return ser


def close_port(ser):
    ser.close()
    if ser in OPEN:
        OPEN.remove(ser)


def close_ports():
    for ser in list(OPEN):
        close_port(ser)


def nice_factor(x):
    """Largest 1, 2 or 5 * 10^n that is not above x."""
    e = math.floor(math.log10(x))
    m = x / 10 ** e
    return float(f'{5 if m >= 5 else 2 if m >= 2 else 1}e{e}')


def stream_load(rec, period_us, baud):
    """Bytes/s the stream needs, bytes/s the UART carries, shortest period that fits (90 %)."""
    frame = rec + 2                         # sync + seq + channels
    need = frame * 1e6 / max(1, period_us or 1)
    have = (baud or 1) / 10                 # 8N1: 10 bits per byte
    return need, have, math.ceil(frame * 1e6 / (0.9 * have))


def measure(t_ms, y):
    """Min, max, pk-pk, mean, RMS, frequency (Hz), period (ms), duty (%) of one channel; None if no data.

    Frequency, period and duty use the rising crossings through the middle between min and max, with
    10 % of pk-pk hysteresis, and need at least two of them.
    """
    y = np.asarray(y, dtype=float)
    if not len(y):
        return None
    lo, hi = float(y.min()), float(y.max())
    m = {'min': lo, 'max': hi, 'pkpk': hi - lo, 'mean': float(y.mean()), 'rms': float(np.sqrt(np.mean(y * y))),
         'freq': None, 'period': None, 'duty': None}
    mid, h = (hi + lo) / 2, 0.05 * (hi - lo)
    state = np.where(y > mid + h, 1, np.where(y < mid - h, -1, 0))
    idx = np.flatnonzero(state)                  # samples clearly above or below the middle
    s = state[idx]
    rise = idx[1:][(s[1:] == 1) & (s[:-1] == -1)]
    if hi > lo and len(rise) >= 2:
        m['period'] = float(t_ms[rise[-1]] - t_ms[rise[0]]) / (len(rise) - 1)
        m['freq'] = 1000 / m['period'] if m['period'] > 0 else None
        m['duty'] = 100 * float(np.mean(y[rise[0]:rise[-1]] > mid))
    return m


def render_png(d):
    """Chart, settings and measurements as a 1920 x 1080 PNG (16:9, a PowerPoint slide). Returns the bytes.

    d: title, note (user text below the title, may be empty), info (lines below), series [(name, x, y)], xlim,
    cursors [(axis, value, label)], columns, rows (measurement table), notes (lines below the table).
    """
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    fig = Figure(figsize=(16, 9), dpi=120)
    FigureCanvasAgg(fig)
    fig.text(0.03, 0.965, d['title'], fontsize=20, fontweight='bold', va='top')
    shift = 0.045 if d.get('note') else 0
    if shift:
        fig.text(0.03, 0.912, d['note'], fontsize=15, va='top')
    fig.text(0.03, 0.915 - shift, '\n'.join(d['info']), fontsize=11, color='#444', va='top', linespacing=1.5)
    # stacked from the bottom: notes, table, x axis label, chart up to the header
    n_rows = len(d['rows'])
    notes_h = 0.03 * len(d['notes'])
    table_y = 0.025 + notes_h
    table_h = min(0.36, 0.03 * (n_rows + 1)) if n_rows else 0
    chart_y = table_y + table_h + 0.075
    chart_top = 0.90 - shift - 0.03 * len(d['info'])
    ax = fig.add_axes([0.06, chart_y, 0.92, chart_top - chart_y])
    for name, x, y in d['series']:
        ax.plot(x, y, linewidth=1.2, label=name)
    for axis, v, label in d['cursors']:
        (ax.axvline if axis == 'x' else ax.axhline)(v, color='#d08000', linestyle='--', linewidth=1)
        if axis == 'x':
            ax.annotate(label, (v, 1), xycoords=('data', 'axes fraction'), xytext=(3, -12),
                        textcoords='offset points', color='#d08000', fontsize=10)
        else:
            ax.annotate(label, (1, v), xycoords=('axes fraction', 'data'), xytext=(-22, 3),
                        textcoords='offset points', color='#d08000', fontsize=10)
    if d['xlim']:
        ax.set_xlim(*d['xlim'])
    ax.set_xlabel('ms')
    ax.grid(True, color='#ddd')
    if d['series']:
        ax.legend(loc='upper right', fontsize=10, framealpha=0.9)
    if n_rows:
        tab_ax = fig.add_axes([0.03, table_y, 0.94, table_h])
        tab_ax.axis('off')
        tab = tab_ax.table(cellText=d['rows'], colLabels=d['columns'], bbox=[0, 0, 1, 1], cellLoc='right',
                           colLoc='right')
        tab.auto_set_font_size(False)
        tab.set_fontsize(10 if n_rows <= 8 else 8)
        for (r, c), cell in tab.get_celld().items():
            cell.set_edgecolor('#ccc')
            if r == 0:
                cell.set_facecolor('#eee')
                cell.set_text_props(fontweight='bold')
            if c == 0:
                cell.set_text_props(ha='left')
    for i, line in enumerate(d['notes']):
        fig.text(0.03, table_y - 0.03 * (i + 1) + 0.005, line, fontsize=10, color='#444')
    buf = io.BytesIO()
    fig.savefig(buf, format='png', facecolor='white')
    return buf.getvalue()


def pick_file(start_dir, title, filetypes, save=False):
    """Windows file dialog (the GUI server runs on the same PC as the browser)."""
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes('-topmost', True)       # in front of the browser
    if save:
        path = filedialog.asksaveasfilename(parent=root, title=title, initialdir=start_dir, filetypes=filetypes,
                                            defaultextension=filetypes[0][1][1:])
    else:
        path = filedialog.askopenfilename(parent=root, title=title, initialdir=start_dir, filetypes=filetypes)
    root.destroy()
    return path


def pick_elf(start_dir):
    return pick_file(start_dir, 'Select the ELF file of your project', [('ELF files', '*.elf'), ('All files', '*.*')])


def _open_port(name, baud, progress):
    if name == 'SIM':
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tests'))
        from sim_serial import SimSerial
        return SimSerial()
    if name == 'MDB':
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools'))
        from mdb_serial import MdbSerial
        return MdbSerial(ARGS.mdb, progress=progress)
    return serial.Serial(name, baud, timeout=0.1)


def main_page():
    app = {'scope': None, 'syms': {}, 'mode': 0, 'progress': None, 'cur': [None, None], 'cfg': CONFIG}
    chans = [{'name': '', 'addr': '', 'type': 's16', 'factor': 1}]
    new_write = lambda: {'name': '', 'addr': '', 'type': 's16', 'value': None}    # noqa: E731
    writes = [new_write()]
    ports = ([p.device for p in serial.tools.list_ports.comports()]
             + (['SIM'] if ARGS.sim else []) + (['MDB'] if ARGS.mdb else []))

    with ui.row().classes('items-center'):
        ui.label('mini-scope').classes('text-2xl font-bold mr-4')
        ui.button('Save', on_click=lambda: save(CONFIG)).props('outline').tooltip(f'Save the settings to {CONFIG}, '
                                                                                  'loaded at every start')
        ui.button('Save as ...', on_click=lambda: save_as()).props('outline')
        ui.button('Load from ...', on_click=lambda: load_from()).props('outline')

    # ---- connection and ELF ----
    with ui.row().classes('items-center'):
        port = ui.select(ports, value=ports[-1] if ARGS.sim or ARGS.mdb else (ports[0] if ports else None),
                         label='Port').classes('w-40')
        baud = ui.number('Baud', value=115200, format='%d').classes('w-32')
        status = ui.label('not connected')

        def set_connected(on):
            conn_btn.text = 'Disconnect' if on else 'Connect'
            port.set_enabled(not on)
            baud.set_enabled(not on)
            conn_btn.enable()

        async def connect():
            conn_btn.disable()
            status.text = 'connecting ...'
            if port.value == 'MDB':
                app['progress'] = (0.0, 'Starting simulator ...')
            try:
                sc = Scope(await run.io_bound(open_port, port.value, int(baud.value),
                                              lambda f, t: app.update(progress=(f, t))))
                app['scope'] = sc
                ok = await run.io_bound(sc.ping)
                status.text = 'connected, target responds' if ok else 'port open, but no answer from target'
            except Exception as e:      # noqa: BLE001 - show any port error in the GUI
                status.text = f'Error: {e}'
            app['progress'] = None
            set_connected(app['scope'] is not None)

        async def disconnect():
            sc, app['scope'] = app['scope'], None
            conn_btn.disable()
            status.text = 'disconnecting ...'
            try:
                await run.io_bound(sc.stop)
            except Exception:           # noqa: BLE001 - the port may already be gone
                pass
            await run.io_bound(close_port, sc.ser)
            status.text = 'not connected'
            set_connected(False)

        async def connect_or_disconnect():
            await (disconnect() if app['scope'] else connect())

        conn_btn = ui.button('Connect', on_click=connect_or_disconnect)

    with ui.column().classes('w-[40rem] gap-1') as progress_box:     # simulator start-up
        progress_text = ui.label()
        progress_bar = ui.linear_progress(value=0, show_value=False, size='10px')
    progress_box.visible = False

    with ui.row().classes('items-center w-full'):
        elf = ui.input(r'ELF file (<project>.X\dist\default\production\*.elf)',
                       value=ARGS.mdb or '').classes('w-[40rem]')
        elf_info = ui.label()

        def load_elf():
            try:
                path = elf.value.strip().strip('"')
                app['syms'] = load_symbols(path) if path or not ARGS.sim else sim_symbols()
                elf_info.text = f"{len(app['syms'])} variables"
                for row in chans + writes:  # addresses move with every rebuild: follow the names
                    if row['name'] in app['syms']:
                        row['addr'] = f"0x{app['syms'][row['name']][0]:04X}"
                channel_list.refresh()
                write_list.refresh()
            except Exception as e:      # noqa: BLE001
                elf_info.text = f'Error: {e}'

        async def browse():
            current = elf.value.strip().strip('"')
            start = os.path.dirname(current) if current and os.path.isdir(os.path.dirname(current)) else os.getcwd()
            try:
                path = await run.io_bound(pick_elf, start)
            except Exception as e:      # noqa: BLE001 - e.g. no tkinter in this Python
                ui.notify(f'File dialog not available: {e}', type='warning')
                return
            if path:
                elf.value = os.path.normpath(path)
                load_elf()

        ui.button('Browse ...', on_click=browse).props('outline')
        ui.button('Load', on_click=load_elf)

    # ---- channels ----
    @ui.refreshable
    def channel_list():
        for ch in chans:
            with ui.row().classes('items-center'):
                def pick(e, ch=ch):
                    if e.value in app['syms']:
                        a, size = app['syms'][e.value]
                        ch.update(name=e.value, addr=f'0x{a:04X}', type=DEFAULT_TYPE[size])
                        channel_list.refresh()
                if app['syms']:         # dropdown with filter: name (address, size)
                    ui.select({n: f'{n}  (0x{a:04X}, {s} B)' for n, (a, s) in app['syms'].items()},
                              value=ch['name'] if ch['name'] in app['syms'] else None,
                              label='Variable', with_input=True, on_change=pick).classes('w-72')
                else:
                    ui.input('Variable', value=ch['name']).bind_value(ch, 'name').classes('w-56')
                ui.input('Address', value=ch['addr']).bind_value(ch, 'addr').classes('w-28')
                ui.select(list(TYPES), value=ch['type']).bind_value(ch, 'type').classes('w-24')
                ui.number('Factor', value=ch['factor']).bind_value(ch, 'factor').classes('w-24')
                ui.button(icon='delete', on_click=lambda ch=ch: (chans.remove(ch), channel_list.refresh())) \
                    .props('flat dense')
        ui.button('+ Channel', on_click=lambda: (chans.append({'name': '', 'addr': '', 'type': 's16', 'factor': 1}),
                                               channel_list.refresh())).props('flat')

    # ---- write variables: same rows as the channels, plus a value ----
    async def write(w):
        if not app['scope']:
            ui.notify('Connect first', type='warning')
            return
        try:
            addr = int(w['addr'], 0)
        except ValueError:
            ui.notify('Enter an address such as 0x1234', type='warning')
            return
        if w['value'] is None:
            return
        err = await run.io_bound(app['scope'].write, addr, w['type'], w['value'])
        ui.notify(err or f"{w['name'] or w['addr']} = {w['value']:g}", type='negative' if err else 'positive')

    @ui.refreshable
    def write_list():
        for w in writes:
            with ui.row().classes('items-center'):
                def pick(e, w=w):
                    if e.value in app['syms']:
                        a, size = app['syms'][e.value]
                        w.update(name=e.value, addr=f'0x{a:04X}', type=DEFAULT_TYPE[size])
                        write_list.refresh()
                if app['syms']:
                    ui.select({n: f'{n}  (0x{a:04X}, {s} B)' for n, (a, s) in app['syms'].items()},
                              value=w['name'] if w['name'] in app['syms'] else None,
                              label='Variable', with_input=True, on_change=pick).classes('w-72')
                else:
                    ui.input('Variable', value=w['name']).bind_value(w, 'name').classes('w-56')
                ui.input('Address', value=w['addr']).bind_value(w, 'addr').classes('w-28')
                ui.select(list(TYPES), value=w['type']).bind_value(w, 'type').classes('w-24')
                ui.number('Value', value=w['value']).bind_value(w, 'value').classes('w-32') \
                    .on('keydown.enter', lambda w=w: write(w))
                ui.button(icon='send', on_click=lambda w=w: write(w)).props('flat dense').tooltip('Write')
                ui.button(icon='delete', on_click=lambda w=w: (writes.remove(w), write_list.refresh())) \
                    .props('flat dense')
        ui.button('+ Variable', on_click=lambda: (writes.append(new_write()), write_list.refresh())).props('flat')

    with ui.row().classes('w-full items-stretch'):
        with ui.card().classes('grow'):
            ui.label('Channels').classes('font-bold')
            channel_list()
        with ui.card().classes('grow'):
            ui.label('Write variable').classes('font-bold')
            write_list()

    # ---- settings ----
    with ui.card().classes('w-full'):
        with ui.row().classes('items-center'):
            mode = ui.toggle({0: 'Stream', 1: 'Capture'}, value=0)
            period = ui.number('Sample period µs', value=1000, min=1, max=65535, format='%d').classes('w-36')
            window = ui.number('Window (samples)', value=2000, min=10, format='%d').classes('w-36') \
                .bind_visibility_from(mode, 'value', value=0)
            rate = ui.label()
        with ui.row().classes('items-center').bind_visibility_from(mode, 'value', value=1):
            tch = ui.number('Trigger channel (0..)', value=0, min=0, format='%d').classes('w-36')
            ttype = ui.select(TRIGGERS, value=1, label='Edge').classes('w-36')
            level = ui.number('Threshold (raw)', value=0, format='%d').classes('w-32')
            pre = ui.number('Pretrigger %', value=20, min=0, max=100, format='%d').classes('w-32')
            auto = ui.checkbox('Auto re-arm', value=True)

    def selected():
        out = []
        for ch in chans:
            if ch['addr']:
                out.append((int(ch['addr'], 0), ch['type'], ch['name'] or ch['addr'], float(ch['factor'] or 1)))
        return out

    async def start():
        sc, sel = app['scope'], selected()
        if not sc or not sel:
            ui.notify('Connect first and enter at least one channel with an address', type='warning')
            return
        trig = int(tch.value)
        if mode.value and (trig >= len(sel) or sel[trig][1] == 'f32'):
            ui.notify('Trigger channel missing or f32', type='warning')
            return
        rec = sum(np.dtype(TYPES[s[1]]).itemsize for s in sel)
        need, have, min_us = stream_load(rec, period.value, baud.value)
        if mode.value == 0 and need >= 0.9 * have:
            ui.notify(f'Sample period too short for streaming: samples will be lost. '
                      f'Use at least {min_us} µs or Capture.', type='warning', timeout=8000)
        app['active'] = [ch for ch in chans if ch['addr']]      # rows being measured
        app['names'] = [s[2] for s in sel]
        app['mode'], app['period'] = int(mode.value), int(period.value)
        err = await run.io_bound(sc.start, [s[:2] for s in sel], int(mode.value), int(period.value),
                                 int(tch.value), int(ttype.value), int(level.value),
                                 float(pre.value or 0), auto.value)
        if err:
            ui.notify(err, type='negative')

    async def stop():
        if app['scope']:
            await run.io_bound(app['scope'].stop)

    def factors():
        """Current factors of the channels being measured (edits apply at once)."""
        return [float(ch['factor'] or 1) for ch in app['active']]

    def shown():
        """Raw data currently shown: the stream window or the last capture."""
        t, cols = app['scope'].snapshot()
        if app['mode'] == 0 and len(t):
            keep = t > t[-1] - int(window.value or 2000)
            t, cols = t[keep], [c[keep] for c in cols]
        return t, cols

    def autoscale():
        if not app['scope'] or 'active' not in app:
            ui.notify('Start a measurement first', type='warning')
            return
        t, cols = shown()
        peaks = [float(np.max(np.abs(c.astype(float)))) if len(c) else 0.0 for c in cols]
        ref = max(peaks, default=0.0)
        if ref == 0:
            ui.notify('No data yet', type='warning')
            return
        for ch, pk in zip(app['active'], peaks):
            if pk > 0:                  # the largest channel keeps 1, the others are scaled up to it
                ch['factor'] = nice_factor(ref / pk)
        channel_list.refresh()
    async def reset():
        if not app['scope']:
            ui.notify('Connect first', type='warning')
            return
        ui.notify('Resetting the target ...')
        err = await run.io_bound(app['scope'].reset)
        ui.notify(err or 'Target restarted', type='negative' if err else 'positive')

    async def png():
        if not app['scope'] or 'active' not in app:
            ui.notify('Start a measurement first', type='warning')
            return
        t, cols = shown()
        fs = factors()
        x = t * app['period'] / 1000
        step = max(1, math.ceil(len(t) / 20000))
        series = [(n if f == 1 else f'{n}  (x {f:g})', x[::step], c[::step] * f)
                  for n, c, f in zip(app['names'], cols, fs)]
        xlim = None
        try:                                # the zoomed part of the chart, as on the screen
            z = await ui.run_javascript(PNG_ZOOM_JS % chart.id)
            if len(x) and z:
                lo, hi = float(x.min()), float(x.max())
                a = z[0] if isinstance(z[0], (int, float)) else lo + (hi - lo) * (z[2] or 0) / 100
                b = z[1] if isinstance(z[1], (int, float)) else lo + (hi - lo) * (z[3] if z[3] is not None else 100) / 100
                xlim = (a, b)
        except Exception:                   # noqa: BLE001 - without the zoom state the whole chart is shown
            pass
        sc, m = app['scope'], app['mode']
        info = [f"{'Stream' if m == 0 else 'Capture'}, sample period {app['period']} µs, {len(t)} samples"
                + (f', lost frames {sc.lost}' if m == 0 else
                   f", trigger channel {int(tch.value)} {TRIGGERS[ttype.value]} through {level.value:g} (raw), "
                   f"pretrigger {pre.value:g} %")]
        info.append(f"Port {port.value}, {int(baud.value)} baud" + (f",  ELF {os.path.basename(elf.value.strip())}"
                                                                      if elf.value.strip() else ''))
        label, rows = app.get('meas', ('', []))
        keys = [c['name'] for c in meas_cols if cur_mode.value == 'time' or c['name'] not in ('c1', 'c2', 'delta')]
        d = {'title': f"mini-scope  {time.strftime('%Y-%m-%d %H:%M:%S')}", 'note': (note.value or '').strip(),
             'info': info, 'series': series,
             'xlim': xlim,
             'cursors': [('x' if cur_mode.value == 'time' else 'y', v, f'C{i + 1}')
                         for i, v in enumerate(app['cur']) if v is not None and cur_mode.value != 'off'],
             'columns': [c['label'] for c in meas_cols if c['name'] in keys],
             'rows': [[r.get(k, '') for k in keys] for r in rows],
             'notes': [n for n in (label, cur_info.text) if n]}
        store(await run.io_bound(render_png, d), 'png')

    def store(data, ext):
        """Saves next to the settings file in use, with a time stamp in the name."""
        path = os.path.join(os.path.dirname(app['cfg']), f"mini-scope_{time.strftime('%Y%m%d_%H%M%S')}.{ext}")
        try:
            with open(path, 'wb') as f:
                f.write(data)
            ui.notify(f'Saved {path}', type='positive')
        except OSError as e:
            ui.notify(f'Could not save: {e}', type='negative')

    def csv():
        if not app['scope'] or 'active' not in app:
            return
        t, cols = app['scope'].snapshot()
        out = io.StringIO()
        out.write('t_ms,' + ','.join(app['names']) + '\n')
        for row in zip(t * app['period'] / 1000, *(c * f for c, f in zip(cols, factors()))):
            out.write(','.join(f'{v:g}' for v in row) + '\n')
        store(out.getvalue().encode(), 'csv')

    with ui.row().classes('items-center'):
        ui.button('Start', on_click=start, color='positive')
        ui.button('Stop', on_click=stop, color='negative')
        ui.button('Autoscale', on_click=autoscale).props('outline')
        ui.button('Reset', on_click=reset).props('outline')
        ui.button('CSV', on_click=csv).props('outline')
        ui.button('PNG', on_click=lambda: png()).props('outline').tooltip('Chart, settings and measurements as a '
                                                                          '16:9 picture for a slide')
        note = ui.input('Note for the PNG').classes('w-96').tooltip('Goes into the header of the PNG')

    chart = ui.echart({
        'backgroundColor': 'transparent',
        'animation': False,
        'tooltip': {'trigger': 'axis'},
        'legend': {'top': 0},
        'grid': {'left': 70, 'right': 20, 'top': 40, 'bottom': 70},
        'xAxis': {'type': 'value', 'name': 'ms', 'scale': True, 'splitLine': {'lineStyle': {'color': '#333'}}},
        'yAxis': {'type': 'value', 'scale': True, 'splitLine': {'lineStyle': {'color': '#333'}}},
        'dataZoom': [{'type': 'inside'}, {'type': 'slider'}],
        'series': [],
    }, theme='dark').classes('w-full h-[32rem]')

    # ---- cursors: a click in the chart sets C1, then C2, then moves the nearer one ----
    def chart_click(e):
        if cur_mode.value == 'off':
            return
        v = float(e.args['x' if cur_mode.value == 'time' else 'y'])
        c = app['cur']
        if c[0] is None:
            c[0] = v
        elif c[1] is None:
            c[1] = v
        else:
            c[0 if abs(v - c[0]) <= abs(v - c[1]) else 1] = v

    ui.on('chart_click', chart_click)
    ui.timer(0.5, lambda: ui.run_javascript(CHART_CLICK_JS % chart.id), once=True)

    with ui.row().classes('items-center'):
        cur_mode = ui.toggle({'off': 'Cursors off', 'time': 'Time', 'value': 'Value'}, value='off',
                             on_change=lambda: app.update(cur=[None, None]))
        ui.button('Clear', on_click=lambda: app.update(cur=[None, None])).props('flat')
        cur_info = ui.label()

    meas_cols = [{'name': k, 'label': lab, 'field': k, 'align': 'left' if k == 'ch' else 'right'}
                 for k, lab in (('ch', 'Channel'), ('min', 'Min'), ('max', 'Max'), ('pkpk', 'Pk-Pk'),
                                ('mean', 'Mean'), ('rms', 'RMS'), ('freq', 'Freq Hz'), ('period', 'Period ms'),
                                ('duty', 'Duty %'), ('c1', '@C1'), ('c2', '@C2'), ('delta', 'Δ'))]
    meas_label = ui.label().classes('text-sm text-gray-400')
    meas = ui.table(columns=meas_cols, rows=[], row_key='ch').props('dense flat').classes('w-full')

    def fmt(v):
        return '' if v is None else f'{v:.0f}' if abs(v) >= 1000 else f'{v:.4g}'

    def update_measurements(x, cols, fs):
        c = app['cur']
        both = cur_mode.value == 'time' and None not in c
        rng = (x >= min(c)) & (x <= max(c)) if both else np.ones(len(x), bool)
        meas_label.text = ('Measurements between the cursors' if both else 'Measurements over the chart') \
            + f', {int(rng.sum())} samples'
        rows = []
        for n, col, f in zip(app['names'], cols, fs):
            y = col.astype(float) * f
            m = measure(x[rng], y[rng]) or {}
            row = {'ch': n, **{k: fmt(m.get(k)) for k in ('min', 'max', 'pkpk', 'mean', 'rms', 'freq', 'period',
                                                           'duty')}}
            if cur_mode.value == 'time' and len(x):
                at = [None if t is None else float(y[np.argmin(np.abs(x - t))]) for t in c]
                row.update(c1=fmt(at[0]), c2=fmt(at[1]), delta=fmt(at[1] - at[0]) if None not in at else '')
            rows.append(row)
        meas.rows = rows
        meas.update()
        app['meas'] = (meas_label.text, rows)
        if cur_mode.value == 'off':
            cur_info.text = ''
        elif c[0] is None:
            cur_info.text = 'click into the chart: cursor 1, then cursor 2, then the nearer one moves'
        elif cur_mode.value == 'time':
            cur_info.text = f't1 = {fmt(c[0])} ms' + (
                f',  t2 = {fmt(c[1])} ms,  Δt = {fmt(c[1] - c[0])} ms,  1/Δt = {fmt(1000 / abs(c[1] - c[0]))} Hz'
                if c[1] is not None and c[1] != c[0] else '')
        else:
            cur_info.text = f'y1 = {fmt(c[0])}' + (f',  y2 = {fmt(c[1])},  Δy = {fmt(c[1] - c[0])}'
                                                   if c[1] is not None else '')

    def refresh():
        progress_box.visible = app['progress'] is not None
        if app['progress']:
            progress_bar.value, progress_text.text = app['progress']
        sel = selected()
        rec = sum(np.dtype(TYPES[s[1]]).itemsize for s in sel)
        if rec:
            need, have, min_us = stream_load(rec, period.value, baud.value)
            depth = f' | capture depth {app["scope"].capbuf // rec} samples' if app['scope'] else ''
            if mode.value == 0 and need >= 0.9 * have:
                rate.text = (f'⚠ Too fast for the UART: the stream needs {need / 1000:.1f} kB/s, the UART '
                             f'carries {have / 1000:.1f} kB/s. Samples will be lost. Use a sample period '
                             f'of at least {min_us} µs, fewer channels, or Capture.')
                rate.classes(replace='text-orange-400 font-bold')
            else:
                rate.text = (f'Stream needs {need / 1000:.1f} kB/s, UART carries {have / 1000:.1f} kB/s{depth}'
                             if mode.value == 0 else depth.lstrip(' |'))
                rate.classes(replace='')
        sc = app['scope']
        if not sc or 'active' not in app:
            return
        t, cols = shown()
        step = max(1, math.ceil(len(t) / MAX_POINTS))
        x = t[::step] * app['period'] / 1000
        fs = factors()
        chart.options['series'] = [
            {'name': n, 'type': 'line', 'showSymbol': False,
             'data': np.column_stack((x, c[::step] * f)).tolist()}
            for n, c, f in zip(app['names'], cols, fs)]
        marks = [{'xAxis' if cur_mode.value == 'time' else 'yAxis': v, 'label': {'formatter': f'C{i + 1}'}}
                 for i, v in enumerate(app['cur']) if v is not None and cur_mode.value != 'off']
        if chart.options['series'] and marks:
            chart.options['series'][0]['markLine'] = {
                'symbol': 'none', 'silent': True, 'animation': False, 'data': marks,
                'lineStyle': {'color': '#ffcc00', 'type': 'dashed'}, 'label': {'color': '#ffcc00'}}
        chart.update()
        update_measurements(t * app['period'] / 1000, cols, fs)
        status.text = (('running' if sc.running else 'stopped')
                       + (f' | lost frames: {sc.lost}' if app['mode'] == 0 else f' | captures: {sc.captures}'))

    ui.timer(0.25, refresh)

    # ---- settings file: Save writes mini-scope.json, which is loaded at start ----
    def settings():
        return {'port': port.value, 'baud': baud.value, 'elf': elf.value, 'channels': chans, 'writes': writes,
                'mode': mode.value, 'period': period.value, 'window': window.value, 'note': note.value,
                'trigger': {'channel': tch.value, 'edge': ttype.value, 'threshold': level.value,
                            'pretrigger': pre.value, 'auto_rearm': auto.value}}

    def apply(cfg):
        if not app['scope']:                    # port and baud are locked while connected
            if cfg.get('port') in port.options:
                port.value = cfg['port']
            baud.value = cfg.get('baud', baud.value)
        if not (ARGS.sim or ARGS.mdb):          # these modes bring their own ELF
            elf.value = cfg.get('elf', elf.value)
        chans[:] = [{'name': '', 'addr': '', 'type': 's16', 'factor': 1, **c} for c in cfg.get('channels', chans)]
        writes[:] = [{**new_write(), **w} for w in cfg.get('writes', writes)]
        mode.value = cfg.get('mode', mode.value)
        note.value = cfg.get('note', note.value)
        period.value = cfg.get('period', period.value)
        window.value = cfg.get('window', window.value)
        trig = cfg.get('trigger', {})
        tch.value = trig.get('channel', tch.value)
        ttype.value = trig.get('edge', ttype.value)
        level.value = trig.get('threshold', level.value)
        pre.value = trig.get('pretrigger', pre.value)
        auto.value = trig.get('auto_rearm', auto.value)
        path = elf.value.strip().strip('"')
        if ARGS.sim or os.path.exists(path):
            load_elf()                          # variable lists for the channels
        channel_list.refresh()
        write_list.refresh()

    def save(path):
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(settings(), f, indent=2)
            app['cfg'] = path                   # CSV and PNG go next to it
            ui.notify(f'Settings saved to {path}', type='positive')
        except OSError as e:
            ui.notify(f'Could not save: {e}', type='negative')

    def load(path, quiet=False):
        try:
            with open(path, encoding='utf-8') as f:
                apply(json.load(f))
            app['cfg'] = path
            if not quiet:
                ui.notify(f'Settings loaded from {path}', type='positive')
        except (OSError, ValueError, TypeError, AttributeError) as e:
            ui.notify(f'Could not load {path}: {e}', type='negative')

    async def save_as():
        try:
            path = await run.io_bound(pick_file, os.path.dirname(CONFIG), 'Save the settings as',
                                      [('JSON files', '*.json')], True)
        except Exception as e:      # noqa: BLE001 - e.g. no tkinter in this Python
            ui.notify(f'File dialog not available: {e}', type='warning')
            return
        if path:
            save(os.path.normpath(path))

    async def load_from():
        try:
            path = await run.io_bound(pick_file, os.path.dirname(CONFIG), 'Load settings',
                                      [('JSON files', '*.json'), ('All files', '*.*')])
        except Exception as e:      # noqa: BLE001
            ui.notify(f'File dialog not available: {e}', type='warning')
            return
        if path:
            load(os.path.normpath(path))

    if os.path.exists(CONFIG):
        load(CONFIG, quiet=True)


def sim_symbols():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tests'))
    from sim_serial import SYMBOLS
    return SYMBOLS


if __name__ in {'__main__', '__mp_main__'}:
    p = argparse.ArgumentParser()
    p.add_argument('--sim', action='store_true', help='no hardware: PC model of scope.c (tests/sim.dll)')
    p.add_argument('--mdb', metavar='ELF', help='no hardware: firmware in the MPLAB X simulator')
    p.add_argument('--port', type=int, default=8080, help='HTTP-Port')
    p.add_argument('--no-browser', action='store_true', help='do not open the browser')
    ARGS = p.parse_args()
    nicegui_app.on_shutdown(close_ports)
    ui.run(main_page, title='mini-scope', port=ARGS.port, reload=False, show=not ARGS.no_browser,
           dark=True)
