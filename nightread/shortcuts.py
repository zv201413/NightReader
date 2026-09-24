"""阅读窗口和设置面板共用的快捷键定义、规范化与冲突检查。"""
from __future__ import annotations

import gi
import re
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk

# 每个动作支持主键与备用键。配置只存 GTK accelerator 字符串。
ACTIONS = {
    "open": ("打开文件", ["<Primary>o"]),
    "save": ("保存", ["<Primary>s"]),
    "close": ("关闭窗口", ["<Primary>w"]),
    "find": ("查找", ["<Primary>f"]),
    "copy": ("复制选中文字", ["<Primary>c"]),
    "selection_mode": ("切换选字方式", ["<Primary><Shift>r"]),
    "highlight": ("添加高亮", ["<Primary>h"]),
    "zoom_in": ("放大", ["<Primary>equal", "<Primary>KP_Add"]),
    "zoom_out": ("缩小", ["<Primary>minus", "<Primary>KP_Subtract"]),
    "zoom_fit": ("适应宽度", ["<Primary>0"]),
    "next_page": ("下一页", ["Page_Down", "Right"]),
    "prev_page": ("上一页", ["Page_Up", "Left"]),
    "first_page": ("首页", ["Home"]),
    "last_page": ("末页", ["End"]),
    "night_mode": ("切换阅读模式", ["d", "<Shift>d"]),
    "text_layer": ("切换文字层", ["<Primary>t"]),
    "next_match": ("下一条搜索结果", ["F3"]),
    "prev_match": ("上一条搜索结果", ["<Shift>F3"]),
    "clear": ("关闭搜索 / 清除选区", ["Escape"]),
    "settings": ("打开设置", ["<Primary>comma"]),
}

MODIFIERS = (("Control", Gdk.ModifierType.CONTROL_MASK),
             ("Shift", Gdk.ModifierType.SHIFT_MASK),
             ("Alt", Gdk.ModifierType.MOD1_MASK),
             ("Super", Gdk.ModifierType.SUPER_MASK),
             ("Meta", Gdk.ModifierType.META_MASK),
             ("Hyper", Gdk.ModifierType.HYPER_MASK))
MOD_MASK = sum(int(mask) for _name, mask in MODIFIERS)
MODIFIER_KEYS = {Gdk.keyval_from_name(name) for name in (
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Super_L", "Super_R", "Meta_L", "Meta_R", "Hyper_L", "Hyper_R",
    "Caps_Lock", "Shift_Lock", "Num_Lock", "ISO_Level3_Shift", "Mode_switch")}


def parse(binding: str) -> tuple[int, int]:
    """不依赖显示服务器:GTK 的 <Primary> 解析在无 DISPLAY 时会丢 Ctrl。"""
    if not isinstance(binding, str) or not re.fullmatch(r"(?:<[^<>]+>)*[^<>]+", binding):
        raise ValueError("快捷键格式无效")
    names = {name.lower(): int(mask) for name, mask in MODIFIERS}
    names.update(primary=int(Gdk.ModifierType.CONTROL_MASK),
                 ctrl=int(Gdk.ModifierType.CONTROL_MASK), mod1=int(Gdk.ModifierType.MOD1_MASK))
    mods = 0
    for token in re.findall(r"<([^<>]+)>", binding):
        if token.lower() not in names:
            raise ValueError("不支持的快捷键修饰键")
        mods |= names[token.lower()]
    key = Gdk.keyval_from_name(re.sub(r"^(?:<[^<>]+>)*", "", binding))
    return key, mods


def canonical_key(key: int, modifiers: int) -> tuple[int, int]:
    key = Gdk.keyval_to_lower(key)
    mods = int(modifiers) & MOD_MASK
    # Ctrl++ 在常用布局中是 Ctrl+Shift+=;两种写法保留原放大行为。
    if key == Gdk.KEY_plus and mods & int(Gdk.ModifierType.CONTROL_MASK):
        key = Gdk.KEY_equal
        mods &= ~int(Gdk.ModifierType.SHIFT_MASK)
    return key, mods


def normalize(binding: str) -> str:
    if not isinstance(binding, str):
        raise ValueError("快捷键格式无效")
    key, mods = parse(binding)
    if key in (0, Gdk.KEY_VoidSymbol) or Gdk.keyval_name(key) is None or key in MODIFIER_KEYS:
        raise ValueError("请按一个按键或组合键")
    key, mods = canonical_key(key, mods)
    return "".join(f"<{name}>" for name, mask in MODIFIERS if mods & mask) + Gdk.keyval_name(key)


def effective(overrides=None) -> dict:
    bindings = {action: [normalize(key) for key in defaults]
                for action, (_label, defaults) in ACTIONS.items()}
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise ValueError("快捷键配置应为动作列表")
    for action, values in overrides.items():
        if action not in ACTIONS:
            continue
        if not isinstance(values, list) or len(values) > 2:
            raise ValueError("每个动作最多设置两个快捷键")
        bindings[action] = [normalize(value) for value in values if value]
    owners = {}
    for action, values in bindings.items():
        for binding in values:
            if binding in owners:
                raise ValueError(f"{label(binding)} 已用于「{ACTIONS[owners[binding]][0]}」")
            owners[binding] = action
    return bindings


def label(binding: str) -> str:
    key, mods = parse(binding)
    if Gdk.Display.get_default() is not None:
        return Gtk.accelerator_get_label(key, Gdk.ModifierType(mods))
    return "+".join(["Ctrl" if name == "Control" else name
                     for name, mask in MODIFIERS if mods & mask] + [Gdk.keyval_name(key)])


def keymap(bindings: dict) -> dict:
    return {canonical_key(*parse(binding)): action
            for action, values in bindings.items() for binding in values}


def event_key(event) -> tuple[int, int]:
    state = event.state
    display = Gdk.Display.get_default()
    if display is not None:
        state = Gdk.Keymap.get_for_display(display).add_virtual_modifiers(state)
    return canonical_key(event.keyval, state)


def editing_text(focus, event) -> bool:
    """文本框保留文字、方向键和常见编辑组合;F键、Esc和应用组合键仍可用。"""
    if not isinstance(focus, (Gtk.Entry, Gtk.TextView)):
        return False
    key, mods = event_key(event)
    command_mods = int(Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK |
                       Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.META_MASK)
    if not mods & command_mods:
        return not (Gdk.KEY_F1 <= key <= Gdk.KEY_F35 or key == Gdk.KEY_Escape)
    return (mods in (int(Gdk.ModifierType.CONTROL_MASK),
                     int(Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK))
            and key in (Gdk.KEY_a, Gdk.KEY_c, Gdk.KEY_v, Gdk.KEY_x, Gdk.KEY_z,
                        Gdk.KEY_y, Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_Home,
                        Gdk.KEY_End, Gdk.KEY_BackSpace, Gdk.KEY_Delete))
