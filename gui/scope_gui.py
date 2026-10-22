"""scope_gui.py - PC side of mini-scope (NiceGUI in the browser).

    python scope_gui.py          pick the COM port in the browser
    python scope_gui.py --sim    no hardware: runs firmware/scope.c as tests/sim.dll
    python scope_gui.py --mdb test.X/dist/sim/production/test.X.production.elf
                                 no hardware: real firmware in the MPLAB X simulator (slow)

Protocol: see the comment block at the top of firmware/scope.c.
"""
import argparse
import io
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


def pick_elf(start_dir):
    """Windows file dialog (the GUI server runs on the same PC as the browser)."""
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes('-topmost', True)       # in front of the browser
    path = filedialog.askopenfilename(parent=root, title='Select the ELF file of your project',
                                      initialdir=start_dir,
                                      filetypes=[('ELF files', '*.elf'), ('All files', '*.*')])
    root.destroy()
    return path


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
    app = {'scope': None, 'syms': {}, 'mode': 0, 'progress': None}
    chans = [{'name': '', 'addr': '', 'type': 's16', 'factor': 1}]
    ports = ([p.device for p in serial.tools.list_ports.comports()]
             + (['SIM'] if ARGS.sim else []) + (['MDB'] if ARGS.mdb else []))

    ui.label('mini-scope').classes('text-2xl font-bold')

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
                channel_list.refresh()
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

    with ui.card().classes('w-full'):
        ui.label('Channels').classes('font-bold')
        channel_list()

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

    def csv():
        if not app['scope'] or 'active' not in app:
            return
        t, cols = app['scope'].snapshot()
        out = io.StringIO()
        out.write('t_ms,' + ','.join(app['names']) + '\n')
        for row in zip(t * app['period'] / 1000, *(c * f for c, f in zip(cols, factors()))):
            out.write(','.join(f'{v:g}' for v in row) + '\n')
        ui.download.content(out.getvalue().encode(), 'scope.csv')

    with ui.row():
        ui.button('Start', on_click=start, color='positive')
        ui.button('Stop', on_click=stop, color='negative')
        ui.button('Autoscale', on_click=autoscale).props('outline')
        ui.button('CSV', on_click=csv).props('outline')

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
        chart.options['series'] = [
            {'name': n, 'type': 'line', 'showSymbol': False,
             'data': np.column_stack((x, c[::step] * f)).tolist()}
            for n, c, f in zip(app['names'], cols, factors())]
        chart.update()
        status.text = (('running' if sc.running else 'stopped')
                       + (f' | lost frames: {sc.lost}' if app['mode'] == 0 else f' | captures: {sc.captures}'))

    ui.timer(0.25, refresh)


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
