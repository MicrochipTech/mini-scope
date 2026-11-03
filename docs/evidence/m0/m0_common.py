"""m0_common.py - builds the throwaway firmware of card M0 and talks to it in MDB.

The firmware is built from a scratch copy in the temp folder; the worktree is
not touched. Used by m0_stream.py, m0_lag.py and m0_rx.py.
"""
import os
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
sys.path.insert(0, os.path.join(REPO, 'tools'))
import mdb_serial                                       # noqa: E402

MPLABX, MdbSerial, symbols = mdb_serial.MPLABX, mdb_serial.MdbSerial, mdb_serial.symbols

GNU = MPLABX + r'\gnuBins\GnuWin32\bin'
BUILD = os.path.join(tempfile.gettempdir(), 'mini_scope_m0')


def sh(cmd, cwd):
    env = dict(os.environ, PATH=GNU + ';' + os.environ['PATH'])
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f'{cmd[0]} failed\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}')


def patch(path, edits):
    with open(path) as f:
        s = f.read()
    for old, new in edits:
        assert old in s, old
        s = s.replace(old, new, 1)
    with open(path, 'w') as f:
        f.write(s)


def build(name, fast, main_edits=(), scope_edits=(), load_edits=()):
    """Scratch copy + load + build, returns the ELF.

    main_edits, scope_edits, load_edits: (old, new) text replacements in
    example_main.c, scope.c and m0_load.h."""
    d = os.path.join(BUILD, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    shutil.copytree(os.path.join(REPO, 'firmware'), os.path.join(d, 'firmware'))
    shutil.copytree(os.path.join(REPO, 'test.X'), os.path.join(d, 'test.X'),
                    ignore=shutil.ignore_patterns('build', 'dist', 'debug', '.generated_files', 'Makefile-*'))
    with open(os.path.join(HERE, 'm0_load.h')) as f:
        load = f.read()
    with open(os.path.join(d, 'firmware', 'm0_load.h'), 'w') as f:
        f.write(f'#define M0_FAST {fast}\n' + load)
    edits = [('#include "scope.h"\n', '#include "scope.h"\n#include "m0_load.h"\n'),
             ('    scope_init();\n', '    scope_init();\n    m0_init();\n')] + list(main_edits)
    patch(os.path.join(d, 'firmware', 'example_main.c'), edits)
    patch(os.path.join(d, 'firmware', 'scope.c'), scope_edits)
    patch(os.path.join(d, 'firmware', 'm0_load.h'), load_edits)
    x = os.path.join(d, 'test.X')
    sh([MPLABX + r'\mplab_platform\bin\prjMakefilesGenerator.bat', '.'], x)
    sh([GNU + r'\make.exe', '-f', 'Makefile', 'CONF=sim', 'build'], x)
    return os.path.join(x, 'dist', 'sim', 'production', 'test.X.production.elf')


def config_stream(addrs, period_us):
    """'C' command for a stream of u16 channels, see the protocol in scope.c."""
    b = bytes([ord('C'), len(addrs), 0]) + struct.pack('<H', period_us)
    b += bytes([0, 0]) + struct.pack('<i', 0) + struct.pack('<H', 0)
    for a in addrs:
        b += struct.pack('<HB', a, 2)
    return b


def write_cmd(addr, value):
    return b'W' + struct.pack('<HBI', addr, 2, value)


def mdb_lines(ser, secs):
    """MDB console output of the next secs seconds, without the log noise."""
    end = time.time() + secs
    out = []
    while time.time() < end:
        try:
            line = ser.lines.get(timeout=0.1).rstrip()
        except queue.Empty:
            continue
        if line and 'W0107' not in line and 'INFO' not in line:
            out.append(line)
    return out


class Frames:
    """Parses stream frames (0xA5 seq n*u16) from the growing UART output file."""

    def __init__(self, ser, n_ch):
        self.ser, self.size = ser, 2 + 2 * n_ch
        self.buf = b''
        self.pos = os.path.getsize(ser.out)
        self.bad = 0
        self.last_seq = None
        self.index = 0                  # sample number including lost ones
        self.lost = 0
        self.received = 0

    def poll(self):
        """Returns the new frames as (index, values)."""
        with open(self.ser.out, 'rb') as f:
            f.seek(self.pos)
            new = f.read()
        self.pos += len(new)
        self.buf += new
        out = []
        n = self.size
        i = 0
        while len(self.buf) - i >= n + 1:
            if self.buf[i] != 0xA5 or self.buf[i + n] != 0xA5:
                self.bad += 1
                i += 1
                continue
            seq = self.buf[i + 1]
            gap = 0 if self.last_seq is None else (seq - self.last_seq - 1) & 255
            self.lost += gap
            self.index += gap + 1
            self.last_seq = seq
            self.received += 1
            out.append((self.index, struct.unpack(f'<{(n - 2) // 2}H', self.buf[i + 2:i + n])))
            i += n
        self.buf = self.buf[i:]
        return out


def start(ser, addrs, period_us=1000):
    """Configures and starts a stream, returns when frames arrive."""
    ser.write(config_stream(addrs, period_us))
    ser.write(b'R')
    t = time.time()
    while ser.in_waiting < 100:
        if time.time() - t > 60:
            raise TimeoutError('no stream')
        time.sleep(0.1)
