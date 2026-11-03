"""make_load.py - builds the load firmware of card M7b from a scratch copy of test.X, with make.

    python docs/evidence/m7b/make_load.py <name> <mips> [share_pct] [overhead] [burn]

name       folder in the temp directory, and the name of the result (ELF path is printed)
mips       32 or 100: PLL setting of firmware/load_main.c and SCOPE_FP_HZ
share_pct  share of the control ISR in percent (default 60)
overhead   cycles of the ISR besides the REPEAT loop (default 0, see M7b.md)
burn       REPEAT count, overrides share_pct and overhead

The worktree is not touched: test.X and firmware/ are copied, the copy gets a configuration "load"
(a copy of "sim" with LOAD_TEST=1) in which load_main.c replaces example_main.c. Makefiles come from
prjMakefilesGenerator, as in start_sim.bat, then make builds CONF=load. The build prints the size of
the ELF and the symbols it needs.
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
MPLABX = r'C:\Program Files\Microchip\MPLABX\v6.35'
GNU = MPLABX + r'\gnuBins\GnuWin32\bin'
BUILD = os.path.join(tempfile.gettempdir(), 'mini_scope_m7b')


def sh(cmd, cwd):
    env = dict(os.environ, PATH=GNU + ';' + os.environ['PATH'])
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f'{cmd[0]} failed\n{r.stdout[-2000:]}\n{r.stderr[-1500:]}')
    return r.stdout + r.stderr


def build(name, mips, share=60, overhead=0, burn=None):
    d = os.path.join(BUILD, name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    shutil.copytree(os.path.join(REPO, 'firmware'), os.path.join(d, 'firmware'))
    shutil.copytree(os.path.join(REPO, 'test.X'), os.path.join(d, 'test.X'),
                    ignore=shutil.ignore_patterns('build', 'dist', 'debug', '.generated_files', 'Makefile-*', 'private'))
    x = os.path.join(d, 'test.X')
    path = os.path.join(x, 'nbproject', 'configurations.xml')
    with open(path, encoding='utf-8', newline='') as f:
        xml = f.read()
    nl = '\r\n' if '\r\n' in xml else '\n'
    xml = xml.replace('../firmware/example_main.c', '../firmware/load_main.c')
    i = xml.index('<conf name="sim"')
    j = xml.index('</conf>', i) + len('</conf>')
    sim = xml[i:j]
    load = sim.replace('<conf name="sim"', '<conf name="load"')
    load = load.replace('value="__MPLAB_DEBUGGER_SIMULATOR=1"', 'value="__MPLAB_DEBUGGER_SIMULATOR=1;LOAD_TEST=1"', 1)
    assert 'LOAD_TEST=1' in load
    xml = xml[:j] + nl + '    ' + load + xml[j:]
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(xml)
    out = sh([MPLABX + r'\mplab_platform\bin\prjMakefilesGenerator.bat', '.'], x)
    flags = f'-DLOAD_MIPS={mips} -DSCOPE_FP_HZ={mips}000000UL -DLOAD_SHARE_PCT={share} -DLOAD_OVERHEAD={overhead}'
    if burn is not None:
        flags += f' -DLOAD_BURN={burn}'
    sh([GNU + r'\make.exe', '-f', 'Makefile', 'CONF=load', 'clean'], x)
    out = sh([GNU + r'\make.exe', '-f', 'Makefile', 'CONF=load', 'build', f'MP_EXTRA_CC_PRE={flags}'], x)
    warn = [ln for ln in out.splitlines() if 'warning' in ln.lower()]
    elf = os.path.join(x, 'dist', 'load', 'production', 'test.X.production.elf')
    print(f'built {elf}\nflags {flags}\nwarnings {len(warn)}')
    for w in warn[:5]:
        print('  ', w[-160:])
    return elf


if __name__ == '__main__':
    a = sys.argv[1:]
    build(a[0], int(a[1]), *(int(v) for v in a[2:]))
