"""Multiple real reader processes on one display/session bus; all data in /tmp."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import fitz
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib

from nightread import config

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("DISPLAY") and shutil.which("xdotool"), "requires X11")
class TestMultipleReaders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nr_multiwindow_")
        self.addCleanup(self.tmp.cleanup)
        self.env = dict(os.environ, XDG_CONFIG_HOME=self.tmp.name + "/config")
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {"XDG_CONFIG_HOME": self.env["XDG_CONFIG_HOME"]}).start()
        patch.object(config, "CONFIG_DIR", self.env["XDG_CONFIG_HOME"] + "/nightread").start()
        patch.object(config, "CONFIG_PATH", config.CONFIG_DIR + "/config.json").start()
        self.assertTrue(config.save(config.DEFAULTS))
        self.paths = []
        for title in ("first book.pdf", "第二本 $(literal) 'book'.pdf", "third.pdf"):
            path = str(Path(self.tmp.name) / title)
            with fitz.open() as doc:
                for pno in range(5):
                    doc.new_page().insert_text((72, 100), f"Page {pno + 1}")
                doc.set_toc([[1, title, 1]])
                doc.save(path)
            self.paths.append(path)
        self.hashes = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in self.paths}
        self.addCleanup(self.check_pdfs_unchanged)
        self.processes = []
        self.addCleanup(self.stop_readers)
        self.local_windows = []
        self.addCleanup(self.stop_local_windows)
        self.log = open(Path(self.tmp.name) / "readers.log", "w+")
        self.addCleanup(self.log.close)

    def check_pdfs_unchanged(self):
        for path, digest in self.hashes.items():
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)

    def stop_local_windows(self):
        for window in self.local_windows:
            window.destroy()
        self.pump(.1)

    def stop_readers(self):
        for process in self.processes:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)

    def pump(self, seconds=.04):
        loop = GLib.MainLoop()
        GLib.timeout_add(max(1, int(seconds * 1000)), loop.quit)
        loop.run()

    def wait(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            value = condition()
            if value:
                return value
        self.fail("Timed out waiting for reader state")

    def start(self, *paths):
        process = subprocess.Popen([sys.executable, "-m", "nightread", *paths],
                                   cwd=ROOT, env=self.env, stdout=self.log,
                                   stderr=self.log, start_new_session=True)
        self.processes.append(process)
        return process

    def windows(self, path):
        title = re.escape(Path(path).name + " — nightread")
        result = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", "^" + title + "$"],
                                capture_output=True, text=True)
        return result.stdout.split()

    def close_reader(self, xid):
        # Send the actual Ctrl+W shortcut, without relying on a window manager.
        process = subprocess.Popen(["xdotool", "windowfocus", "--sync", xid,
                                    "key", "--clearmodifiers", "ctrl+w"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait(lambda: process.poll() is not None)
        self.assertEqual(process.returncode, 0)

    def local_reader(self, path=None):
        from nightread.window import MainWindow
        window = MainWindow(path=path)
        self.local_windows.append(window)
        window.show_all()
        if path:
            self.wait(lambda: window._path == path and window.bm.marks
                      and window.cont_view._rendered)
        return window

    def choose(self, window, paths):
        with patch("nightread.window.Gtk.FileChooserDialog") as picker:
            picker.return_value.run.return_value = Gtk.ResponseType.ACCEPT
            picker.return_value.get_filenames.return_value = list(paths)
            # Exercise the real handler and real launch_reader, but retain process
            # handles so teardown never touches another user's reader process.
            spawned = []
            def record_spawn(argv, **kwargs):
                process = subprocess.Popen(argv, cwd=kwargs["working_directory"],
                                           env=self.env, stdout=self.log, stderr=self.log,
                                           start_new_session=True)
                self.processes.append(process)
                spawned.append(argv)
                return process.pid, None, None, None
            with patch("nightread.app.GLib.spawn_async", side_effect=record_spawn):
                window._on_open_clicked(None)
            picker.return_value.set_select_multiple.assert_called_once_with(True)
            return spawned

    def test_repeated_cli_launch_keeps_both_windows_and_close_is_independent(self):
        first = self.start(self.paths[0])
        first_xid = self.wait(lambda: self.windows(self.paths[0]))[0]
        second = self.start(self.paths[1])
        self.wait(lambda: self.windows(self.paths[1]))
        self.assertIsNone(first.poll())
        self.assertIsNone(second.poll())
        self.assertNotEqual(first.pid, second.pid)
        self.close_reader(first_xid)
        self.wait(lambda: first.poll() is not None)
        self.assertEqual(first.returncode, 0)
        self.assertTrue(self.windows(self.paths[1]))
        self.assertIsNone(second.poll())

    def test_multi_file_cli_does_not_drop_arguments_and_children_outlive_first(self):
        parent = self.start(self.tmp.name + "/missing.pdf", *self.paths)
        for path in self.paths:
            self.wait(lambda p=path: self.windows(p))
        xids = [self.windows(p)[0] for p in self.paths]
        pids = [subprocess.check_output(["xdotool", "getwindowpid", xid], text=True).strip()
                for xid in xids]
        self.assertEqual(len(set(pids)), 3)
        self.close_reader(xids[0])
        self.wait(lambda: parent.poll() is not None)
        self.assertEqual(parent.returncode, 0)
        for path in self.paths[1:]:
            self.assertTrue(self.windows(path))
        # These windows were launched by the real GLib.spawn_async path.
        for xid in xids[1:]:
            self.close_reader(xid)
        self.wait(lambda: all(not self.windows(p) for p in self.paths))

    def test_open_dialog_keeps_dirty_document_and_opens_every_selected_file(self):
        window = self.local_reader(self.paths[0])
        window.bm.rename(0, "unsaved title")
        window._mark_dirty()
        window.view.goto(2)
        self.pump(.2)
        spawned = self.choose(window, self.paths[1:])
        for path in self.paths[1:]:
            self.wait(lambda p=path: self.windows(p))
        self.assertEqual([args[-1] for args in spawned], self.paths[1:])
        self.assertEqual(window._path, self.paths[0])
        self.assertTrue(window._dirty)
        self.assertEqual(window.bm.marks[0].title, "unsaved title")
        self.assertEqual(window.view.page_no, 2)

    def test_empty_window_uses_first_selection_and_opens_rest_separately(self):
        window = self.local_reader()
        spawned = self.choose(window, self.paths[:2])
        self.wait(lambda: window._path == self.paths[0] and window.bm.marks)
        self.wait(lambda: self.windows(self.paths[1]))
        self.assertEqual(len(spawned), 1)
        self.assertEqual(window.get_title(), Path(self.paths[0]).name + " — nightread")

    def test_open_other_book_starts_at_first_page_but_same_book_restores(self):
        self.assertTrue(config.update({"last_file": self.paths[0], "last_page": 3}))
        window = self.local_reader(self.paths[1])
        self.wait(lambda: window._restore_id == 0)
        self.assertEqual(window.view.page_no, 0)
        window.destroy()
        self.local_windows.remove(window)
        self.assertTrue(config.update({"last_file": self.paths[0], "last_page": 3}))
        window = self.local_reader(self.paths[0])
        self.wait(lambda: window._restore_id == 0 and window.view.page_no == 3)


class TestConcurrentPreferences(unittest.TestCase):
    def test_concurrent_process_updates_keep_all_keys_and_valid_json(self):
        with tempfile.TemporaryDirectory(prefix="nr_multi_config_") as tmp:
            env = dict(os.environ, XDG_CONFIG_HOME=tmp)
            keys = {"night_mode": "off", "highlight_color": "绿", "sidebar_visible": False,
                    "remember_position": False, "selection_mode": "rectangle"}
            code = """
import json, sys
from pathlib import Path
from nightread import config
# Hold the lock long enough to exercise simultaneous read/modify/write calls.
original_load = config.load
def delayed_load():
    import time
    result = original_load()
    time.sleep(.05)
    return result
config.load = delayed_load
for _ in range(4):
    assert config.set_(sys.argv[1], json.loads(sys.argv[2]))
"""
            procs = [subprocess.Popen([sys.executable, "-c", code, key, json.dumps(value)],
                                      env=env, cwd=ROOT, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True)
                     for key, value in keys.items()]
            for process in procs:
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stdout + stderr)
            result = json.loads((Path(tmp) / "nightread/config.json").read_text())
            for key, value in keys.items():
                self.assertEqual(result[key], value)
            self.assertFalse(list((Path(tmp) / "nightread").glob("*.tmp")))
