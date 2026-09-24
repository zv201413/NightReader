"""快捷键回归:真实 XTest 输入,配置与 PDF 均隔离在 /tmp。"""
import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import fitz
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk

from nightread import config, shortcuts


class TestShortcutConfig(unittest.TestCase):
    def test_headless_load_keeps_control_and_rejects_conflicts(self):
        env = dict(os.environ)
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        result = subprocess.run([sys.executable, "-c", """
import json
from nightread import config, shortcuts
print(json.dumps(config.validated({'shortcuts': {'close': ['<Primary>q']}})['shortcuts']))
assert config.validated({'shortcuts': {'close': ['<Control>s']}})['shortcuts'] == {}
assert shortcuts.effective({'close': []})['close'] == []
"""], env=env, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), {"close": ["<Control>q"]})
        self.assertEqual(result.stderr, "")


@unittest.skipUnless(os.environ.get("DISPLAY") and shutil.which("xdotool"), "requires X11")
class TestShortcuts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nr_shortcuts_")
        self.addCleanup(self.tmp.cleanup)
        for key, value in (("CONFIG_DIR", self.tmp.name),
                           ("CONFIG_PATH", self.tmp.name + "/config.json")):
            mock = patch.object(config, key, value)
            mock.start()
            self.addCleanup(mock.stop)
        config.save(config.DEFAULTS)
        self.path = self.tmp.name + "/book.pdf"
        with fitz.open() as doc:
            for i in range(4):
                page = doc.new_page()
                page.insert_text((72, 100), f"Keyboard testing page {i + 1}")
                page.insert_text((72, 130), "Second line with spaces")
                page.insert_text((72, 160), "中文复制 测试 ABC", fontname="china-s")
            doc.set_toc([[1, "Original title", 1]])
            doc.save(self.path)
        self.w = self.reader()

    def pump(self, seconds=.08):
        loop = GLib.MainLoop()
        GLib.timeout_add(max(1, int(seconds * 1000)), loop.quit)
        loop.run()

    def wait(self, condition, timeout=6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            if condition():
                return
        self.fail("Timed out waiting for GUI/worker state")

    def xdo(self, *args):
        # 输入期间也处理 GTK/输入法事件,不把整段按键堆到阻塞的 UI 队列里。
        with subprocess.Popen(["xdotool", *map(str, args)]) as process:
            deadline = time.monotonic() + 5
            while process.poll() is None:
                self.pump(.01)
                if time.monotonic() > deadline:
                    process.kill()
                    self.fail("xdotool timed out")
            self.assertEqual(process.returncode, 0)
        self.pump()

    def focus(self, window):
        self.pump()  # 等待 show_all 的 X11 map 请求实际发出。
        self.xdo("windowraise", window.get_window().get_xid(),
                 "windowfocus", window.get_window().get_xid())

    def reader(self):
        from nightread.window import MainWindow
        window = MainWindow()
        window.page_entry.set_property("im-module", "gtk-im-context-simple")
        window.search_entry.set_property("im-module", "gtk-im-context-simple")
        self.addCleanup(window.destroy)
        window.show_all()
        window.open_file(self.path)
        self.wait(lambda: window.bm.marks and window.cont_view._rendered)
        self.focus(window)
        window.cont_view.canvas.grab_focus()
        return window

    def click(self, widget):
        window = widget.get_toplevel()
        x, y = widget.translate_coordinates(window, widget.get_allocated_width() // 2,
                                            widget.get_allocated_height() // 2)
        _, ox, oy = window.get_window().get_origin()
        self.xdo("mousemove", ox + x, oy + y, "click", "1")

    def test_ctrl_f_search_input_navigation_and_reopen(self):
        self.assertFalse(self.w.searchbar.get_visible())
        self.xdo("key", "ctrl+f")
        self.assertTrue(self.w.search_entry.get_mapped(),
                        "Ctrl+F must display the input, not just its container")
        self.assertIs(self.w.get_focus(), self.w.search_entry)
        self.xdo("type", "Keyboard")
        self.wait(lambda: len(self.w._search_hits) == 4)
        self.assertTrue(self.w.search_label.get_mapped())
        self.assertTrue(self.w.search_label.get_text().startswith("1/4"))
        self.assertTrue(self.w.cont_view._overlay_rects)
        self.xdo("key", "F3")
        self.assertTrue(self.w.search_label.get_text().startswith("2/4"))
        self.assertEqual(self.w.view.page_no, 1)
        self.xdo("key", "shift+F3")
        self.assertEqual(self.w.view.page_no, 0)
        self.xdo("key", "Return")
        self.assertEqual(self.w.view.page_no, 1)
        self.xdo("key", "Escape")
        self.assertFalse(self.w.searchbar.get_visible())
        self.assertFalse(self.w._search_hits)
        self.assertFalse(self.w.cont_view._overlay_rects)
        self.xdo("key", "ctrl+f")
        self.assertTrue(self.w.search_entry.get_mapped())
        self.assertIs(self.w.get_focus(), self.w.search_entry)
        self.wait(lambda: len(self.w._search_hits) == 4)
        self.xdo("key", "ctrl+a")
        self.xdo("type", "absent-query")
        self.wait(lambda: self.w.search_label.get_text().startswith("0 处"))
        self.assertFalse(self.w.cont_view._overlay_rects)

    def test_toolbar_search_controls_are_visible(self):
        self.click(self.w.search_toggle)
        self.assertTrue(self.w.searchbar.get_visible())
        for widget in self.w.searchbar.get_children():
            self.assertTrue(widget.get_mapped())
        self.assertIs(self.w.get_focus(), self.w.search_entry)
        self.click(self.w.search_toggle)
        self.assertFalse(self.w.searchbar.get_visible())
        self.w.show_all()
        self.assertFalse(self.w.searchbar.get_visible())

    def test_search_after_selection_covers_all_pages(self):
        self.select_text()
        self.assertTrue(self.w._sel_text)
        self.xdo("key", "ctrl+f")
        self.xdo("type", "Keyboard")
        self.wait(lambda: self.w.search_label.get_text())
        self.assertEqual([hit["page"] for hit in self.w._search_hits], [1, 2, 3, 4])

    def test_escape_cancels_pending_search(self):
        from nightread.docworker import SEARCH
        self.xdo("key", "ctrl+f")
        with patch.object(self.w.worker, "submit", wraps=self.w.worker.submit) as submit:
            self.w.search_entry.set_text("Keyboard")
            self.xdo("key", "Escape")
            self.pump(.4)
        self.assertFalse(self.w.searchbar.get_visible())
        self.assertFalse(self.w._search_hits)
        self.assertFalse(self.w.cont_view._overlay_rects)
        self.assertFalse(any(call.args[0].kind == SEARCH for call in submit.call_args_list))

    def cell(self, tree, row, column):
        path = Gtk.TreePath.new_from_indices([row])
        col = tree.get_column(column)
        tree.scroll_to_cell(path, col, False, 0, 0)
        self.pump()
        rect = tree.get_cell_area(path, col)
        _, ox, oy = tree.get_bin_window().get_origin()
        self.xdo("mousemove", ox + rect.x + rect.width // 2,
                 oy + rect.y + rect.height // 2, "click", "--repeat", "2", "--delay", "100", "1")

    def edit_bookmark(self):
        self.cell(self.w.tree, 0, 0)
        self.assertIsInstance(self.w.get_focus(), Gtk.Entry)
        self.w.get_focus().set_property("im-module", "gtk-im-context-simple")
        self.xdo("key", "ctrl+a")
        self.xdo("type", "Saved by close shortcut")
        self.xdo("key", "Return")
        self.wait(lambda: self.w._dirty)
        self.assertEqual(self.w.bm.marks[0].title, "Saved by close shortcut")

    def close_with_answer(self, response, wait_saved=False):
        seen = []
        deadline = time.monotonic() + 5
        def answer():
            dialogs = [w for w in Gtk.Window.list_toplevels()
                       if isinstance(w, Gtk.MessageDialog) and w.get_visible()]
            if not dialogs:
                return time.monotonic() < deadline
            if wait_saved and self.w._dirty and time.monotonic() < deadline:
                return True
            dialog = dialogs[0]
            seen.append(dialog.get_title())
            # XTest click enters the nested Gtk.Dialog.run() event loop.
            self.click(dialog.get_widget_for_response(response))
            return False
        GLib.timeout_add(30, answer)
        self.focus(self.w)
        self.xdo("key", "ctrl+w")
        self.assertEqual(seen, ["未保存的修改"])

    def panel(self):
        from nightread.settings import SettingsWindow
        panel = SettingsWindow()  # 独立窗口:通过文件监视器通知阅读窗口。
        self.addCleanup(panel.destroy)
        panel.show_all()
        self.focus(panel)
        self.click(panel.tabs.get_tab_label(panel.tabs.get_nth_page(1)))
        self.assertEqual(panel.tabs.get_current_page(), 1)
        return panel

    def record(self, panel, action, key, slot=0):
        self.cell(panel.shortcut_tree, list(shortcuts.ACTIONS).index(action), slot + 1)
        self.assertTrue(panel._editing_shortcut)
        self.focus(panel._shortcut_dialog)
        self.xdo("key", key)
        self.wait(lambda: not panel._editing_shortcut)
        self.focus(panel)

    def select_text(self, reverse=False, wait=True, partial=False):
        self.focus(self.w)
        with fitz.open(self.path) as doc:
            lines = [line for block in doc[0].get_text("dict")["blocks"]
                     for line in block.get("lines", [])]
            words = doc[0].get_text("words")
        if partial:
            first = next(word[:4] for word in words if word[4] == "line")
            last = next(word[:4] for word in words if word[4] == "with")
        else:
            first, last = lines[0]["bbox"], lines[2]["bbox"]
        cv = self.w.cont_view
        _, ox, oy = cv.canvas.get_window().get_origin()
        left = max(0, (cv.canvas.get_allocated_width() - cv._pixmaps[0].width) / 2)
        def point(x, y):
            return (round(ox + left + x * cv.zoom - cv.scroller.get_hadjustment().get_value()),
                    round(oy + y * cv.zoom - cv.scroller.get_vadjustment().get_value()))
        start = point(first[0] + .5, (first[1] + first[3]) / 2)
        end = point(last[2] - .5, (last[1] + last[3]) / 2)
        if reverse:
            start, end = end, start
        self.xdo("mousemove", *start, "mousedown", "1", "mousemove", *end, "mouseup", "1")
        if wait:
            self.wait(lambda: self.w._sel_text and not self.w._selection_inflight)

    def external_clipboard(self):
        # 独立进程向 X11 请求剪贴板,主进程持续处理 selection-request。
        code = """import json,gi
gi.require_version('Gtk','3.0')
from gi.repository import Gtk,Gdk
print(json.dumps(Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text()))
"""
        with subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as process:
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                self.pump(.02)
            if process.poll() is None:
                process.kill()
                self.fail("Clipboard request timed out")
            out, err = process.communicate()
            self.assertEqual(process.returncode, 0, err)
            return json.loads(out)

    def clipboard_marker(self):
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("previous clipboard", -1)

    def test_copy_multiline_unicode_forward_reverse_and_paste(self):
        expected = "Keyboard testing page 1\nSecond line with spaces\n中文复制 测试 ABC"
        with open(self.path, "rb") as source:
            before = hashlib.sha256(source.read()).hexdigest()
        for reverse in (False, True):
            self.select_text(reverse=reverse)
            self.xdo("key", "ctrl+c")
            self.assertEqual(self.external_clipboard(), expected)
        paste_window = Gtk.Window()
        self.addCleanup(paste_window.destroy)
        textview = Gtk.TextView()
        paste_window.add(textview)
        paste_window.show_all()
        self.focus(paste_window)
        textview.grab_focus()
        self.xdo("key", "ctrl+v")
        buffer = textview.get_buffer()
        self.assertEqual(buffer.get_text(*buffer.get_bounds(), True), expected)
        self.assertFalse(self.w._dirty)
        with open(self.path, "rb") as source:
            self.assertEqual(hashlib.sha256(source.read()).hexdigest(), before)

    def test_copy_empty_or_cleared_selection_preserves_clipboard(self):
        self.clipboard_marker()
        self.xdo("key", "ctrl+c")
        self.assertEqual(self.external_clipboard(), "previous clipboard")

        self.select_text()
        self.xdo("key", "Escape", "ctrl+c")
        self.assertEqual(self.external_clipboard(), "previous clipboard")
        self.select_text()
        self.xdo("key", "Page_Down", "ctrl+c")
        self.assertEqual(self.w.view.page_no, 1)
        self.assertEqual(self.external_clipboard(), "previous clipboard")

    def test_copy_only_selected_words_in_both_directions(self):
        for reverse in (False, True):
            self.select_text(reverse=reverse, partial=True)
            self.xdo("key", "ctrl+c")
            self.assertEqual(self.external_clipboard(), "line with")

    def test_copy_waits_for_selection_and_cancel_discards_pending_copy(self):
        original_select = self.w.worker._op_select_text
        def slow_select(**kwargs):
            time.sleep(.4)
            return original_select(**kwargs)
        with patch.object(self.w.worker, "_op_select_text", side_effect=slow_select):
            self.select_text(wait=False)
            self.xdo("key", "ctrl+c")
            self.assertIsNotNone(self.w._copy_pending)
            self.wait(lambda: not self.w._selection_inflight and self.w._copy_pending is None)
            self.assertEqual(self.external_clipboard(),
                             "Keyboard testing page 1\nSecond line with spaces\n中文复制 测试 ABC")
            self.clipboard_marker()
            self.select_text(wait=False)
            self.xdo("key", "ctrl+c", "Escape")
            self.wait(lambda: not self.w._selection_inflight)
            self.assertEqual(self.external_clipboard(), "previous clipboard")

    def test_custom_copy_binding_and_entry_copy(self):
        panel = self.panel()
        self.record(panel, "copy", "ctrl+shift+c")
        self.click(panel.save_button)
        self.wait(lambda: self.w._bindings["copy"] == ["<Control><Shift>c"])
        self.select_text()
        self.clipboard_marker()
        self.xdo("key", "ctrl+c")
        self.assertEqual(self.external_clipboard(), "previous clipboard")
        self.xdo("key", "ctrl+shift+c")
        self.assertEqual(self.external_clipboard(),
                         "Keyboard testing page 1\nSecond line with spaces\n中文复制 测试 ABC")
        self.w.page_entry.grab_focus()
        self.xdo("key", "ctrl+a")
        self.xdo("type", "entry text")
        self.xdo("key", "ctrl+a", "ctrl+c")
        self.assertEqual(self.external_clipboard(), "entry text")

    def test_ctrl_w_closes_clean_reader_from_text_entry(self):
        self.w.page_entry.grab_focus()
        self.xdo("key", "ctrl+w")
        self.wait(lambda: not self.w.get_visible())

    def test_dirty_close_cancel_and_discard(self):
        self.edit_bookmark()
        self.close_with_answer(Gtk.ResponseType.CANCEL)
        self.assertTrue(self.w.get_visible())
        self.assertTrue(self.w._dirty)
        self.close_with_answer(Gtk.ResponseType.REJECT)
        self.wait(lambda: not self.w.get_visible())
        with fitz.open(self.path) as doc:
            self.assertEqual(doc.get_toc()[0][1], "Original title")

    def test_save_and_close_writes_before_window_destruction(self):
        self.edit_bookmark()
        before = os.path.getsize(self.path)
        self.close_with_answer(Gtk.ResponseType.ACCEPT)
        self.wait(lambda: not self.w.get_visible())
        with fitz.open(self.path) as doc:
            self.assertEqual(len(doc), 4)
            self.assertEqual(doc.get_toc()[0][1], "Saved by close shortcut")
        self.assertGreater(os.path.getsize(self.path), before)
        self.assertFalse(self.w._dirty)

    def test_failed_save_leaves_window_dirty_and_allows_retry(self):
        self.edit_bookmark()
        with patch.object(self.w.worker, "_op_save", side_effect=OSError("test disk full")):
            self.close_with_answer(Gtk.ResponseType.ACCEPT)
            self.wait(lambda: not self.w._close_after_save)
        self.assertTrue(self.w.get_visible())
        self.assertTrue(self.w._dirty)
        self.assertTrue(self.w.get_sensitive())
        with fitz.open(self.path) as doc:
            self.assertEqual(doc.get_toc()[0][1], "Original title")
        self.close_with_answer(Gtk.ResponseType.ACCEPT)
        self.wait(lambda: not self.w.get_visible())
        with fitz.open(self.path) as doc:
            self.assertEqual(doc.get_toc()[0][1], "Saved by close shortcut")

    def test_existing_save_finishes_while_close_prompt_is_open(self):
        self.edit_bookmark()
        original_save = self.w.worker._op_save
        def slow_save():
            time.sleep(.5)
            return original_save()
        with patch.object(self.w.worker, "_op_save", side_effect=slow_save):
            self.xdo("key", "ctrl+s")
            self.close_with_answer(Gtk.ResponseType.ACCEPT, wait_saved=True)
            self.wait(lambda: not self.w.get_visible())
        with fitz.open(self.path) as doc:
            self.assertEqual(doc.get_toc()[0][1], "Saved by close shortcut")

    def test_custom_close_conflict_recording_clear_cancel_reset_and_persist(self):
        panel = self.panel()
        self.record(panel, "close", "ctrl+s")
        self.assertIn("已用于", panel.status.get_text())
        self.assertEqual(panel._bindings["close"], ["<Control>w"])
        self.record(panel, "close", "ctrl+w")
        self.assertTrue(panel.get_visible(), "录入 Ctrl+W 时不能关闭面板")
        self.record(panel, "close", "ctrl+q")
        self.record(panel, "close", "Escape")
        self.assertEqual(panel._bindings["close"], ["<Control>q"])
        self.record(panel, "close", "ctrl+w", slot=1)
        self.assertEqual(panel._bindings["close"], ["<Control>q", "<Control>w"])
        self.record(panel, "close", "BackSpace", slot=1)
        self.assertEqual(panel._bindings["close"], ["<Control>q"])
        self.click(panel.save_button)
        self.wait(lambda: self.w._bindings["close"] == ["<Control>q"])
        self.focus(self.w)
        self.xdo("key", "ctrl+w")
        self.assertTrue(self.w.get_visible())
        self.xdo("key", "ctrl+q")
        self.wait(lambda: not self.w.get_visible())
        self.assertEqual(config.load()["shortcuts"]["close"], ["<Control>q"])
        reopened = self.reader()
        self.xdo("key", "ctrl+q")
        self.wait(lambda: not reopened.get_visible())
        self.focus(panel)
        self.click(panel.reset_shortcuts_button)
        self.assertEqual(panel._bindings, shortcuts.effective())
        self.click(panel.save_button)
        self.xdo("key", "ctrl+w")
        self.wait(lambda: not panel.get_visible())

    def test_editing_keys_do_not_turn_pages_and_reader_keys_still_work(self):
        self.w.page_entry.grab_focus()
        self.xdo("key", "ctrl+a")
        self.xdo("type", "123")
        for key, position in (("Home", 0), ("Right", 1), ("End", 3), ("Left", 2)):
            self.xdo("key", key)
            self.assertEqual(self.w.page_entry.get_position(), position)
            self.assertEqual(self.w.view.page_no, 0)
        mode = self.w.view.night_mode
        self.xdo("type", "d")
        self.assertEqual(self.w.page_entry.get_text(), "12d3")
        self.assertEqual(self.w.view.night_mode, mode)
        self.w.cont_view.canvas.grab_focus()
        self.xdo("key", "Right")
        self.wait(lambda: self.w.view.page_no == 1)
        self.xdo("key", "d")
        self.assertNotEqual(self.w.view.night_mode, mode)
        self.xdo("key", "ctrl+comma")
        self.wait(lambda: self.w._settings_window is not None)
        self.assertTrue(self.w._settings_window.get_visible())

    def test_modifier_distinctions_caps_lock_and_zoom_alias(self):
        self.edit_bookmark()
        self.w.cont_view.canvas.grab_focus()
        self.xdo("key", "ctrl+shift+s")
        self.assertTrue(self.w._dirty)
        with fitz.open(self.path) as doc:
            self.assertEqual(doc.get_toc()[0][1], "Original title")
        self.xdo("key", "Caps_Lock")
        try:
            self.xdo("key", "ctrl+s")
            self.wait(lambda: not self.w._dirty)
        finally:
            self.xdo("key", "Caps_Lock")
        before = self.w.view.zoom
        self.xdo("key", "ctrl+plus")
        self.assertGreater(self.w.view.zoom, before)
        self.xdo("key", "ctrl+minus")
        self.assertAlmostEqual(self.w.view.zoom, before)

    def test_disabled_binding_and_super_shortcut(self):
        panel = self.panel()
        self.record(panel, "close", "BackSpace")
        self.click(panel.save_button)
        self.wait(lambda: self.w._bindings["close"] == [])
        self.focus(self.w)
        self.xdo("key", "ctrl+w")
        self.assertTrue(self.w.get_visible())
        self.focus(panel)
        self.record(panel, "close", "super+q")
        self.assertIn("<Super>", panel._bindings["close"][0])
        self.click(panel.save_button)
        self.wait(lambda: self.w._bindings["close"] == panel._bindings["close"])
        self.focus(self.w)
        self.xdo("key", "q")
        self.assertTrue(self.w.get_visible())
        self.xdo("key", "super+q")
        self.wait(lambda: not self.w.get_visible())
