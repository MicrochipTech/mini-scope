"""build_nokick.py - card M7e, builds the MDB variant without tx_kick() in a scratch copy (temp folder).

    python docs/evidence/m7e/build_nokick.py        prints the path of the ELF

tx_kick() sets only the enable bit and the 10 kHz model ISR no longer raises U1TXIF, as docs/evidence/M7a.md
section 3 describes. Uses build() of m7a_sim.py; the worktree firmware is not changed.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'm7a'))
import m7a_sim as m                                             # noqa: E402

if __name__ == '__main__':
    os.makedirs(m.BUILD, exist_ok=True)
    print(m.build('tx_nokick', main_edits=[m.NO_10K], scope_edits=[m.TX_KICK]))
