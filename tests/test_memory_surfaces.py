"""Cached display surfaces preserve the former GDK drawing path exactly."""
from unittest.mock import patch
import threading
import time
import unittest

import cairo
import fitz
import numpy as np

from nightread import darkmode
from nightread.viewer import Gdk, Gtk, to_gdkpixbuf, to_cairo_surface


def wait_surfaces(cv):
    end = time.monotonic() + 5
    while cv._surface_future is not None or not cv._visible_pages() <= cv._rendered.keys():
        assert time.monotonic() < end, cv._surface_error
        while Gtk.events_pending():
            Gtk.main_iteration()
        time.sleep(.002)


class TestMemorySurfaces(unittest.TestCase):
    def test_surface_matches_gdk_at_fractional_offsets(self):
        for mode in darkmode.MODES:
            for comfort in (None, (30, 110, 3)):
                with self.subTest(mode=mode, comfort=comfort):
                    rgb = np.arange(37 * 257 * 3, dtype=np.uint8).reshape(37, 257, 3)
                    base = fitz.Pixmap(fitz.csRGB, 257, 37, rgb.tobytes(), False)
                    mask = {'x': 3, 'y': 4, 'width': 17, 'height': 3, 'peak': 255,
                            'alpha': bytes(range(0, 255, 5))}
                    pix = darkmode.render_page(base, mode, [mask], comfort)
                    surface = to_cairo_surface(pix)
                    outputs = []
                    for cached in (False, True):
                        target = cairo.ImageSurface(cairo.FORMAT_ARGB32, 290, 70)
                        cr = cairo.Context(target)
                        cr.set_source_rgb(.1, .1, .1)
                        cr.paint()
                        cr.rectangle(9.5, 7.25, pix.width, pix.height)
                        cr.clip()
                        if cached:
                            cr.set_source_surface(surface, 9.5, 7.25)
                        else:
                            Gdk.cairo_set_source_pixbuf(cr, to_gdkpixbuf(pix), 9.5, 7.25)
                        cr.paint()
                        outputs.append(bytes(target.get_data()))
                    self.assertEqual(outputs[0], outputs[1])
                    self.assertEqual(base.samples, rgb.tobytes())

    def test_visible_surfaces_reuse_and_bounded_lookahead(self):
        ok, _ = Gtk.init_check()
        if not ok:
            self.fail('GTK display required; run under Xvfb')
        from nightread.continuous import ContinuousView
        requests = []
        cv = ContinuousView(lambda p, z: requests.append((p, z)))
        win = Gtk.Window()
        win.set_default_size(600, 500)
        win.add(cv.widget)
        win.show_all()
        try:
            while Gtk.events_pending():
                Gtk.main_iteration()
            cv.set_page_count(30)
            cv.set_zoom(1)
            cv.goto(10)
            for pno in cv._visible_window():
                base = fitz.Pixmap(fitz.csRGB, (0, 0, 595, 842), False)
                base.clear_with(210 + pno)
                cv.show_pixmap(base, pno)
            target = cairo.ImageSurface(cairo.FORMAT_RGB24, 600, 500)
            cv._on_draw(cv.canvas, cairo.Context(target))
            wait_surfaces(cv)
            self.assertEqual(set(cv._rendered), cv._surface_pages())
            self.assertLessEqual(len(cv._rendered.keys() - cv._visible_pages()), 1)
            self.assertTrue(set(cv._pixmaps) - set(cv._rendered))
            before = cv.memory_stats()
            self.assertLess(before['rendered_bytes'], before['pixmap_bytes'])
            with patch('nightread.continuous.to_cairo_surface',
                       wraps=to_cairo_surface) as convert:
                for _ in range(5):
                    cv._on_draw(cv.canvas, cairo.Context(target))
                convert.assert_not_called()
            # A prefetched page becomes visible without another PDF render.
            requests.clear()
            cv.goto(11)
            wait_surfaces(cv)
            self.assertIn(11, cv._rendered)
            self.assertFalse(any(p == 11 for p, z in requests))
            self.assertNotIn(10, cv._rendered)
            self.assertIn(10, cv._pixmaps)
        finally:
            win.destroy()

    def test_slow_surface_work_does_not_block_scroll_or_deliver_stale_mode(self):
        from nightread.continuous import ContinuousView
        gate, entered = threading.Event(), threading.Event()
        cv = ContinuousView(lambda *_: None)
        win = Gtk.Window()
        win.set_default_size(600, 500)
        win.add(cv.widget)
        win.show_all()
        try:
            while Gtk.events_pending():
                Gtk.main_iteration()
            cv.set_page_count(10)
            cv.set_zoom(1)
            pix = fitz.Pixmap(fitz.csRGB, (0, 0, 595, 842), False)
            pix.clear_with(200)

            def slow(p):
                entered.set()
                assert gate.wait(5)
                return to_cairo_surface(p)

            with patch('nightread.continuous.to_cairo_surface', side_effect=slow):
                cv.show_pixmap(pix, 0)
                self.assertTrue(entered.wait(2))
                # This returns while conversion remains blocked in another thread.
                cv.set_night_mode('off')
                cv.goto(1)
                cv.show_pixmap(pix, 1)
                self.assertFalse(gate.is_set())
                gate.set()
                wait_surfaces(cv)
            self.assertNotIn(0, cv._rendered)
            shown = cv._rendered[1]
            self.assertEqual(bytes(shown.get_data()),
                             bytes(to_cairo_surface(pix).get_data()))
            self.assertIsNone(cv._surface_error)
        finally:
            gate.set()
            win.destroy()


if __name__ == '__main__':
    unittest.main()