"""区域选字:交集命中、视觉行顺序、真实 X11 预览/复制/保存的一致性。"""
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import fitz
from nightread import config
from nightread.textindex import build_page_index, rectangle_selection
import test_shortcuts as _shortcuts
import test_continuous_input as _input


def table(page, by_column=False):
    cells = [(row, col) for row in range(3) for col in range(3)]
    if by_column:
        cells.sort(key=lambda cell: (cell[1], cell[0]))
    for row, col in cells:
        page.insert_text(((72, 220, 400)[col], 100 + row * 30),
                         f"{('LEFT', 'COL', 'RIGHT')[col]} {row + 1}",
                         fontsize=(12, 20, 10)[row])


class TestRectangleSelection(unittest.TestCase):
    def index(self, by_column=False):
        with fitz.open() as doc:
            page = doc.new_page()
            table(page, by_column)
            return build_page_index(page, 1)

    def test_columns_keep_separate_ranges_instead_of_filling_between_endpoints(self):
        idx = self.index()
        ranges, text = rectangle_selection(idx, (219, 97), (300, 161))
        self.assertEqual(text, "COL 1\nCOL 2\nCOL 3")
        self.assertEqual(len(ranges), 3)
        selected = {i for a, b in ranges for i in range(a, b)}
        self.assertLess(len(selected), max(selected) - min(selected) + 1)
        self.assertEqual(rectangle_selection(idx, (300, 161), (219, 97)), (ranges, text))

    def test_copy_reorders_column_blocks_into_visual_rows(self):
        idx = self.index(by_column=True)
        _ranges, text = rectangle_selection(idx, (71, 90), (520, 165))
        self.assertEqual(text, "LEFT 1 COL 1 RIGHT 1\nLEFT 2 COL 2 RIGHT 2\nLEFT 3 COL 3 RIGHT 3")

    def test_partial_height_and_narrow_vertical_drag_do_not_omit_letters(self):
        idx = self.index()
        _ranges, text = rectangle_selection(idx, (219, 99), (300, 99))
        self.assertEqual(text, "COL 1")
        _ranges, text = rectangle_selection(idx, (221, 90), (221, 165))
        self.assertEqual(text, "C\nC\nC")

    def test_touching_a_small_strip_of_each_glyph_selects_the_whole_character(self):
        idx = self.index()
        for i, (x0, y0, x1, y1) in enumerate(idx.boxes):
            with self.subTest(character=i):
                ranges, _text = rectangle_selection(idx, (x0 + .2, y0 + .1),
                                                     (x1 - .2, y0 + .3))
                self.assertIn(i, {j for a, b in ranges for j in range(a, b)})

    def test_blank_rectangle_selects_nothing(self):
        self.assertEqual(rectangle_selection(self.index(), (530, 200), (580, 300)), ([], ""))


@unittest.skipUnless(os.environ.get("DISPLAY") and shutil.which("xdotool"), "requires X11")
class TestRectangleInput(unittest.TestCase):
    pump = _shortcuts.TestShortcuts.pump
    wait = _shortcuts.TestShortcuts.wait
    xdo = _shortcuts.TestShortcuts.xdo
    focus = _shortcuts.TestShortcuts.focus
    click = _shortcuts.TestShortcuts.click
    external_clipboard = _shortcuts.TestShortcuts.external_clipboard
    clipboard_marker = _shortcuts.TestShortcuts.clipboard_marker
    goto = _input.TestContinuousInput.goto
    screen_point = _input.TestContinuousInput.screen_point

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nr_rectangle_")
        self.addCleanup(self.tmp.cleanup)
        for key, value in (("CONFIG_DIR", self.tmp.name),
                           ("CONFIG_PATH", self.tmp.name + "/config.json")):
            mock = patch.object(config, key, value)
            mock.start()
            self.addCleanup(mock.stop)
        config.save(dict(config.DEFAULTS, window_width=1200, window_height=800))
        self.path = self.tmp.name + "/columns.pdf"
        with fitz.open() as doc:
            for pno in range(256):
                page = doc.new_page(width=595, height=842)
                if pno in (0, 68, 70, 127, 255):
                    table(page)
            doc.set_toc([[1, "Columns", 1]])
            doc.save(self.path)
        from nightread.window import MainWindow
        self.w = MainWindow()
        self.addCleanup(self.w.destroy)
        self.w.page_entry.set_property("im-module", "gtk-im-context-simple")
        self.w.show_all()
        self.w.open_file(self.path)
        self.cv = self.w.cont_view
        self.wait(lambda: self.w.page_count == 256 and self.cv._rendered)
        self.focus(self.w)

    def drag(self, pno, start=(219, 97), end=(300, 161), reverse=False, wait=True):
        a, b = self.screen_point(pno, *start), self.screen_point(pno, *end)
        if reverse:
            a, b = b, a
        self.xdo("mousemove", *a, "mousedown", "1", "mousemove", *b)
        if wait:
            self.wait(lambda: self.w._sel_ranges and not self.w._selection_inflight)
            preview = list(self.cv._selection_rects)
        self.xdo("mouseup", "1")
        if wait:
            self.wait(lambda: self.w._sel_ranges and not self.w._selection_inflight)
            self.assertEqual(self.cv._selection_rects, preview)
            return preview

    def test_column_preview_copy_and_saved_quads_on_long_document(self):
        self.click(self.w.selection_btn)
        self.assertEqual(self.cv.selection_mode, "rectangle")
        for pno in (0, 68, 127, 255):
            with self.subTest(page=pno + 1):
                self.goto(pno)
                preview = self.drag(pno, reverse=pno % 2 == 1)
                self.assertEqual(len(self.w._sel_ranges), 3)
                self.xdo("key", "ctrl+c")
                self.assertEqual(self.external_clipboard(), "COL 1\nCOL 2\nCOL 3")
                self.xdo("key", "ctrl+h")
                self.wait(lambda: self.w._dirty)
                self.xdo("key", "ctrl+s")
                self.wait(lambda: not self.w._dirty)
                with fitz.open(self.path) as doc:
                    page = doc[pno]
                    annots = list(page.annots())
                    self.assertEqual(len(annots), 1)
                    vertices = annots[0].vertices
                    quads = [fitz.Quad(vertices[i:i + 4]).rect for i in range(0, len(vertices), 4)]
                    self.assertEqual([[round(x, 2) for x in rect] for rect in quads], preview)
                    self.assertEqual(len(quads), 3)
                    for x0, y0, x1, y1, word, *_ in page.get_text("words"):
                        center = fitz.Point((x0 + x1) / 2, (y0 + y1) / 2)
                        self.assertEqual(any(center in rect for rect in quads), 200 < x0 < 320)
        print("\nRectangle XTest preview/copy/save pages: [1, 69, 128, 256]; 3 column quads each")

    def test_switch_mode_cancels_inflight_selection_and_restores_continuous_selection(self):
        self.cv.canvas.grab_focus()
        self.xdo("key", "ctrl+shift+r")
        self.assertEqual(self.cv.selection_mode, "rectangle")
        self.assertEqual(config.load()["selection_mode"], "rectangle")
        original = self.w.worker._op_select_text
        def delayed(**kwargs):
            time.sleep(.4)
            return original(**kwargs)
        self.clipboard_marker()
        with patch.object(self.w.worker, "_op_select_text", side_effect=delayed):
            self.drag(0, wait=False)
            self.xdo("key", "ctrl+c", "ctrl+shift+r")
            self.wait(lambda: not self.w._selection_inflight)
        self.assertEqual(self.cv.selection_mode, "text")
        self.assertFalse(self.cv._selection_rects)
        self.assertIsNone(self.w._sel_ranges)
        self.assertEqual(self.external_clipboard(), "previous clipboard")
        self.drag(0)
        self.xdo("key", "ctrl+c")
        copied = self.external_clipboard()
        self.assertIn("RIGHT 1", copied)
        self.assertIn("LEFT 2", copied)
        self.assertEqual(config.load()["selection_mode"], "text")
