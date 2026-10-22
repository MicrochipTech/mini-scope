"""make_pdfs.py - builds USER_GUIDE.pdf, SIMULATOR.pdf and TROUBLESHOOTING.pdf from the .md files.

    python tools\\make_pdfs.py

Needs pandoc and Microsoft Edge: Markdown -> HTML (docs/pdf.css) -> Edge prints the PDF.
"""
import os
import shutil
import subprocess
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
DOCS = ('USER_GUIDE', 'SIMULATOR', 'TROUBLESHOOTING')
EDGE = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'


def main():
    pandoc = shutil.which('pandoc') or 'pandoc'
    with tempfile.TemporaryDirectory() as tmp:
        for name in DOCS:
            html = os.path.join(tmp, name + '.html')
            pdf = os.path.join(ROOT, name + '.pdf')
            subprocess.run([pandoc, name + '.md', '-s', '--embed-resources', '--css', 'docs/pdf.css',
                            '--metadata', 'lang=en', '-o', html], cwd=ROOT, check=True)
            if os.path.exists(pdf):
                os.remove(pdf)
            edge = subprocess.Popen([EDGE, '--headless=new', '--disable-gpu', '--no-pdf-header-footer',
                                     f'--user-data-dir={os.path.join(tmp, "edge")}',
                                     f'--print-to-pdf={pdf}', 'file:///' + html.replace('\\', '/')],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                edge.wait(60)
            except subprocess.TimeoutExpired:     # Edge sometimes stays alive after printing
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(edge.pid)], capture_output=True)
            if not os.path.exists(pdf):
                raise SystemExit(f'Edge did not write {pdf}')
            print(pdf)


if __name__ == '__main__':
    main()
