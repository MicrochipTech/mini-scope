"""m7a_sim.py - simulator checks of card M7a. One MDB session at a time.

    python docs/evidence/m7a/m7a_sim.py rx        ping, W and a stream: UxRX_ISR against the mailbox
    python docs/evidence/m7a/m7a_sim.py overrun   RX FIFO overrun, then recovery
    python docs/evidence/m7a/m7a_sim.py tx        TX path with the kicks taken out

Firmware comes from a scratch copy in the temp folder, see build(). The worktree is not touched.
"""
import os
import shutil
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'm0'))
from m0_common import GNU, MPLABX, REPO, config_stream, patch, sh, symbols, write_cmd  # noqa: E402
from m0_rx import SCOPE_EDITS, Session, scl                     # noqa: E402

BUILD = os.path.join(tempfile.gettempdir(), 'mini_scope_m7a')
PROBE = [('volatile uint16_t ramp_step = 1;', 'volatile uint16_t probe;\nvolatile uint16_t ramp_step = 1;')]
RX_LATE = PROBE + [('        i++;\n', '        i++;\n        if (probe == 1) {\n            probe = 0;\n'
                                     '            _U1RXIE = 1;\n        }\n')]
TX_KICK = ('static void tx_kick(void)\n{\n    UxTXIF = 1;\n    UxTXIE = 1;\n}',
           'static void tx_kick(void)\n{\n    UxTXIE = 1;\n}')
NO_10K = ('    TUNE_TXIF = 1;\n', '')
# what UTXISEL = 7 should do on a device: raise the flag only while the FIFO has a free slot
FLAG = ('    TUNE_TXIF = 1;\n', '    if (!U1STAHbits.UTXBF)\n        TUNE_TXIF = 1;\n')


def build(name, main_edits=(), scope_edits=()):
    d = os.path.join(BUILD, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    shutil.copytree(os.path.join(REPO, 'firmware'), os.path.join(d, 'firmware'))
    shutil.copytree(os.path.join(REPO, 'test.X'), os.path.join(d, 'test.X'),
                    ignore=shutil.ignore_patterns('build', 'dist', 'debug', '.generated_files', 'Makefile-*'))
    patch(os.path.join(d, 'firmware', 'example_main.c'), main_edits)
    patch(os.path.join(d, 'firmware', 'scope.c'), scope_edits)
    x = os.path.join(d, 'test.X')
    sh([MPLABX + r'\mplab_platform\bin\prjMakefilesGenerator.bat', '.'], x)
    sh([GNU + r'\make.exe', '-f', 'Makefile', 'CONF=sim', 'build'], x)
    return os.path.join(x, 'dist', 'sim', 'production', 'test.X.production.elf')


def hex_bytes(data):
    return ' '.join(f'{x:02X}' for x in data)


def packets(steps):
    """SCL body: (wait in us, bytes) per step."""
    body = '    variable s : line;\n  begin\n'
    for wait_us, data in steps:
        body += f'    wait for {wait_us} us;\n    s := "{hex_bytes(data)}";\n    packetin(s, U1RXREG, true);\n'
    return body + '    wait;'


def session(name, elf, steps):
    """Fresh MDB session; steps is the receive path stimulus or None. Returns (session, UART output file)."""
    d = tempfile.mkdtemp(dir=BUILD, prefix=name)
    out = os.path.join(d, 'uart.bin')
    stim = None
    if steps:
        stim = os.path.join(d, 'stim.scl').replace('\\', '/')
        with open(stim, 'w') as f:
            f.write(scl(packets(steps)))
    return Session(elf, out, [], stim), out


def mail(s, sym, data):
    """Mailbox write as in tools/mdb_serial.py."""
    for k, b in enumerate(data):
        s.cmd(f"write {sym['sim_rx'] + 2 * k} {b}")
    s.cmd(f"write {sym['sim_rx_len']} {len(data)}")
    time.sleep(1)


def size(path):
    return os.path.getsize(path) if os.path.exists(path) else 0


def data(path):
    return open(path, 'rb').read() if os.path.exists(path) else b''


def frames(buf, n):
    """Stream frames A5 seq n bytes, returns (frames, lost by seq, junk bytes)."""
    i = good = lost = junk = 0
    last = None
    while i + 2 + n <= len(buf):
        if buf[i] != 0xA5:
            i += 1
            junk += 1
            continue
        if last is not None:
            lost += (buf[i + 1] - last - 1) & 255
        last = buf[i + 1]
        good += 1
        i += 2 + n
    return good, lost, junk


def show_regs(s, names):
    return {n: s.show(n) for n in names}


CFG = ('rx_hits', 'rx_bytes', 'ramp_step', 'U1STA', 'U1STAH', 'U1BRG', 'CLKDIV', 'PLLFBD', 'PLLDIV')


def stream_config(sym):
    return config_stream([sym['ramp_step']], 1000)


def rx():
    elf = build('rx', scope_edits=SCOPE_EDITS)
    sym = symbols(elf)
    w = write_cmd(sym['ramp_step'], 7)
    assert len(w) == 8
    ping = b'P\0'                                   # second byte: answer NAK, and an even count for the simulator
    # 1. ping, W, ping through the real receive path
    print('--- UxRX_ISR', flush=True)
    s, out = session('rx1', elf, [(100, ping), (2000, w), (2000, ping)])
    time.sleep(10)
    print(show_regs(s, CFG))
    s.close()
    time.sleep(1)
    a = data(out)
    print(f'UART {len(a)} bytes: {hex_bytes(a)}\nMDB said: {s.log[:4]}', flush=True)
    # 2. the same bytes through the mailbox
    print('--- mailbox', flush=True)
    s, out = session('mb1', elf, None)
    time.sleep(3)
    mail(s, sym, b'S')                              # first mailbox write after Run may get lost
    mail(s, sym, b'S')
    time.sleep(4)
    skip = size(out)
    for x in (ping, w, ping):
        mail(s, sym, x)
    time.sleep(8)
    print(show_regs(s, CFG))
    s.close()
    time.sleep(1)
    b = data(out)[skip:]
    print(f'UART {len(b)} bytes after {skip} from S: {hex_bytes(b)}')
    print(f'identical: {a == b}', flush=True)
    # 3. stream with a W in the middle and S at the end, through the receive path
    print('--- UxRX_ISR stream', flush=True)
    s, out = session('rx2', elf, [(100, stream_config(sym) + b'R\0'), (100000, write_cmd(sym['ramp_step'], 5)),
                                  (100000, b'S\0')])
    time.sleep(25)
    print(show_regs(s, ('rx_hits', 'rx_bytes', 'ramp_step', 'scope_overflows')))
    s.close()
    time.sleep(1)
    c = data(out)
    print(f'UART {len(c)} bytes, first {hex_bytes(c[:2])}, last {hex_bytes(c[-2:])}')
    body = c[2:-2]
    print(f'frames, lost, junk: {frames(body, 2)}')
    vals = [struct.unpack('<H', body[i + 2:i + 4])[0] for i in range(0, len(body) - 3, 4)]
    print(f'ramp_step in the frames: {sorted(set(vals))}, first change at sample {vals.index(5) if 5 in vals else None}',
          flush=True)


def overrun():
    # the RX interrupt stays off until probe = 1, so the FIFO fills
    elf = build('overrun', main_edits=RX_LATE, scope_edits=SCOPE_EDITS + [('    UxRXIE = 1;\n', '    UxRXIE = 0;\n')])
    sym = symbols(elf)
    s, out = session('ov', elf, [(100, b'P\0' * 6), (1500000, b'P\0')])
    time.sleep(6)
    print('12 bytes in, RX interrupt off:', show_regs(s, ('rx_hits', 'rx_bytes', 'U1STA', 'U1STAH')), flush=True)
    for _ in range(3):
        s.cmd(f"write {sym['probe']} 1")
        time.sleep(1)
    time.sleep(6)
    print('RX interrupt on:', show_regs(s, ('rx_hits', 'rx_bytes', 'U1STA', 'U1STAH', 'probe')), flush=True)
    t = size(out)
    print(f'UART {t} bytes: {hex_bytes(data(out)[:48])}', flush=True)
    time.sleep(25)
    print('after 2 more bytes:', show_regs(s, ('rx_hits', 'rx_bytes', 'U1STA', 'U1STAH')), flush=True)
    s.close()
    time.sleep(1)
    print(f'UART {size(out)} bytes (+{size(out) - t}): {hex_bytes(data(out)[t:t + 24])}', flush=True)


def capture_config(sym):
    return bytes([ord('C'), 1, 1]) + struct.pack('<H', 100) + bytes([0, 0]) + struct.pack('<iH', 0, 0) \
        + struct.pack('<HB', sym['ramp'], 2)


def tx_case(name, main_edits, scope_edits):
    print(f'--- {name}', flush=True)
    elf = build(name, main_edits=main_edits, scope_edits=scope_edits)
    sym = symbols(elf)
    s, out = session(name, elf, None)
    time.sleep(3)
    mail(s, sym, b'S')
    mail(s, sym, b'S')
    time.sleep(6)
    n0 = size(out)
    mail(s, sym, b'P')
    time.sleep(8)
    n1 = size(out)
    print(f'answers to S: {n0} bytes, ping: {n1 - n0} bytes {hex_bytes(data(out)[n0:n1][:10])}', flush=True)
    mail(s, sym, config_stream([sym['ramp']], 1000))
    mail(s, sym, b'R')
    time.sleep(40)
    mail(s, sym, b'S')
    time.sleep(8)
    n2 = size(out)
    body = data(out)[n1:n2]
    print(f'stream: {n2 - n1} bytes, frames/lost/junk {frames(body[2:], 2)}, overflows {s.show("scope_overflows")}',
          f'U1STAH {s.show("U1STAH")}, head {s.show("head")}, tail {s.show("tail")}', flush=True)
    mail(s, sym, capture_config(sym))
    mail(s, sym, b'R')
    time.sleep(25)
    n3 = size(out)
    cap = data(out)[n2:n3]
    i = cap.find(b'\xC5')
    print(f'capture: {n3 - n2} bytes, header {hex_bytes(cap[i:i + 5]) if i >= 0 else None}, '
          f'payload {n3 - n2 - i - 5 if i >= 0 else 0} of 4096', f'dump_left {s.show("dump_left")}', flush=True)
    s.close()
    time.sleep(1)


def tx():
    tx_case('tx_base', (), ())
    tx_case('tx_nokick', [NO_10K], [TX_KICK])
    tx_case('tx_level', [FLAG], [TX_KICK])


if __name__ == '__main__':
    os.makedirs(BUILD, exist_ok=True)
    {'rx': rx, 'overrun': overrun, 'tx': tx}[sys.argv[1]]()
