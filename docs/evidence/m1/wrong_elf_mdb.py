"""wrong_elf_mdb.py - card 5.1, MDB: the firmware of one ELF runs in the simulator, the PC resolves names from another.

    python docs/evidence/m1/wrong_elf_mdb.py <ELF the simulator runs> <ELF the PC reads names and tune_build from>

Two builds of test.X (configuration sim) made one after the other: the second must be refused with E402 and not a
write may reach the target. The ELF files are not in git.
"""
import os
import struct
import sys
import tempfile
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..'))
sys.path[:0] = [os.path.join(ROOT, 'gui'), os.path.join(ROOT, 'tools')]
import tune_api  # noqa: E402
from elf_vars import VarTable  # noqa: E402
from scope_gui import Scope  # noqa: E402
from mdb_serial import MdbSerial  # noqa: E402

fw, tab = sys.argv[1], sys.argv[2]            # ELF the simulator runs, ELF the PC resolves names from


class Logged(MdbSerial):
    log = []

    def write(self, data):
        b = bytes(data)
        if b[:1] == b'W':
            for i in range(0, len(b), 8):
                a, w = struct.unpack('<HB', b[i + 1:i + 4])
                self.log.append(('W', a, int.from_bytes(b[i + 4:i + 4 + w], 'little')))
        else:
            self.log.append((chr(b[0]),))
        return super().write(b)


ser = Logged(fw)
try:
    sc = Scope(ser)
    print('ping', sc.ping())
    Logged.log.clear()
    r = tune_api.probe(os.path.join(ROOT, 'scripts', 'tune_startup.py'), sc, VarTable.from_elf(tab),
                       out_dir=tempfile.mkdtemp())
    print('status', r['status'], 'codes', r['rule_codes'])
    print('reason', r['reason'])
    print('commands after the ping', [x[0] for x in Logged.log])
    print('writes', [x for x in Logged.log if x[0] == 'W'])
finally:
    ser.close()
