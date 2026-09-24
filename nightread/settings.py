"""可单独从 .desktop 打开的 GTK 设置面板。"""
from __future__ import annotations

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk

from . import config, darkmode, shortcuts, comfort
from .annotate import COLORS


class SettingsWindow(Gtk.Window):
    def __init__(self, app=None, parent=None, on_apply=None):
        super().__init__(title="夜读设置", application=app, transient_for=parent)
        self.set_default_size(560, 540)
        self.set_border_width(20)
        self.set_resizable(False)
        self._on_apply = on_apply
        cfg = config.load()
        self._bindings = shortcuts.effective(cfg["shortcuts"])
        self._active_shortcuts = shortcuts.keymap(self._bindings)
        self._editing_shortcut = False
        self._shortcut_dialog = None
        self.connect("key-press-event", self._on_key_press)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.add(box)
        self.tabs = Gtk.Notebook()
        box.pack_start(self.tabs, True, True, 0)
        reading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        reading.set_border_width(14)
        self.tabs.append_page(reading, Gtk.Label(label="阅读"))
        grid = Gtk.Grid(column_spacing=20, row_spacing=14)
        reading.pack_start(grid, True, True, 0)

        self.mode = Gtk.ComboBoxText()
        for name in darkmode.MODES:
            self.mode.append(name, darkmode.MODE_LABELS[name])
        self.mode.set_active_id(cfg["night_mode"])
        self.color = Gtk.ComboBoxText()
        for name in COLORS:
            self.color.append(name, name + "色")
        self.color.set_active_id(cfg["highlight_color"])
        self.opacity = Gtk.SpinButton.new_with_range(10, 100, 5)
        self.opacity.set_value(round(cfg["highlight_opacity"] * 100))
        self.selection_mode = Gtk.ComboBoxText()
        self.selection_mode.append("text", "连续选字")
        self.selection_mode.append("rectangle", "区域 / 列选择")
        self.selection_mode.set_active_id(cfg["selection_mode"])
        for row, (title, widget) in enumerate((("阅读模式", self.mode),
                                              ("高亮颜色", self.color),
                                              ("高亮浓度（%）", self.opacity),
                                              ("选字方式", self.selection_mode))):
            label = Gtk.Label(label=title, xalign=0)
            grid.attach(label, 0, row, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row, 1, 1)
        self.sidebar = Gtk.CheckButton(label="显示书签侧栏")
        self.sidebar.set_active(cfg["sidebar_visible"])
        self.remember = Gtk.CheckButton(label="记住上次阅读页码")
        self.remember.set_active(cfg["remember_position"])
        grid.attach(self.sidebar, 0, 4, 2, 1)
        grid.attach(self.remember, 0, 5, 2, 1)
        note = Gtk.Label(label="高亮在日间、反相和柔化模式下保持原色。\n颜色和浓度设置用于新添加的高亮。", xalign=0)
        note.set_line_wrap(True)
        reading.pack_start(note, False, False, 0)
        self.tabs.append_page(self._build_shortcuts(), Gtk.Label(label="快捷键"))
        self.tabs.append_page(self._build_comfort(cfg), Gtk.Label(label="舒适阅读"))
        self.mode.connect("changed", lambda *_: self._update_comfort_preview())
        self.status = Gtk.Label(xalign=0)
        box.pack_start(self.status, False, False, 0)
        buttons = Gtk.ButtonBox(orientation=Gtk.Orientation.HORIZONTAL)
        buttons.set_layout(Gtk.ButtonBoxStyle.END)
        buttons.set_spacing(10)
        close = Gtk.Button(label="关闭")
        close.connect("clicked", lambda *_: self.destroy())
        self.save_button = Gtk.Button(label="保存设置")
        self.save_button.get_style_context().add_class("suggested-action")
        self.save_button.connect("clicked", self._save)
        buttons.add(close)
        buttons.add(self.save_button)
        box.pack_start(buttons, False, False, 0)

    def _save(self, _button=None):
        values = {"night_mode": self.mode.get_active_id(),
                  "highlight_color": self.color.get_active_id(),
                  "highlight_opacity": self.opacity.get_value() / 100,
                  "sidebar_visible": self.sidebar.get_active(),
                  "remember_position": self.remember.get_active(),
                  "selection_mode": self.selection_mode.get_active_id(),
                  **self._comfort_values(),
                  "shortcuts": self._bindings}
        if not config.update(values):
            self.status.set_text("保存失败，请检查配置目录是否可写。")
            return
        if self._on_apply:
            self._on_apply(config.load())
        self._active_shortcuts = shortcuts.keymap(self._bindings)
        self.status.set_text("已保存，设置已应用。")

    def _comfort_values(self):
        return {"comfort_preset": self.comfort_preset.get_active_id(),
                "comfort_weight": self.comfort_weight.get_value_as_int(),
                "comfort_contrast": self.comfort_contrast.get_value_as_int(),
                "comfort_brightness": self.comfort_brightness.get_value_as_int()}

    def _build_comfort(self, cfg):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_border_width(14)
        note = Gtk.Label(label="仅用于文字层的「舒适阅读」，OCR校对保持原样。\n"
                              "下方预览随调节更新；点击保存设置后应用到阅读窗口。", xalign=0)
        note.set_line_wrap(True)
        page.pack_start(note, False, False, 0)
        grid = Gtk.Grid(column_spacing=20, row_spacing=10)
        page.pack_start(grid, False, False, 0)
        self.comfort_preset = Gtk.ComboBoxText()
        for key, (label, _params) in comfort.PRESETS.items():
            self.comfort_preset.append(key, label)
        self.comfort_preset.append("custom", "自定义")
        self.comfort_preset.set_active_id(cfg["comfort_preset"])
        self.comfort_weight = Gtk.SpinButton.new_with_range(0, 100, 5)
        self.comfort_contrast = Gtk.SpinButton.new_with_range(60, 140, 5)
        self.comfort_brightness = Gtk.SpinButton.new_with_range(-30, 30, 1)
        self._comfort_spins = (self.comfort_weight, self.comfort_contrast, self.comfort_brightness)
        for spin, value in zip(self._comfort_spins, comfort.parameters(cfg)):
            spin.set_value(value)
        for row, (title, widget) in enumerate((("档位", self.comfort_preset),
                ("加粗程度（0–100）", self.comfort_weight),
                ("对比度（标准100）", self.comfort_contrast),
                ("亮度（标准0）", self.comfort_brightness))):
            grid.attach(Gtk.Label(label=title, xalign=0), 0, row, 1, 1)
            widget.set_hexpand(True)
            grid.attach(widget, 1, row, 1, 1)
        self._updating_comfort = False
        self.comfort_preview = Gtk.Image()
        page.pack_start(self.comfort_preview, True, True, 0)
        self.comfort_preset.connect("changed", self._comfort_preset_changed)
        for spin in self._comfort_spins:
            spin.connect("value-changed", self._comfort_custom_changed)
        self._update_comfort_preview()
        return page

    def _comfort_preset_changed(self, combo):
        preset = comfort.PRESETS.get(combo.get_active_id())
        if preset:
            self._updating_comfort = True
            for spin, value in zip(self._comfort_spins, preset[1]):
                spin.set_value(value)
            self._updating_comfort = False
        self._update_comfort_preview()

    def _comfort_custom_changed(self, _spin):
        if not self._updating_comfort:
            self.comfort_preset.set_active_id("custom")
            self._update_comfort_preview()

    def _update_comfort_preview(self):
        import io
        import cairo
        import fitz
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import Pango, PangoCairo
        from .viewer import to_gdkpixbuf
        if not hasattr(self, "_comfort_preview_base"):
            surface = cairo.ImageSurface(cairo.FORMAT_RGB24, 420, 120)
            ctx = cairo.Context(surface)
            ctx.set_source_rgb(1, 1, 1)
            ctx.paint()
            ctx.set_source_rgb(0, 0, 0)
            ctx.move_to(18, 16)
            layout = PangoCairo.create_layout(ctx)
            font_options = cairo.FontOptions()
            font_options.set_antialias(cairo.ANTIALIAS_GRAY)
            PangoCairo.context_set_font_options(layout.get_context(), font_options)
            layout.set_font_description(Pango.FontDescription("Sans 15"))
            layout.set_text("舒适阅读 · 文字与数字\n土力学  2.72 / (1 + e)\n字号和位置保持不变", -1)
            PangoCairo.show_layout(ctx, layout)
            data = io.BytesIO()
            surface.write_to_png(data)
            self._comfort_preview_base = fitz.Pixmap(data.getvalue())
        params = tuple(spin.get_value_as_int() for spin in self._comfort_spins)
        pix = darkmode.render_page(self._comfort_preview_base, self.mode.get_active_id(),
                                   comfort_params=params)
        self.comfort_preview.set_from_pixbuf(to_gdkpixbuf(pix))

    def _build_shortcuts(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_border_width(14)
        hint = Gtk.Label(label="双击快捷键后按下新组合；Backspace 清除，Esc 取消。", xalign=0)
        hint.set_line_wrap(True)
        page.pack_start(hint, False, False, 0)
        self.shortcut_store = Gtk.ListStore(str, str, str, str)
        self.shortcut_tree = Gtk.TreeView(model=self.shortcut_store)
        self.shortcut_tree.connect("row-activated", self._shortcut_activated)
        action_column = Gtk.TreeViewColumn("操作", Gtk.CellRendererText(), text=1)
        action_column.set_expand(True)
        self.shortcut_tree.append_column(action_column)
        for slot, title in enumerate(("快捷键", "备用键")):
            column = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=2 + slot)
            column.set_min_width(125)
            self.shortcut_tree.append_column(column)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(280)
        scroller.add(self.shortcut_tree)
        page.pack_start(scroller, True, True, 0)
        self.reset_shortcuts_button = Gtk.Button(label="恢复默认快捷键")
        self.reset_shortcuts_button.connect("clicked", self._reset_shortcuts)
        page.pack_start(self.reset_shortcuts_button, False, False, 0)
        self._refresh_shortcuts()
        return page

    def _refresh_shortcuts(self):
        self.shortcut_store.clear()
        for action, (name, _defaults) in shortcuts.ACTIONS.items():
            values = self._bindings[action] + ["", ""]
            self.shortcut_store.append([action, name] + [
                shortcuts.label(value) if value else "未设置" for value in values[:2]])

    def _shortcut_activated(self, tree, path, column):
        if self._editing_shortcut:
            return
        slot = max(0, tree.get_columns().index(column) - 1)
        name = self.shortcut_store[path][1]
        dialog = Gtk.Dialog(title=f"设置快捷键：{name}", transient_for=self,
                            modal=True, destroy_with_parent=True)
        dialog.add_button("取消", Gtk.ResponseType.CANCEL)
        hint = Gtk.Label(label="请按下新快捷键\nBackspace 清除，Esc 取消。")
        hint.set_margin_start(30)
        hint.set_margin_end(30)
        hint.set_margin_top(24)
        hint.set_margin_bottom(24)
        dialog.get_content_area().add(hint)
        dialog.connect("response", lambda *_: dialog.destroy())
        dialog.connect("destroy", self._shortcut_editing_done)
        dialog.connect("key-press-event", self._record_shortcut, path.to_string(), slot)
        self._shortcut_dialog = dialog
        self._editing_shortcut = True
        dialog.show_all()
        dialog.present()

    def _shortcut_editing_done(self, _dialog):
        self._shortcut_dialog = None
        self._editing_shortcut = False

    def _record_shortcut(self, dialog, event, path, slot):
        if event.is_modifier or event.keyval in shortcuts.MODIFIER_KEYS:
            return True
        key, mods = shortcuts.event_key(event)
        if key == Gdk.KEY_Escape and not mods:
            dialog.destroy()
            return True
        binding = "" if key == Gdk.KEY_BackSpace and not mods else Gtk.accelerator_name(
            key, Gdk.ModifierType(mods))
        dialog.destroy()
        self._set_shortcut(path, slot, binding)
        return True

    def _set_shortcut(self, path, slot, binding):
        action = self.shortcut_store[path][0]
        proposed = {key: list(values) for key, values in self._bindings.items()}
        values = proposed[action] + ["", ""]
        values[slot] = binding
        proposed[action] = [value for value in values[:2] if value]
        try:
            self._bindings = shortcuts.effective(proposed)
        except ValueError as error:
            self.status.set_text(str(error))
            return
        self._refresh_shortcuts()
        self.status.set_text("快捷键已修改，点击保存设置生效。")

    def _reset_shortcuts(self, _button=None):
        self._bindings = shortcuts.effective()
        self._refresh_shortcuts()
        self.status.set_text("已恢复默认快捷键，点击保存设置生效。")

    def _on_key_press(self, _window, event):
        if self._editing_shortcut or shortcuts.editing_text(self.get_focus(), event):
            return False
        if self._active_shortcuts.get(shortcuts.event_key(event)) == "close":
            self.close()
            return True
        return False


class SettingsApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.nightread.Nightread.Settings")
        self.window = None

    def do_activate(self):
        if self.window is None:
            self.window = SettingsWindow(app=self)
            self.window.connect("destroy", self._forget_window)
        self.window.show_all()
        self.window.present()

    def _forget_window(self, _window):
        self.window = None
