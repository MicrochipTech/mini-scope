"""m0_rx.py - result 5 of card M0: can UxRX_ISR be reached with injected data?

    python docs/evidence/m0/m0_rx.py [case ...]

scope.c gets two counters in the scratch copy: rx_hits (ISR entries) and
rx_bytes (bytes taken from UxRXREG). A byte 'P' that really arrives must also
produce the ping reply "SCP1" in the UART output file. Without a case name, all
cases run. Every case is a fresh MDB session.
"""
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time

from m0_common import BUILD, MPLABX, build, config_stream, mdb_lines, symbols

DEVICE = 'dsPIC33CK64MC103'
BYTE = 0x50                                     # 'P'

SCOPE_EDITS = [
    ('void SCOPE_ISR UxRX_ISR(void)\n{\n    UxRXIF = 0;\n    while (!UxSTAHbits.URXBE)\n'
     '        scope_rx_byte((uint8_t)UxRXREG);\n',
     'volatile uint16_t rx_hits, rx_bytes;\n'
     'void SCOPE_ISR UxRX_ISR(void)\n{\n    UxRXIF = 0;\n    rx_hits++;\n'
     '    while (!UxSTAHbits.URXBE) {\n        rx_bytes++;\n'
     '        scope_rx_byte((uint8_t)UxRXREG);\n    }\n'),
]
# the firmware raises the flag on request: shows that the counters and the vector work
MAIN_EDITS = [
    ('        i++;\n', '        i++;\n        if (probe == 1) {\n            probe = 0;\n'
                       '            _U1RXIF = 1;\n        }\n'),
]


def scl(body, cfg=''):
    return (f'configuration for "{DEVICE}" is\n{cfg}end configuration;\n\n'
            f'testbench for "{DEVICE}" is\nbegin\n  process is\n'
            f'{body}\nend process;\nend testbench;\n')


def uart_bits(byte, pin):
    """SCL for one 8N1 byte at 115200 baud on an input pin."""
    t = 'wait for 8680 ns;'
    lines = [f'    {pin} <= \'0\'; {t}']
    for i in range(8):
        lines.append(f"    {pin} <= '{(byte >> i) & 1}'; {t}")
    lines.append(f"    {pin} <= '1'; {t}")
    return '\n'.join(lines)


CASES = {
    # name: dict(sets=MDB Set lines, stim=SCL process, writes=f(symbols) -> MDB commands, note)
    'firmware_flag': dict(probe=1, note='firmware sets _U1RXIF, proves the counters'),
    'mdb_input_file': dict(sets=['Set uart1io.input file', 'Set uart1io.inputfile {rx}'],
                           note='UART input file, same keys as the output file'),
    'scl_packetin_hex': dict(stim='  begin\n    wait for 100 us;\n    packetin("50", U1RXREG, true);\n    wait;',
                             note='packetin literal hex'),
    'scl_packetin_var': dict(stim='    variable s : line;\n  begin\n    wait for 100 us;\n'
                                  '    s := "P";\n    packetin(s, U1RXREG, true);\n    wait;',
                             note='packetin with a string variable'),
    'scl_accessin': dict(stim='  begin\n    wait for 100 us;\n'
                              '    accessin("{rxscl}", hex_mode, U1RXREG, false);\n'
                              '    IFS0.U1RXIF <= \'1\';\n    wait;',
                         note='accessin on demand, then the flag'),
    'scl_regs': dict(stim='  begin\n    wait for 100 us;\n    U1RXREG <= 16#50#;\n'
                          '    U1STAH.URXBE <= \'0\';\n    IFS0.U1RXIF <= \'1\';\n    wait;',
                     note='SCL writes RX register, RX empty flag and interrupt flag'),
    'scl_flag_only': dict(stim='  begin\n    wait for 100 us;\n    IFS0.U1RXIF <= \'1\';\n    wait;',
                          note='SCL sets the interrupt flag only'),
    'scl_pin': dict(stim='  begin\n    RB6 <= \'1\';\n    wait for 100 us;\n' + uart_bits(BYTE, 'RB6') + '\n    wait;',
                    note='SCL drives the UART frame on the RX pin RB6'),
    'scl_packetin_value': dict(stim='    variable s : line;\n  begin\n    wait for 100 us;\n'
                                    '    s := "50 50";\n    packetin(s, U1RXREG, true);\n    wait;',
                               note='packetin with hex values in a string variable'),
    'scl_word_flag': dict(stim='  begin\n    wait for 100 us;\n    IFS0 <= 16#0800#;\n    wait;',
                          note='SCL writes the whole IFS0 word'),
    'scl_sync': dict(cfg='  shared location m0_probe := 16#{probe:X}#;\n  shared location m0_rxb := 16#{rxb:X}#;\n',
                     stim='  begin\n    wait for 200 us;\n    U1RXREG <= 16#50#;\n'
                          "    U1STAH.URXBE <= '0';\n    m0_probe <= 16#01#;\n"
                          "    wait until m0_rxb != 0;\n    U1STAH.URXBE <= '1';\n    wait;",
                     note='SCL presents a byte, the firmware raises the flag, SCL ends it when the ISR read it'),
    'scl_packetin_stream': dict(stim='    variable s : line;\n  begin\n    wait for 100 us;\n'
                                     '    s := "{cmds}";\n    packetin(s, U1RXREG, true);\n    wait;',
                                note='a complete configure + run through the real receive path'),
    'scl_packetin_stream_pad': dict(stim='    variable s : line;\n  begin\n    wait for 100 us;\n'
                                         '    s := "{cmds} 00";\n    packetin(s, U1RXREG, true);\n    wait;',
                                    note='the same, one padding byte behind the run command'),
    'scl_packetin_one': dict(stim='    variable s : line;\n  begin\n    wait for 100 us;\n'
                                  '    s := "50";\n    packetin(s, U1RXREG, true);\n    wait;',
                             note='a single byte'),
    'mdb_regs': dict(writes=True, note='MDB write to RX register, status and IFS0 while running'),
}


class Session:
    def __init__(self, elf, out, sets, stim):
        self.lines = queue.Queue()
        self.p = subprocess.Popen(['cmd', '/c', os.path.join(MPLABX, r'mplab_platform\bin\mdb.bat')],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=lambda: [self.lines.put(x) for x in self.p.stdout], daemon=True).start()
        for c in [f'Device {DEVICE}', 'Set oscillator.frequency 64', 'Set oscillator.frequencyunit Mega',
                  'Set uart1io.uartioenabled true', 'Set uart1io.output file',
                  f'Set uart1io.outputfile {out}'] + sets + ['Hwtool SIM', f'Program "{elf}"']:
            self.cmd(c)
        t = time.time()
        log = []
        while time.time() - t < 120:
            log += mdb_lines(self, 0.5)
            if any('Program succeeded' in x for x in log):
                break
        else:
            raise TimeoutError('Program')
        self.log = [x for x in log if 'rror' in x or 'nvalid' in x or 'nknown' in x]
        if stim:
            self.cmd(f'Stim "{stim}"')
            self.log += [x for x in mdb_lines(self, 3) if x != '>']
        self.cmd('Run')

    def cmd(self, c):
        self.p.stdin.write(c + '\n')
        self.p.stdin.flush()

    def show(self, name):
        self.cmd(f'Print {name}')
        lines = mdb_lines(self, 1.5)
        return ' '.join(x.lstrip('>') for x in lines if x != '>')

    def close(self):
        self.cmd('Halt')
        self.cmd('Quit')
        try:
            self.p.wait(15)
        except subprocess.TimeoutExpired:
            self.p.kill()


def run_case(name, c, elf, sym):
    d = tempfile.mkdtemp(dir=BUILD)
    out = os.path.join(d, 'uart.bin')
    rx = os.path.join(d, 'rx.bin')
    with open(rx, 'wb') as f:
        f.write(bytes([BYTE]))
    rxscl = os.path.join(d, 'rx.txt').replace('\\', '/')
    with open(rxscl, 'w') as f:
        f.write('50\n')
    sets = [x.format(rx=rx) for x in c.get('sets', [])]
    stim = None
    if 'stim' in c:
        stim = os.path.join(d, 'stim.scl').replace('\\', '/')
        with open(stim, 'w') as f:
            cfg = c.get('cfg', '').format(probe=sym['probe'], rxb=sym['rx_bytes'])
            cmds = ' '.join(f'{x:02X}' for x in config_stream([sym['probe']], 1000) + b'R')
            f.write(scl(c['stim'].replace('{rxscl}', rxscl).replace('{cmds}', cmds), cfg))
    s = Session(elf, out, sets, stim)
    time.sleep(4)
    if c.get('probe'):
        s.cmd(f"write {sym['probe']} {c['probe']}")
    if c.get('writes'):
        # 32 bit writes: U1RXREG, then the status word, then IFS0; addresses from the ELF
        s.cmd(f"write {sym['U1RXREG']} {BYTE}")
        s.cmd(f"write {sym['U1STAH']} 0")
        s.cmd(f"write {sym['IFS0']} {1 << 11}")
    time.sleep(6)
    res = [s.show(x) for x in ('rx_hits', 'rx_bytes', 'U1STAH', 'IFS0')]
    s.close()
    time.sleep(1)
    size = os.path.getsize(out) if os.path.exists(out) else -1
    data = open(out, 'rb').read(16) if size > 0 else b''
    print(f'{name}: {c["note"]}\n   {res}\n   UART output {size} bytes {data!r}\n   MDB said: {s.log[:6]}', flush=True)


def main():
    elf = build('rx', 1, main_edits=MAIN_EDITS, scope_edits=SCOPE_EDITS)
    sym = symbols(elf)
    for k in ('U1RXREG', 'U1STAH', 'IFS0'):
        print(k, hex(sym.get(k, 0)))
    for name in (sys.argv[1:] or CASES):
        run_case(name, CASES[name], elf, sym)


if __name__ == '__main__':
    main()
