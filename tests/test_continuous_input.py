"""S2: XTest mouse/keyboard input must cross X11 before reaching GTK.

Run with xvfb-run -a python3 -m unittest discover -s tests
-p test_continuous_input.py -v. Never replace xdotool with widget.emit().
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import fitz
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib

from nightread import config
from nightread.window import MainWindow


@unittest.skipUnless(os.environ.get("DISPLAY") and shutil.which("xdotool"),
                     "requires X11 and xdotool")
class TestContinuousInput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nr_input_")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "long.pdf")
        with fitz.open() as doc:
            for i in range(256):
                page = doc.new_page(width=595, height=842)
                for y in (100, 250, 430, 650):
                    page.insert_text((72, y), f"Page {i + 1:03d} selection target", fontsize=16)
            doc.set_toc([[1, "Beginning", 1], [1, "Middle", 128], [1, "End", 256]])
            doc.save(self.path)
        cfg = dict(config.DEFAULTS, window_width=1100, window_height=800)
        for name, value in (("load", cfg), ("save", True)):
            mock = patch.object(config, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.w = MainWindow()
        self.addCleanup(self.w.destroy)
        self.w.show_all()
        self.w.open_file(self.path)
        self.cv = self.w.cont_view
        self.wait(lambda: self.w.page_count == 256 and self.cv._rendered)
        self.pump(.4)  # include legacy delayed restore
        self.xdo("windowfocus", str(self.w.get_window().get_xid()))

    def pump(self, seconds=.06):
        loop = GLib.MainLoop()
        GLib.timeout_add(max(1, int(seconds * 1000)), loop.quit)
        loop.run()

    def wait(self, condition, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump()
            if condition():
                return
        self.fail("Timed out waiting for GUI/worker state")

    def xdo(self, *args):
        subprocess.run(["xdotool", *map(str, args)], check=True, timeout=5)
        self.pump()

    def goto(self, pno):
        # Real page-entry keyboard input, including focus and Return.
        self.w.page_entry.grab_focus()
        self.xdo("key", "ctrl+a")
        self.xdo("type", str(pno + 1))
        self.xdo("key", "Return")
        self.wait(lambda: self.cv.page_no == pno and pno in self.cv._rendered)
        self.pump(.2)

    def screen_point(self, pno, x, y):
        # Use the displayed image geometry as the independent coordinate oracle.
        cv = self.cv
        win = cv.canvas.get_window()
        width = cv.canvas.get_allocated_width()
        _, ox, oy = win.get_origin()
        pix = cv._pixmaps[pno]
        left = max(0, (width - pix.width) / 2)
        return (round(ox + left + x * cv.zoom - cv.scroller.get_hadjustment().get_value()),
                round(oy + cv._page_top(pno) + y * cv.zoom - cv.scroller.get_vadjustment().get_value()))

    def drag(self, pno, y=100):
        start = self.screen_point(pno, 73, y - 10)
        end = self.screen_point(pno, 245, y + 2)
        self.xdo("mousemove", *start)
        self.xdo("mousedown", "1")
        self.xdo("mousemove", *end)
        self.xdo("mouseup", "1")
        self.wait(lambda: self.w._sel_anchor is not None)
        self.assertEqual(self.w._sel_page, pno + 1)

    def test_native_windows_remain_within_x11_limit(self):
        self.goto(127)
        seen = set()
        def walk(win):
            if win in seen:
                return
            seen.add(win)
            self.assertLessEqual(win.get_width(), 32767)
            self.assertLessEqual(win.get_height(), 32767)
            for child in win.get_children():
                walk(child)
        walk(self.w.get_window())
        self.assertGreater(self.cv.scroller.get_vadjustment().get_upper(), 32767)
        for x, y in ((80, 100), (250, 250), (430, 430)):
            self.xdo("mousemove", *self.screen_point(127, x, y))
            window, _, _ = Gdk.Window.at_pointer()
            self.assertEqual(window, self.cv.canvas.get_window())
            self.assertIsNotNone(window.get_cursor())

    def test_drag_and_save_on_first_middle_last_pages(self):
        for pno in (0, 68, 127, 255):
            with self.subTest(page=pno + 1):
                self.goto(pno)
                self.w._sel_anchor = None
                self.drag(pno)
                base = self.cv._pixmaps[pno]
                self.xdo("key", "ctrl+h")
                self.wait(lambda: self.w._dirty)
                self.wait(lambda: self.cv._pixmaps.get(pno) is not base)
                self.assertNotEqual(self.cv._pixmaps[pno].samples, base.samples)
                self.xdo("key", "ctrl+s")
                self.wait(lambda: not self.w._dirty)
        with fitz.open(self.path) as doc:
            self.assertEqual(len(doc), 256)
            self.assertEqual(len(doc.get_toc()), 3)
            for pno in (0, 68, 127, 255):
                page = doc[pno]
                annotations = list(page.annots())
                self.assertEqual(len(annotations), 1)
                self.assertEqual(annotations[0].type[1], "Highlight")
                self.assertLess(annotations[0].rect.y1, 110)

    def test_scroll_then_select_lower_body(self):
        self.goto(68)
        self.xdo("mousemove", *self.screen_point(68, 300, 100))
        old = self.cv.scroller.get_vadjustment().get_value()
        self.xdo("click", "--repeat", "4", "--delay", "80", "5")
        self.wait(lambda: self.cv.scroller.get_vadjustment().get_value() > old + 50)
        self.drag(68, y=430)

    def test_chosen_color_is_saved_and_highlight_matches_day_mode(self):
        import numpy as np
        self.w._apply_preferences(dict(self.w.cfg, highlight_opacity=.4))
        self.goto(68)
        self.drag(68)
        button = self.w.color_btn
        x, y = button.translate_coordinates(self.w, button.get_allocated_width() // 2,
                                            button.get_allocated_height() // 2)
        _, ox, oy = self.w.get_window().get_origin()
        self.xdo("mousemove", ox + x, oy + y)
        self.xdo("click", "1")  # 黄 → 绿
        self.xdo("key", "ctrl+h")
        self.wait(lambda: self.w._dirty and self.cv._highlight_masks.get(68))
        self.assertFalse(self.cv._selection_rects, "蓝色选区不应盖在实际高亮上")
        masks = self.cv._highlight_masks[68]
        mask = masks[0]
        x, y, w, h = (mask[k] for k in ("x", "y", "width", "height"))
        coverage = np.frombuffer(mask["alpha"], dtype=np.uint8).reshape(h, w) == mask["peak"]
        base = self.cv._pixmaps[68]
        day = np.frombuffer(base.samples, dtype=np.uint8).reshape(base.height, base.width, 3)
        for mode in ("invert", "soft", "off"):
            self.w.set_night_mode(mode)
            self.wait(lambda: 68 in self.cv._rendered)
            surface = self.cv._rendered[68]
            import sys
            raw = np.frombuffer(surface.get_data(), dtype=np.uint8).reshape(base.height, base.width, 4)
            shown = raw[:, :, 2::-1] if sys.byteorder == 'little' else raw[:, :, 1:]
            np.testing.assert_array_equal(shown[y:y+h, x:x+w][coverage], day[y:y+h, x:x+w][coverage])
        self.xdo("key", "ctrl+s")
        self.wait(lambda: not self.w._dirty)
        with fitz.open(self.path) as doc:
            page = doc[68]
            self.assertEqual(list(page.annots())[0].colors["stroke"], [0, 1, 0])
            self.assertAlmostEqual(list(page.annots())[0].opacity, .4, places=5)

    def test_horizontal_drag_selects_and_click_clears_old_selection(self):
        self.goto(68)
        self.xdo("mousemove", *self.screen_point(68, 73, 95))
        self.xdo("mousedown", "1")
        self.xdo("mousemove", *self.screen_point(68, 245, 95))
        self.xdo("mouseup", "1")
        self.wait(lambda: self.w._sel_anchor is not None)
        self.assertTrue(self.cv._selection_rects)
        self.xdo("click", "1")
        self.xdo("key", "ctrl+h")
        self.assertIsNone(self.w._sel_anchor)
        self.assertFalse(self.w._dirty)

    def test_zoom_resize_and_horizontal_scroll_keep_selection_aligned(self):
        self.goto(127)
        for key in ("ctrl+minus", "ctrl+0", "ctrl+plus"):
            self.xdo("key", key)
            self.wait(lambda: 127 in self.cv._rendered and not self.cv._pending)
            self.assertEqual(self.cv.page_no, 127)
            self.drag(127)
        self.w.resize(1000, 700)
        self.pump(.3)
        self.cv.scroller.get_hadjustment().set_value(50)
        self.pump()
        self.drag(127)
        self.assertEqual(self.w._sel_page, 128)
