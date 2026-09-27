"""Remember visible geometry, never the minimum size of a destroyed GTK widget."""
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, GLib
from nightread import config
from nightread.window import MainWindow


class TestGeometryConfig(unittest.TestCase):
    def test_invalid_and_legacy_collapsed_sizes_are_repaired(self):
        for value in (None, '900', True, float('nan'), -10, 0, 181, 20000):
            with self.subTest(value=value):
                cfg = config.validated({'window_width':value, 'window_height':value,
                                        'window_maximized':'yes'})
                self.assertEqual(cfg['window_width'], config.DEFAULTS['window_width'])
                self.assertEqual(cfg['window_height'], config.DEFAULTS['window_height'])
                self.assertFalse(cfg['window_maximized'])


@unittest.skipUnless(os.environ.get('DISPLAY'), 'requires GTK display')
class TestWindowGeometry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='nr_window_geometry_')
        self.addCleanup(self.tmp.cleanup)
        for key, value in (('CONFIG_DIR', self.tmp.name),
                           ('CONFIG_PATH', self.tmp.name+'/config.json')):
            context = patch.object(config, key, value)
            context.start()
            self.addCleanup(context.stop)
        config.save(dict(config.DEFAULTS, window_width=1000, window_height=700))
        self.windows = []
        self.addCleanup(self.close_windows)

    def close_windows(self):
        for window in self.windows:
            window.destroy()
        self.pump()

    def pump(self, seconds=.05):
        loop = GLib.MainLoop()
        GLib.timeout_add(int(seconds*1000), loop.quit)
        loop.run()

    def wait(self, condition):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            self.pump()
            if condition():
                return
        self.fail('window/config did not reach expected geometry')

    def reader(self):
        window=MainWindow()
        self.windows.append(window)
        window.show_all()
        self.pump(.1)
        return window

    def close(self, window):
        window.destroy()
        self.windows.remove(window)
        self.pump()

    def test_resize_live_save_destroy_and_reopen_preserves_exact_size(self):
        window=self.reader()
        self.assertEqual(tuple(window.get_size()), (1000,700))
        window.resize(940,620)
        self.wait(lambda: tuple(window.get_size())==(940,620))
        self.wait(lambda: config.load()['window_width']==940 and config.load()['window_height']==620)
        # Reproduce GTK's dismantled size independently of the stored live size.
        with patch.object(window,'get_size',return_value=(1653,181)):
            self.close(window)
        self.assertEqual((config.load()['window_width'],config.load()['window_height']), (940,620))
        reopened=self.reader()
        self.assertEqual(tuple(reopened.get_size()), (940,620))
        self.assertLessEqual(reopened.toolbar_scroll.get_allocated_width(),940)
        # Toolbar is a single row now: display/annotation controls share the
        # save button's row instead of wrapping to a second one.
        _x, save_y = reopened.save_btn.translate_coordinates(reopened, 0, 0)
        for button in (reopened.selection_btn, reopened._shortcut_buttons['settings']):
            _x, y = button.translate_coordinates(reopened, 0, 0)
            self.assertLessEqual(abs(y - save_y), 2)
        # The merged row overflows 940px, so the horizontal scroller keeps the
        # right-hand controls reachable rather than forcing the window wider.
        self.assertGreater(reopened.toolbar_scroll.get_hadjustment().get_upper(),
                           reopened.toolbar_scroll.get_allocated_width())

    def test_new_window_uses_latest_resize_before_first_window_closes(self):
        first=self.reader()
        first.resize(920,650)
        self.wait(lambda: config.load()['window_width']==920 and config.load()['window_height']==650)
        second=self.reader()
        self.assertEqual(tuple(second.get_size()),(920,650))

    def test_maximized_and_fullscreen_events_preserve_normal_size(self):
        window=self.reader()
        # Xvfb has no window manager. Feed the same GDK events a WM sends, and
        # independently verify maximize() is requested when the next window opens.
        def state(flag, enabled):
            event=SimpleNamespace(changed_mask=flag, new_window_state=flag if enabled else Gdk.WindowState(0))
            window._on_window_state(window,event)
        state(Gdk.WindowState.MAXIMIZED,True)
        window._on_window_configure(window,SimpleNamespace(width=1920,height=1080))
        self.wait(lambda: config.load()['window_maximized'])
        self.assertEqual(window._normal_size,(1000,700))
        self.close(window)
        with patch.object(MainWindow,'maximize') as maximize:
            reopened=self.reader()
            maximize.assert_called_once()
        self.assertEqual(reopened._normal_size,(1000,700))
        self.close(reopened)
        config.update({'window_maximized':False})
        window=self.reader()
        state(Gdk.WindowState.FULLSCREEN,True)
        window._on_window_configure(window,SimpleNamespace(width=1920,height=1080))
        self.assertEqual(window._normal_size,(1000,700))
        state(Gdk.WindowState.FULLSCREEN,False)
        window.resize(960,660)
        self.wait(lambda: window._normal_size==(960,660))

    def test_saved_dimensions_fit_smaller_monitor(self):
        config.update({'window_width':1653,'window_height':1000})
        monitor=SimpleNamespace(get_workarea=lambda:SimpleNamespace(width=800,height=600))
        display=SimpleNamespace(get_primary_monitor=lambda:monitor)
        with patch('nightread.window.Gdk.Display.get_default',return_value=display):
            window=self.reader()
        self.assertEqual(window._normal_size,(768,552))
        self.assertEqual(tuple(window.get_size()),(768,552))
