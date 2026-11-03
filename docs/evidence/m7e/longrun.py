"""longrun.py - card M7e, N runs of one tuning script in a row on SIM, one process, one Scope.

    python docs/evidence/m7e/longrun.py [runs] [script] [out folder]

Prints at the start and after 1, 10, 50 and 100 runs: RSS of the process, open handles, threads (Windows
GetProcessHandleCount and the thread list of the process), threading.active_count() and the status of the run.
At the end the out folder is deleted: Windows refuses that while a file in it is open, which counts open files.
The run folders go to the out folder (default: scratch folder in the temp directory, not the repository).
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..'))
sys.path[:0] = [os.path.join(ROOT, 'gui'), os.path.join(ROOT, 'tests'), os.path.join(ROOT, 'tools')]
import scope_gui as g           # noqa: E402
import tune_api                 # noqa: E402
from elf_vars import VarTable   # noqa: E402
from sim_serial import SYMBOLS, SYMBOL_TYPES    # noqa: E402

k32 = ctypes.WinDLL('kernel32')
psapi = ctypes.WinDLL('psapi')
k32.GetCurrentProcess.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.Thread32First.argtypes = k32.Thread32Next.argtypes = [wt.HANDLE, ctypes.c_void_p]
k32.GetProcessHandleCount.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD]
k32.CloseHandle.argtypes = [wt.HANDLE]


class PMC(ctypes.Structure):
    _fields_ = [('cb', wt.DWORD), ('PageFaultCount', wt.DWORD)] + \
               [(n, ctypes.c_size_t) for n in ('Peak', 'WorkingSetSize', 'QPPP', 'QPP', 'QPNPP', 'QNPP', 'Pagefile', 'PeakPagefile')]


def rss_mb():
    c = PMC()
    c.cb = ctypes.sizeof(c)
    psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
    return c.WorkingSetSize / 1048576.0


def handles():
    n = wt.DWORD()
    k32.GetProcessHandleCount(k32.GetCurrentProcess(), ctypes.byref(n))
    return n.value


def os_threads():
    class TE(ctypes.Structure):
        _fields_ = [(x, wt.DWORD) for x in ('size', 'usage', 'tid', 'owner', 'base')] + [('delta', wt.LONG), ('flags', wt.DWORD)]
    snap = k32.CreateToolhelp32Snapshot(4, 0)
    e = TE()
    e.size = ctypes.sizeof(e)
    pid, n = os.getpid(), 0
    ok = k32.Thread32First(snap, ctypes.byref(e))
    while ok:
        n += e.owner == pid
        ok = k32.Thread32Next(snap, ctypes.byref(e))
    k32.CloseHandle(snap)
    return n


def line(tag, status=''):
    print(f'{tag:>8}  rss {rss_mb():7.1f} MB  handles {handles():4d}  os threads {os_threads():3d}  '
          f'py threads {threading.active_count():3d}  {status}', flush=True)


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    script = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, 'scripts', 'tune_startup.py'))
    out = sys.argv[3] if len(sys.argv) > 3 else tempfile.mkdtemp(prefix='m7e_longrun_')
    table = VarTable.from_symbols(SYMBOLS, SYMBOL_TYPES)
    sc = g.Scope(g.open_port('SIM', 115200))
    assert sc.ping()
    line('start')
    marks = {1, 10, 50, 100, n}
    bad = 0
    t0 = time.perf_counter()
    for i in range(1, n + 1):
        s = tune_api.run(script, sc, table, out_dir=out, elf_name='model')
        s.done.wait()
        st = s.result['status']
        bad += st != 'ok'
        if i in marks:
            line(f'run {i}', f'status {st}, {time.perf_counter() - t0:.0f} s elapsed, not ok so far {bad}')
        elif st != 'ok':
            print(f'run {i}: status {st}: {s.result["reason"]}', flush=True)
    g.close_ports()
    time.sleep(1)
    line('closed', f'run folders: {len(os.listdir(out))} in {out}')
    print('threads left:', sorted(t.name for t in threading.enumerate()))
    import shutil
    try:                        # Windows refuses to delete a file that is still open
        shutil.rmtree(out)
        print('open files: none (the whole out folder could be deleted)')
    except OSError as e:
        print('open files: the out folder could not be deleted:', e)


if __name__ == '__main__':
    main()
