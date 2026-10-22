"""mdb_serial.py - serial port replacement that runs the firmware in the MPLAB X simulator.

The compiled ELF (test.X, configuration "sim") runs in MDB, the command line
debugger of MPLAB X. Bytes from the PC go into the mailbox sim_rx/sim_rx_len
of example_main.c (the simulator's UART cannot receive), bytes from the target
are taken from the simulator's UART output file (uart=SCOPE_UART).

Slow: start + program take about 40 s, the UART output appears with a delay
of a few seconds. Good enough to check the real firmware without hardware.
"""
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


class MdbSerial:
    quiet = 6.0                 # Scope.stop(): the output file lags by a few seconds
    reset_wait = 40.0           # Scope.reset(): restart + first answer through the lagging file

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
        self.progress(0.02, 'Starting MDB (Java) ...')
        for c in (f'Device {DEVICE}', 'Set oscillator.frequency 64', 'Set oscillator.frequencyunit Mega',
                  f'Set uart{uart}io.uartioenabled true', f'Set uart{uart}io.output file',
                  f'Set uart{uart}io.outputfile {self.out}', 'Hwtool SIM'):
            self._cmd(c)
        self._cmd(f'Program "{elf}"')
        self._wait_for('Program succeeded', 120, 0.02, 0.75, est=25,
                       stages={'Resetting SFRs': 'Simulator ready, loading firmware'})
        self._cmd('Run')
        self.t_cmd = 0.0
        t1 = t_send = time.time()
        self.write(b'S')            # the first mailbox write after Run tends to get lost: repeat
        while not self.in_waiting:
            if time.time() - t1 > 90:
                raise TimeoutError('MDB: firmware does not respond')
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
        raise TimeoutError(f'MDB: "{text}" did not arrive')

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
        try:
            self._cmd('Halt')
            self._cmd('Quit')
            self.p.wait(10)
        except Exception:       # noqa: BLE001 - MDB may already be gone
            self.p.kill()
