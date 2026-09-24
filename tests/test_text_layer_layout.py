"""Text-only rendering must retain glyph geometry, including invisible OCR.

The oracle is MuPDF's normal rendering of a visible, undecorated twin PDF.
Fixtures are generated in a temporary directory; no personal PDFs are changed.
"""
import hashlib
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import fitz
import numpy as np
from gi.repository import GLib

from nightread import docworker


def make_page(path, mode=0, rotation=0, crop=False, decorations=False):
    with fitz.open() as doc:
        page = doc.new_page(width=595, height=842)
        if decorations:
            image = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
            image.clear_with(100)
            page.insert_image(page.rect, pixmap=image)
            page.draw_rect((10, 10, 585, 832), color=(0, 0, 0), width=5)
        for point, text, font, size in (
            ((50, 100), 'i' * 100 + ' END', 'helv', 20),
            ((50, 200), '2.1.1 岩土工程勘察', 'china-s', 14),
            ((265, 200), 'geotechnical investigation', 'heit', 14),
            ((50, 350), 'Wide W, narrow i, punctuation: 1.23!', 'tiro', 17),
            ((50, 700), 'Rotation target', 'helv', 20),
        ):
            page.insert_text(point, text, fontname=font, fontsize=size,
                             render_mode=mode, color=(0, 0, 0))
        page.insert_text((50, 450), 'Scaled spacing 0123456789', fontsize=18,
                         morph=(fitz.Point(50, 450), fitz.Matrix(.65, 1.1)),
                         render_mode=mode)
        if crop:
            page.set_cropbox(fitz.Rect(20, 40, 575, 790))
        page.set_rotation(rotation)
        doc.save(path)


class TestTextLayerLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nr_text_layout_')
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / 'source.pdf'
        self.reference = Path(self.tmp.name) / 'reference.pdf'
        self.worker = docworker.DocWorker()
        self.worker.start()
        self.addCleanup(self.worker.stop)

    def run_task(self, kind, **kwargs):
        results = []
        self.worker.submit(docworker.Task(kind, kwargs, callback=results.append))
        deadline = time.monotonic() + 10
        context = GLib.MainContext.default()
        while not results and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(.005)
        self.assertTrue(results, f'{kind}: no worker callback')
        self.assertTrue(results[0].ok, str(results[0].error))
        return results[0].value

    def compare_to_visible(self, mode=3, rotation=0, crop=False, zoom=1.5):
        # Close first so subtests can replace only their own temporary fixture.
        self.run_task(docworker.CLOSE)
        make_page(self.source, mode, rotation, crop, decorations=True)
        make_page(self.reference, {1: 1, 2: 2, 5: 1, 6: 2}.get(mode, 0), rotation, crop)
        before = hashlib.sha256(self.source.read_bytes()).digest()
        self.run_task(docworker.OPEN, path=str(self.source))
        actual = self.run_task(docworker.RENDER_TEXT, page_no=0, zoom=zoom)['pixmap']
        with fitz.open(self.reference) as ref:
            expected = ref[0].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        self.assertEqual((actual.width, actual.height, actual.n),
                         (expected.width, expected.height, 3))
        a = np.frombuffer(actual.samples, np.uint8).reshape(actual.height, actual.width, 3)
        b = np.frombuffer(expected.samples, np.uint8).reshape(expected.height, expected.width, 3)
        ink_a, ink_b = a.min(axis=2) < 240, b.min(axis=2) < 240
        self.assertGreater(ink_b.sum(), 1000, 'oracle must contain visible text')
        # Different draw-device color conversion can slightly change edge pixels.
        overlap = np.count_nonzero(ink_a & ink_b) / np.count_nonzero(ink_a | ink_b)
        self.assertGreater(overlap, .95, 'glyphs moved, disappeared, or graphics leaked')
        # Check every occupied tile as well: a single lost word must not hide in
        # the whole-page overlap score (in particular the right-edge END marker).
        for y in range(0, actual.height, 40):
            for x in range(0, actual.width, 40):
                target = ink_b[y:y+40, x:x+40]
                if target.sum() >= 10:
                    got = ink_a[y:y+40, x:x+40]
                    self.assertGreater(np.count_nonzero(got & target) / target.sum(), .9,
                                       f'missing text near pixel {(x, y)}')
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), before)
        return actual

    def test_hidden_ocr_preserves_fonts_spacing_and_line_end(self):
        self.compare_to_visible()

    def test_rotations_and_cropbox_preserve_all_text(self):
        for rotation in (0, 90, 180, 270):
            with self.subTest(rotation=rotation):
                self.compare_to_visible(rotation=rotation, crop=True, zoom=1.25)

    def test_visible_and_stroked_text_keeps_geometry(self):
        for mode in (0, 1, 2, 4, 5, 6, 7):
            with self.subTest(mode=mode):
                self.compare_to_visible(mode=mode, zoom=2)

    def test_selection_and_search_still_point_to_visible_end_marker(self):
        pix = self.compare_to_visible(zoom=2)
        with fitz.open(self.source) as src:
            rect = src[0].search_for('END')[0]
        selected = self.run_task(docworker.SELECT_TEXT, page=1,
                                 x0=rect.x0, y0=rect.y0, x1=rect.x1, y1=rect.y1,
                                 mode='rectangle')
        self.assertEqual(selected['copy_text'], 'END')
        hits = self.run_task(docworker.SEARCH, needle='END')['hits']
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['page'], 1)
        self.assertGreater(fitz.Rect(hits[0]['rects'][0]).intersect(rect).get_area(), 0)
        box = (rect * fitz.Matrix(2, 2)).irect
        a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)
        self.assertGreater(np.count_nonzero(a[box.y0:box.y1, box.x0:box.x1] < 128), 100)

    def test_image_only_page_is_blank(self):
        with fitz.open() as doc:
            page = doc.new_page()
            image = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8), False)
            image.clear_with(0)
            page.insert_image(page.rect, pixmap=image)
            page.draw_line((0, 0), (595, 842), color=(0, 0, 0), width=10)
            doc.save(self.source)
        self.run_task(docworker.OPEN, path=str(self.source))
        result = self.run_task(docworker.RENDER_TEXT, page_no=0, zoom=1)
        self.assertEqual(result['spans'], 0)
        self.assertTrue(np.all(np.frombuffer(result['pixmap'].samples, np.uint8) == 255))

    def test_stale_render_is_actually_rejected(self):
        from nightread.window import MainWindow
        view = SimpleNamespace(page_zoom=lambda pno: 1.5, view_mode='text', show_pixmap=Mock())
        window = SimpleNamespace(cont_view=view, _update_cache_status=Mock())
        pix = object()
        MainWindow._deliver_pixmap(window, 0, 1.5, pix, view_mode='image')
        view.show_pixmap.assert_not_called()
        MainWindow._deliver_pixmap(window, 0, 1, pix, view_mode='text')
        view.show_pixmap.assert_not_called()
        MainWindow._deliver_pixmap(window, 0, 1.5, pix, view_mode='text')
        view.show_pixmap.assert_called_once_with(pix, page_no=0, highlight_masks=())


@unittest.skipUnless(os.environ.get('DISPLAY') and shutil.which('xdotool'),
                     'requires X11 and xdotool')
class TestTextLayerKeyboard(unittest.TestCase):
    # Reuse the isolated XTest harness without inheriting its unrelated tests.
    import test_continuous_input as _input
    setUp = _input.TestContinuousInput.setUp
    pump = _input.TestContinuousInput.pump
    wait = _input.TestContinuousInput.wait
    xdo = _input.TestContinuousInput.xdo
    goto = _input.TestContinuousInput.goto
    screen_point = _input.TestContinuousInput.screen_point
    drag = _input.TestContinuousInput.drag

    def test_real_ctrl_t_toggle_and_drag_keep_page_and_text(self):
        self.goto(127)
        before = self.cv.scroller.get_vadjustment().get_value()
        self.xdo('key', 'ctrl+t')
        self.wait(lambda: self.cv.view_mode == 'text' and 127 in self.cv._rendered
                  and not self.cv._pending)
        self.assertEqual(self.w.text_btn.get_label(), '文字层:开')
        self.assertAlmostEqual(self.cv.scroller.get_vadjustment().get_value(), before, delta=2)
        self.drag(127)
        self.assertTrue(self.cv._selection_rects)
        self.assertIn('Page 128 selection', self.w._sel_text)
        self.assertFalse(self.w._dirty)
        self.xdo('key', 'ctrl+t')
        self.wait(lambda: self.cv.view_mode == 'image' and 127 in self.cv._rendered
                  and not self.cv._pending)
        self.assertEqual(self.cv.page_no, 127)
        self.assertEqual(self.w.text_btn.get_label(), '文字层')
