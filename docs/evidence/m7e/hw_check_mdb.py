"""hw_check_mdb.py - card M7e, runs tools/hw_check.py against an MDB session with a given ELF.

    python docs/evidence/m7e/hw_check_mdb.py <elf> [hw_check options]

hw_check.py opens port MDB through scope_gui.ARGS.mdb, which only the GUI start sets. Here it is set to the
ELF, then hw_check.main() runs unchanged. The report goes to the scratch folder given by --report.
"""
import argparse
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')
sys.path[:0] = [os.path.join(ROOT, 'gui'), os.path.join(ROOT, 'tools')]
import scope_gui as g       # noqa: E402
import hw_check             # noqa: E402

if __name__ == '__main__':
    elf = sys.argv[1]
    g.ARGS = argparse.Namespace(mdb=elf, sim=False)
    sys.argv = ['hw_check.py', '--port', 'MDB', '--elf', elf] + sys.argv[2:]
    hw_check.main()
