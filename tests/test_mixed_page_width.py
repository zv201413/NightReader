"""Fit every physical page to the viewport; keep input and cached results aligned."""
import math
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import fitz
from nightread import config
from nightread.window import MainWindow
from nightread.continuous import PAGE_GAP, WINDOW_MARGIN
import test_continuous_input as inputs
import test_shortcuts as shortcuts


@unittest.skipUnless(os.environ.get('DISPLAY') and shutil.which('xdotool'), 'requires X11')
class TestMixedPageWidth(unittest.TestCase):
    pump = inputs.TestContinuousInput.pump
    wait = inputs.TestContinuousInput.wait
    xdo = inputs.TestContinuousInput.xdo
    goto = inputs.TestContinuousInput.goto
    external_clipboard = shortcuts.TestShortcuts.external_clipboard

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nr_mixed_width_')
        self.addCleanup(self.tmp.cleanup)
        for key, value in (('CONFIG_DIR', self.tmp.name),
                           ('CONFIG_PATH', self.tmp.name + '/config.json')):
            p = patch.object(config, key, value)
            p.start()
            self.addCleanup(p.stop)
        config.save(dict(config.DEFAULTS, window_width=1000, window_height=750,
                         sidebar_visible=False, night_mode='off'))
        self.path = self.tmp.name + '/mixed.pdf'
        self.sizes = []
        with fitz.open() as doc:
            for pno in range(24):
                width, height = ((1200, 1800), (600, 900), (2400, 1800), (500, 700))[pno % 4]
                page = doc.new_page(width=width, height=height)
                page.insert_text((1800 if width == 2400 else 120, 180),
                                 f'TARGET{pno + 1}', fontsize=48 if width == 2400 else 24)
                if pno % 4 == 3:
                    page.set_cropbox(fitz.Rect(20, 30, 420, 630))
                    page.set_rotation(90)
                self.sizes.append((page.rect.width, page.rect.height))
            doc.save(self.path)
        self.w = MainWindow(path=self.path)
        self.addCleanup(self.w.destroy)
        self.w.page_entry.set_property('im-module', 'gtk-im-context-simple')
        self.w.show_all()
        self.cv = self.w.cont_view
        self.wait(lambda: self.w.page_count == 24 and self.cv._rendered and not self.cv._pending)
        self.xdo('windowfocus', self.w.get_window().get_xid())

    def assert_fitted(self):
        target = self.cv.scroller.get_allocated_width() - 28
        self.assertTrue(self.cv._pixmaps)
        for pno, pix in self.cv._pixmaps.items():
            self.assertAlmostEqual(pix.width, target, delta=1)
            width, height = self.sizes[pno]
            self.assertAlmostEqual(pix.height, height * target / width, delta=1)
        adj = self.cv.scroller.get_hadjustment()
        self.assertLessEqual(adj.get_upper(), adj.get_page_size())

    def test_all_sizes_fit_on_open_resize_and_far_jump_without_geometry_guessing(self):
        self.assertEqual(self.cv._page_sizes, self.sizes)
        self.assert_fitted()
        target = self.cv.scroller.get_allocated_width() - 28
        expected = sum(math.ceil(h * target / w) + PAGE_GAP for w, h in self.sizes[:19])
        # This page has never rendered: its position must already use its own size.
        self.assertAlmostEqual(self.cv._page_top(19), expected, delta=3)
        self.goto(19)
        self.wait(lambda: not self.cv._pending)
        self.assert_fitted()
        self.assertLessEqual(len(self.cv._pixmaps), 2 * WINDOW_MARGIN + 1)
        anchor, offset = self.cv._scroll_anchor()
        self.w.resize(850, 650)
        self.wait(lambda: self.w.get_size().width == 850 and not self.cv._pending)
        self.assert_fitted()
        self.assertEqual(self.cv._scroll_anchor()[0], anchor)
        self.assertAlmostEqual(self.cv._scroll_anchor()[1], offset, delta=2)

    def test_manual_zoom_reset_modes_and_stale_results_use_each_pages_scale(self):
        self.goto(1)
        old_scale = self.cv.page_zoom(1)
        old_pix = self.cv._pixmaps[1]
        self.cv.set_zoom(self.cv.zoom)  # Same first-page zoom, different second-page zoom.
        self.wait(lambda: 1 in self.cv._rendered and not self.cv._pending)
        self.assertFalse(self.cv._auto_fit)
        self.assertLess(self.cv._pixmaps[1].width, old_pix.width * .6)
        with patch.object(self.cv, 'show_pixmap') as show:
            self.w._deliver_pixmap(1, old_scale, old_pix, view_mode='image')
            show.assert_not_called()
        self.cv.zoom_reset()
        self.wait(lambda: 1 in self.cv._rendered and not self.cv._pending)
        self.assert_fitted()
        for presentation in ('proof', 'reading'):
            before = self.cv._scroll_anchor()
            self.w.text_presentation_combo.set_active_id(presentation)
            self.wait(lambda: 1 in self.cv._rendered and not self.cv._pending)
            self.assert_fitted()
            self.assertEqual(self.cv._scroll_anchor(), before)
        # +/- starts from the current page's actual magnification.
        scale = self.cv.page_zoom(1)
        self.cv.zoom_in()
        self.assertAlmostEqual(self.cv.page_zoom(1), scale * 1.25)

    def test_fit_ignores_manual_zoom_limits_and_replaces_previous_document_sizes(self):
        path = self.tmp.name + '/extreme-sizes.pdf'
        self.sizes = [(12000, 8000), (100, 150), (600, 900)]
        with fitz.open() as doc:
            for width, height in self.sizes:
                doc.new_page(width=width, height=height)
            doc.save(path)
        self.w.open_file(path)
        self.wait(lambda: self.w._path == path and len(self.cv._pixmaps) == 3 and not self.cv._pending)
        self.assertEqual(self.cv._page_sizes, self.sizes)
        self.assertLess(self.cv.page_zoom(0), .25)
        self.assertGreater(self.cv.page_zoom(1), 8)
        self.assert_fitted()

    def screen_point(self, pno, x, y):
        # Independent oracle: visible bitmap width / original PDF width.
        pix = self.cv._pixmaps[pno]
        scale = pix.width / self.sizes[pno][0]
        _, ox, oy = self.cv.canvas.get_window().get_origin()
        left = max(0, (self.cv.canvas.get_allocated_width() - pix.width) / 2)
        return (round(ox + left + x * scale - self.cv.scroller.get_hadjustment().get_value()),
                round(oy + self.cv._page_top(pno) + y * scale - self.cv.scroller.get_vadjustment().get_value()))

    def test_real_drag_copy_and_saved_highlight_on_small_and_large_pages(self):
        for pno in (1, 2):
            with self.subTest(page=pno + 1):
                self.goto(pno)
                with fitz.open(self.path) as doc:
                    word = doc[pno].get_text('words')[0]
                x0, y0, x1, y1, text, *_ = word
                start = self.screen_point(pno, x0 + 1, (y0 + y1) / 2)
                end = self.screen_point(pno, x1 - 1, (y0 + y1) / 2)
                self.xdo('mousemove', *start, 'mousedown', '1', 'mousemove', *end, 'mouseup', '1')
                self.wait(lambda: self.w._sel_ranges and not self.w._selection_inflight)
                self.xdo('key', 'ctrl+c')
                self.assertEqual(self.external_clipboard(), text)
                preview = list(self.cv._selection_rects)
                self.xdo('key', 'ctrl+h')
                self.wait(lambda: self.w._dirty and self.cv._highlight_masks.get(pno))
                self.xdo('key', 'ctrl+s')
                self.wait(lambda: not self.w._dirty)
                with fitz.open(self.path) as doc:
                    page = doc[pno]
                    annots = list(page.annots())
                    self.assertEqual(len(annots), 1)
                    quad = fitz.Quad(annots[0].vertices).rect
                    self.assertEqual([round(v, 2) for v in quad], preview[0])
                    self.assertTrue(quad.contains(fitz.Point((x0+x1)/2, (y0+y1)/2)))
