"""mdb_serial.py - serial port replacement that runs the firmware in the MPLAB X simulator.

The compiled ELF (test.X, configuration "sim") runs in MDB, the command line
debugger of MPLAB X. Bytes from the PC go into the mailbox sim_rx/sim_rx_len
of example_main.c (the simulator's UART cannot receive), bytes from the target
are taken from the simulator's UART output file (uart=SCOPE_UART).

Slow: start + program take about 40 s, the UART output appears with a delay
of a few seconds. Good enough to check the real firmware without hardware.
"""
import atexit
import ctypes
import ctypes.wintypes as wt
import math
import os
import queue
import subprocess
import tempfile
import threading
import time

MPLABX = r'C:\Program Files\Microchip\MPLABX\v6.35'
XC_DSC = r'C:\Program Files\Microchip\xc-dsc\v3.30\bin'
DFP = MPLABX + r'\packs\Microchip\dsPIC33CK-MC_DFP\1.10.386\xc16'
DEVICE = 'dsPIC33CK64MC103'


def symbols(elf):
    out = subprocess.run([os.path.join(XC_DSC, 'xc-dsc-nm.exe'), f'-mdfp={DFP}', elf],
                         capture_output=True, text=True, check=True).stdout
    return {line.split()[2].lstrip('_'): int(line.split()[0], 16)
            for line in out.splitlines() if len(line.split()) == 3}


def kill_on_close_job():
    """A Windows job object that kills every process in it when its last handle closes, also when this
    process is killed hard. Returns the handle, or None if it cannot be made."""
    try:
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateJobObjectW.restype = wt.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
        k.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]

        class Basic(ctypes.Structure):
            _fields_ = [('a', ctypes.c_int64), ('b', ctypes.c_int64), ('LimitFlags', wt.DWORD), ('c', ctypes.c_size_t),
                        ('d', ctypes.c_size_t), ('e', wt.DWORD), ('f', ctypes.c_size_t), ('g', wt.DWORD), ('h', wt.DWORD)]

        class Counters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in 'abcdef']

        class Extended(ctypes.Structure):
            _fields_ = [('Basic', Basic), ('Io', Counters), ('i', ctypes.c_size_t), ('j', ctypes.c_size_t),
                        ('k', ctypes.c_size_t), ('l', ctypes.c_size_t)]
        job = k.CreateJobObjectW(None, None)
        info = Extended()
        info.Basic.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not job or not k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            return None
        return job
    except (OSError, AttributeError):
        return None


def full_speed(job):
    """Windows treats a process without a window as background work and may switch it to its power saving mode (EcoQoS)
    at any moment. The simulator then needs 45 to 60 wall clock seconds per target second instead of 14 and flips between
    the two during a run (docs/evidence/4.4b.md). This turns that off for the processes of the job. Returns how many."""
    class Throttling(ctypes.Structure):
        _fields_ = [('Version', wt.ULONG), ('ControlMask', wt.ULONG), ('StateMask', wt.ULONG)]

    class Pids(ctypes.Structure):
        _fields_ = [('assigned', wt.DWORD), ('listed', wt.DWORD), ('pid', ctypes.c_size_t * 64)]

    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.restype = wt.HANDLE
    k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k.CloseHandle.argtypes = [wt.HANDLE]
    k.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
    k.SetProcessInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    pids = Pids()
    if not job or not k.QueryInformationJobObject(job, 3, ctypes.byref(pids), ctypes.sizeof(pids), None):   # BasicProcessIdList
        return 0
    state = Throttling(1, 1, 0)         # PROCESS_POWER_THROTTLING_EXECUTION_SPEED under control, and off
    done = 0
    for pid in pids.pid[:pids.listed]:
        h = k.OpenProcess(0x0200, False, pid)       # PROCESS_SET_INFORMATION
        if h:
            done += bool(k.SetProcessInformation(h, 4, ctypes.byref(state), ctypes.sizeof(state)))  # ProcessPowerThrottling
            k.CloseHandle(h)
    return done


class MdbNoAnswer(TimeoutError):
    """The simulator start-up did not finish. A TimeoutError, so old callers still work; str() is the user text."""


class MdbSerial:
    quiet = 6.0                 # Scope.stop(): the output file lags by a few seconds
    reset_wait = 40.0           # Scope.reset(): restart + first answer through the lagging file
    time_scale = 25             # wall clock seconds per second of target time (14..18 measured, see DECISIONS.md)
    output_lag_ms = 3           # target time until a written byte shows up in the stream (with the TX kick)

    def __init__(self, elf, timeout=15.0, progress=None, uart=1):
        # uart: SCOPE_UART of the firmware (1..3)
        # progress(fraction 0..1, text) is called during the slow start-up
        self.progress = progress or (lambda f, t: print(f'{f:4.0%}  {t}'))
        self.t0 = time.time()
        sym = symbols(elf)
        self.rx_addr, self.len_addr = sym['sim_rx'], sym['sim_rx_len']
        self.timeout = timeout
        self.out = os.path.join(tempfile.gettempdir(), f'mini_scope_uart_{os.getpid()}.bin')
        open(self.out, 'wb').close()
        self.pos = 0
        self.lines = queue.Queue()
        self.p = subprocess.Popen(['cmd', '/c', os.path.join(MPLABX, r'mplab_platform\bin\mdb.bat')],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=lambda: [self.lines.put(x) for x in self.p.stdout], daemon=True).start()
        # the Java process is a grandchild of this one: a job object ends it with us, even after a hard kill
        self.job = kill_on_close_job()
        if self.job:
            ctypes.WinDLL('kernel32').AssignProcessToJobObject(wt.HANDLE(self.job), wt.HANDLE(int(self.p._handle)))
        atexit.register(self.close)
        self.progress(0.02, 'Starting MDB (Java) ...')
        for c in (f'Device {DEVICE}', 'Set oscillator.frequency 64', 'Set oscillator.frequencyunit Mega',
                  f'Set uart{uart}io.uartioenabled true', f'Set uart{uart}io.output file',
                  f'Set uart{uart}io.outputfile {self.out}', 'Hwtool SIM'):
            self._cmd(c)
        self._cmd(f'Program "{elf}"')
        self._wait_for('Program succeeded', 120, 0.02, 0.75, est=25,
                       stages={'Resetting SFRs': 'Simulator ready, loading firmware'})
        self.throttling_off = full_speed(self.job)      # processes switched to full speed, 0 = not in effect
        self._cmd('Run')
        self.t_cmd = 0.0
        t1 = t_send = time.time()
        self.write(b'S')            # the first mailbox write after Run tends to get lost: repeat
        while not self.in_waiting:
            if time.time() - t1 > 90:
                self.close()
                raise MdbNoAnswer(
                    'the firmware in the simulator sent nothing within 90 s: it may hang before scope_init(), '
                    'the TX path may be dead (no tx_kick), or the ELF is not a sim build. '
                    'See SIMULATOR.md section 9 and TROUBLESHOOTING.md section 4.2')
            if time.time() - t_send > 5:
                self.write(b'S')
                t_send = time.time()
            self._show(0.75, 0.99, t1, 8, 'Firmware starting, waiting for the first answer')
            time.sleep(0.2)
        self.progress(0.99, 'Firmware responds, waiting for the last bytes ...')
        time.sleep(self.quiet)      # late answers to repeated 'S'
        self.reset_input_buffer()
        self.progress(1.0, f'Simulator ready after {time.time() - self.t0:.0f} s')

    def reset_target(self):
        # device reset through MDB, also works when the firmware no longer reads the mailbox
        for c in ('Halt', 'Reset', 'Run'):
            self._cmd(c)
        self.t_cmd = time.time()

    def _cmd(self, c):
        self.p.stdin.write(c + '\n')
        self.p.stdin.flush()

    def _show(self, lo, hi, t_start, est, label):
        # creeps towards hi without ever reaching it: the real duration varies
        f = lo + (hi - lo) * (1 - math.exp(-(time.time() - t_start) / est))
        self.progress(f, f'{label} ({time.time() - self.t0:.0f} s, typically 40-60 s)')

    def _wait_for(self, text, secs, lo, hi, est, stages=None):
        start, end = time.time(), time.time() + secs
        label = 'Starting MDB (Java) and the simulator'
        while time.time() < end:
            try:
                line = self.lines.get(timeout=0.3)
                if text in line:
                    return
                for key, new in (stages or {}).items():
                    if key in line:
                        label = new
            except queue.Empty:
                pass
            self._show(lo, hi, start, est, label)
        self.close()
        raise MdbNoAnswer(f'the simulator did not report "{text}" within {secs} s: MPLAB X may be missing or '
                          'in another place, an old MDB process may block it, or the ELF could not be loaded. '
                          'See SIMULATOR.md section 9')

    def write(self, data):
        data = bytes(data)
        for i in range(0, len(data), 60):
            # give the firmware time to empty the mailbox of the previous chunk
            time.sleep(max(0.0, self.t_cmd + 1.0 - time.time()))
            chunk = data[i:i + 60]
            for k, b in enumerate(chunk):        # ascending: MDB writes 32 bit, the next write fixes the upper half
                self._cmd(f'write {self.rx_addr + 2 * k} {b}')
            self._cmd(f'write {self.len_addr} {len(chunk)}')
            self.t_cmd = time.time()
        return len(data)

    def _new(self):
        with open(self.out, 'rb') as f:
            f.seek(self.pos)
            return f.read()

    def read(self, n=1):
        end = time.time() + self.timeout
        data = b''
        while True:
            data = self._new()[:n]
            if len(data) >= n or time.time() > end:
                break
            time.sleep(0.05)
        self.pos += len(data)
        return data

    @property
    def in_waiting(self):
        return len(self._new())

    def reset_input_buffer(self):
        self.pos = os.path.getsize(self.out)

    def close(self):
        """Quits MDB, then ends the whole tree of the process started here, by its PID (never by image name)."""
        p = getattr(self, 'p', None)
        if p is None:
            return
        try:
            self._cmd('Halt')
            self._cmd('Quit')
            p.wait(10)
        except Exception:       # noqa: BLE001 - MDB may already be gone
            pass
        subprocess.run(['taskkill', '/T', '/F', '/PID', str(p.pid)], capture_output=True)
        job, self.job = getattr(self, 'job', None), None
        if job:                 # also reaches the Java process once cmd has gone, when the tree link is lost
            k = ctypes.WinDLL('kernel32')
            k.TerminateJobObject(wt.HANDLE(job), 1)
            k.CloseHandle(wt.HANDLE(job))
        self.p = None
