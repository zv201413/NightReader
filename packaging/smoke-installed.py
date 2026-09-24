"""Exercise installed modules and the installed launcher outside the checkout."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument('--root', type=Path, default=Path('/opt/nightreader'))
args = parser.parse_args()
root = args.root.resolve()
sys.dont_write_bytecode = True
sys.path[:0] = [str(root / 'lib'), str(root)]
os.environ['PYTHONPATH'] = os.pathsep.join((str(root / 'lib'), str(root)))
os.environ['PYTHONNOUSERSITE'] = '1'
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
os.environ['NO_AT_BRIDGE'] = '1'

import nightread
import fitz
import numpy
import PIL
for module in (nightread, fitz, numpy, PIL):
    assert Path(module.__file__).resolve().is_relative_to(root), module.__file__

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import GLib


def pump(seconds=.05):
    loop = GLib.MainLoop()
    GLib.timeout_add(max(1, int(seconds * 1000)), loop.quit)
    loop.run()


def wait(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pump()
        if condition():
            return
    raise AssertionError('Installed GUI did not reach the expected state')


def xdo(*args):
    with subprocess.Popen(['xdotool', *map(str, args)]) as process:
        wait(lambda: process.poll() is not None)
        assert process.returncode == 0
    pump(.1)


with tempfile.TemporaryDirectory(prefix='nightreader-installed-') as tmp:
    os.chdir(tmp)
    os.environ['XDG_CONFIG_HOME'] = str(Path(tmp) / 'config')
    version = subprocess.check_output([str(root / 'nightreader'), '--version'], text=True)
    assert version.strip() == 'NightReader ' + nightread.__version__
    pdf = Path(tmp) / 'smoke $(literal) 中文.pdf'
    with fitz.open() as doc:
        for i in range(3):
            doc.new_page().insert_text((72, 100), f'SearchTarget page {i + 1}')
        doc.set_toc([[1, 'Original', 1]])
        doc.save(pdf)
    before = hashlib.sha256(pdf.read_bytes()).hexdigest()
    from nightread.window import MainWindow
    window = MainWindow(path=str(pdf))
    try:
        window.show_all()
        window.search_entry.set_property('im-module', 'gtk-im-context-simple')
        wait(lambda: window.page_count == 3 and window.bm.marks and window.cont_view._rendered)
        xdo('windowfocus', window.get_window().get_xid())
        window.cont_view.canvas.grab_focus()
        xdo('key', 'ctrl+f')
        assert window.search_entry.get_mapped()
        xdo('type', 'SearchTarget')
        wait(lambda: len(window._search_hits) == 3)
        xdo('key', 'F3')
        assert window.view.page_no == 1
        xdo('key', 'Escape')
        assert not window._search_hits and not window.searchbar.get_visible()
        assert hashlib.sha256(pdf.read_bytes()).hexdigest() == before
        window.bm.rename(0, 'Saved from installed package')
        window._mark_dirty()
        xdo('key', 'ctrl+s')
        wait(lambda: not window._dirty)
        with fitz.open(pdf) as doc:
            assert doc.get_toc()[0][1] == 'Saved from installed package'
            assert len(doc) == 3
        # The former repository-relative helper must also work after installation.
        result = subprocess.run([sys.executable, '-s', '-m', 'nightread.shrink',
                                 str(pdf), '--dry-run'], text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
    finally:
        window.destroy()
        pump()
    # Exercise the actual installed entry point and child-reader launch path.
    with open(Path(tmp) / 'launcher.log', 'w+') as log:
        process = subprocess.Popen([str(root / 'nightreader'), str(pdf), str(pdf)],
                                   stdout=log, stderr=log)
        try:
            def readers():
                result = subprocess.run(['xdotool', 'search', '--onlyvisible', '--name',
                                         'smoke.*literal'], capture_output=True, text=True)
                return result.stdout.split()
            wait(lambda: len(readers()) == 2)
            assert process.poll() is None
            for xid in readers():
                xdo('windowfocus', xid, 'key', '--clearmodifiers', 'ctrl+w')
            wait(lambda: process.poll() is not None and not readers())
            assert process.returncode == 0
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            log.seek(0)
            diagnostic = log.read()
            assert 'Traceback' not in diagnostic, diagnostic
print('Installed package: imports, CLI, Ctrl+F, navigation, save, helper, multiple windows OK')
