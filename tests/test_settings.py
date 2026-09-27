import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib

from nightread import config


class TestSettingsConfig(unittest.TestCase):
    def test_invalid_preferences_fall_back_and_unknown_keys_are_removed(self):
        cfg = config.validated({"night_mode": "bad", "highlight_color": "bad",
                                "highlight_opacity": float("nan"),
                                "sidebar_visible": "false", "unknown": True})
        for key in config.PREFERENCE_KEYS:
            self.assertEqual(cfg[key], config.DEFAULTS[key])
        self.assertNotIn("unknown", cfg)

    def test_ui_font_scale_is_clamped(self):
        self.assertEqual(config.validated({"ui_font_scale": 500})["ui_font_scale"], 200)
        self.assertEqual(config.validated({"ui_font_scale": 10})["ui_font_scale"], 70)
        self.assertEqual(config.validated({"ui_font_scale": "x"})["ui_font_scale"], 100)


@unittest.skipUnless(os.environ.get("DISPLAY"), "requires X11")
class TestUiFontScale(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nr_uifont_")
        self.addCleanup(self.tmp.cleanup)
        for key, value in (("CONFIG_DIR", self.tmp.name),
                           ("CONFIG_PATH", self.tmp.name + "/config.json")):
            mock = patch.object(config, key, value)
            mock.start()
            self.addCleanup(mock.stop)
        config.save(config.DEFAULTS)

    def test_saving_panel_persists_scale_and_applies_css(self):
        from nightread.settings import SettingsWindow, apply_ui_font_scale
        from nightread import settings as settings_mod
        panel = SettingsWindow()
        self.addCleanup(panel.destroy)
        panel.ui_font.set_value(150)
        panel._save()
        self.assertEqual(config.load()["ui_font_scale"], 150)
        # The provider is a module singleton; a larger scale must render a
        # larger point size in its CSS than a smaller one.
        apply_ui_font_scale(80)
        small = settings_mod._ui_font_provider.to_string()
        apply_ui_font_scale(200)
        large = settings_mod._ui_font_provider.to_string()
        def pt(css):
            return float(css.split("font-size:")[1].split("pt")[0])
        self.assertGreater(pt(large), pt(small))


@unittest.skipUnless(os.environ.get("DISPLAY") and shutil.which("xdotool"), "requires X11")
class TestSettingsPanel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="nr_settings_")
        self.addCleanup(self.tmp.cleanup)
        for key, value in (("CONFIG_DIR", self.tmp.name),
                           ("CONFIG_PATH", self.tmp.name + "/config.json")):
            mock = patch.object(config, key, value)
            mock.start()
            self.addCleanup(mock.stop)
        config.save(config.DEFAULTS)

    def pump(self, seconds=.1):
        loop = GLib.MainLoop()
        GLib.timeout_add(int(seconds * 1000), loop.quit)
        loop.run()

    def test_panel_updates_open_reader_and_survives_reader_close(self):
        from nightread.window import MainWindow
        from nightread.settings import SettingsWindow
        reader = MainWindow()
        self.addCleanup(reader.destroy)
        reader.show_all()
        panel = SettingsWindow()  # 独立面板,没有直接调用阅读窗口的回调。
        self.addCleanup(panel.destroy)
        panel.show_all()
        self.pump()
        panel.mode.set_active_id("soft")
        panel.color.set_active_id("绿")
        panel.opacity.set_value(40)
        panel.sidebar.set_active(False)
        panel.remember.set_active(False)
        panel.selection_mode.set_active_id("rectangle")
        button = panel.save_button
        x, y = button.translate_coordinates(panel, button.get_allocated_width() // 2,
                                            button.get_allocated_height() // 2)
        _, ox, oy = panel.get_window().get_origin()
        subprocess.run(["xdotool", "mousemove", str(ox+x), str(oy+y), "click", "1"], check=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and reader.view.night_mode != "soft":
            self.pump()
        self.assertEqual(reader.view.night_mode, "soft")
        self.assertEqual(reader._highlight_color, (0, 1, 0))
        self.assertAlmostEqual(reader._highlight_opacity, .4)
        self.assertFalse(reader.sidebar.get_visible())
        self.assertFalse(reader.cfg["remember_position"])
        self.assertEqual(reader.view.selection_mode, "rectangle")
        self.assertIn("已保存", panel.status.get_text())
        reader.destroy()
        cfg = config.load()
        self.assertEqual(cfg["highlight_color"], "绿")
        self.assertEqual(cfg["night_mode"], "soft")
        self.assertEqual(cfg["highlight_opacity"], .4)
        self.assertFalse(cfg["sidebar_visible"])
        self.assertFalse(cfg["remember_position"])
        self.assertEqual(cfg["selection_mode"], "rectangle")
        # 再一次原子替换也应被监视到,不是只对第一次设置生效。
        reader2 = MainWindow()
        self.addCleanup(reader2.destroy)
        reader2.show_all()
        self.assertEqual(reader2.view.selection_mode, "rectangle")
        config.update({"night_mode": "off", "sidebar_visible": True, "selection_mode": "text"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and reader2.view.night_mode != "off":
            self.pump()
        self.assertEqual(reader2.view.night_mode, "off")
        self.assertTrue(reader2.sidebar.get_visible())
        self.assertEqual(reader2.view.selection_mode, "text")
