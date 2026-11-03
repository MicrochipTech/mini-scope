"""model2_serial.py - the Python side of the second model for SIM (tests/mock/sim2.c, tests/mock/model2.h).

The drive of the second model has other names, other structs and inverted logic compared with the example firmware:
axis[0].ctl.disable = 1 stops, the state values are 7, 8, 9, 12, the fault is a bit mask in axis[0].st.flags. Only the
interface differs, the dynamics behind it are those of tune_model.h.

    build(out, defines)        compiles a DLL of sim2.c with gcc, the way CLAUDE.md "Changing the code" shows
    Sim2Serial(dll)            serial port replacement like SimSerial, on that DLL
    table(dll, mark)           the VarTable of the model; mark puts a prefix in front of every identifier
    python tests/model2_serial.py <run_script.py arguments>
                               tools/run_script.py against the second model (environment M2_DLL, M2_MARK,
                               M2_ABORT_AFTER, M2_ABORT_TEXT), used by tests/test_adapt.py in a process of its own
"""
import ctypes
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
sys.path[:0] = [HERE, os.path.join(ROOT, 'gui'), os.path.join(ROOT, 'tools')]
from elf_vars import VarTable                       # noqa: E402
from sim_serial import SimSerial                    # noqa: E402

BASE = 0x1400
# name -> (offset in struct m2_axis, type), the order of struct m2_axis in tests/mock/model2.h
AXIS = {'axis[0].ctl.disable': (0, 'u16'), 'axis[0].st.mode': (2, 'u16'), 'axis[0].st.rpm': (4, 's16'),
        'axis[0].st.rpm_cmd': (6, 's16'), 'axis[0].st.torque': (8, 's16'), 'axis[0].st.flags': (10, 'u16'),
        'axis[0].sl.kp': (12, 's16'), 'axis[0].sl.ki': (14, 's16'), 'axis[0].sl.trim': (16, 's16'),
        'axis[0].cl.ref': (18, 's16'), 'axis[0].cl.act': (20, 's16'), 'axis[0].cl.kp': (22, 's16'),
        'axis[0].cl.ki': (24, 's16'), 'axis[0].su.acc': (26, 'u16'), 'axis[0].su.lock_amp': (28, 'u16'),
        'axis[0].su.switch_rpm': (30, 'u16')}
SIZE = 32
ALIVE, BUILD_ID = 0x1480, 0x1482
M2_IDLE, M2_LOCATE, M2_SPINUP, M2_RUN = 7, 8, 9, 12
FAULT_MASK = 0x03F0


def build(out, defines=()):
    """Compiles tests/mock/sim2.c and firmware/scope.c into the DLL `out`. Raises with the gcc output on an error."""
    cmd = ['gcc', '-O2', '-shared', f'-I{os.path.join(HERE, "mock")}', f'-I{os.path.join(ROOT, "firmware")}',
           *[f'-D{d}' for d in defines], '-o', out, os.path.join(HERE, 'mock', 'sim2.c'), os.path.join(ROOT, 'firmware', 'scope.c')]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError('gcc failed:\n' + p.stderr[-800:])
    return out


def layout(dll):
    """The offsets gcc computed for struct m2_axis: 16 member offsets in the order of AXIS, then the size."""
    lib = ctypes.CDLL(dll)
    buf = (ctypes.c_int * 32)()
    n = lib.sim_axis_layout(buf)
    return list(buf)[:n]


def names(mark=''):
    """name -> (address, size, type); mark is put in front of every identifier of the model's own names."""
    out = {}
    for n, (off, typ) in AXIS.items():
        key = re.sub(r'[A-Za-z_]\w*', lambda m: mark + m.group(0), n) if mark else n
        out[key] = (BASE + off, 2, typ)
    out['tune_alive'] = (ALIVE, 2, 'u16')
    out['tune_build_id'] = (BUILD_ID, 2, 'u16')
    return out


def peek16(ser, addr):
    """A 16 bit value of the model, read from the RAM of the DLL (no stream, no command to the target)."""
    return ser.lib.sim_peek16(addr)


def build_text(dll):
    lib = ctypes.CDLL(dll)
    lib.sim_build.restype = ctypes.c_char_p
    return lib.sim_build().decode('latin-1')


class Symbols(dict):
    """Like sim_serial.Symbols: the const text tune_build of the DLL is the `strings` of the table (none if text is False)."""

    def __init__(self, dll, entries, text=True):
        super().__init__({k: (a, s) for k, (a, s, _) in entries.items()})
        self.dll, self.text = dll, text

    @property
    def strings(self):
        return {'tune_build': build_text(self.dll)} if self.text else {}


def table(dll, mark='', extra=None, drop=(), text=True):
    """extra: more name -> (address, size, type) entries, drop: names left out, text False: no tune_build text."""
    n = names(mark)
    n.update(extra or {})
    for d in drop:
        del n[d]
    return VarTable.from_symbols(Symbols(dll, n, text), {k: t for k, (_, _, t) in n.items()})


class Sim2Serial(SimSerial):
    """SimSerial on a DLL of the second model. bad_build_id: the target reports another build than the ELF."""

    def __init__(self, dll, bad_build_id=False):
        super().__init__(dll=dll)
        if bad_build_id:
            self.lib.sim_poke8(BUILD_ID, self.lib.sim_peek16(BUILD_ID) ^ 0xFF)


def drive(argv):
    """tools/run_script.py with the port SIM mapped to the second model."""
    import threading
    import scope_gui
    import run_script
    import tune_api
    dll, mark = os.environ['M2_DLL'], os.environ.get('M2_MARK', '')
    tab = table(dll, mark)
    run_script.make_table = lambda a: tab
    scope_gui._open_port = lambda name, baud, progress: Sim2Serial(dll)
    after = float(os.environ.get('M2_ABORT_AFTER') or 0)
    if after:
        threading.Timer(after, lambda: tune_api.abort(os.environ.get('M2_ABORT_TEXT', 'abort'))).start()
    sys.argv = ['run_script.py'] + argv
    run_script.main()


if __name__ == '__main__':
    drive(sys.argv[1:])
