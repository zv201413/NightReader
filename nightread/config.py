"""状态持久化 —— ~/.config/nightread/config.json。

M1 只用它记住窗口尺寸与最近打开的缩放;夜读模式、字体等由 M3 追加。
设计原则:配置读写**永不抛异常**。配置文件坏了、权限不对、磁盘满,
都不该让应用起不来 —— 退回默认值即可。
"""

from __future__ import annotations

import json
import fcntl
import math
import os
import tempfile
from typing import Any
from .comfort import PRESETS

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or
                          os.path.expanduser("~/.config"), "nightread")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS: dict[str, Any] = {
    "window_width": 1200,
    "window_height": 900,
    "window_maximized": False,
    "text_presentation": "reading",
    "comfort_preset": "standard",
    "comfort_weight": 0,
    "comfort_contrast": 100,
    "comfort_brightness": 0,
    "sidebar_width": 300,
    "sidebar_visible": True,
    "zoom": 1.0,
    "last_page": 0,
    "last_file": "",
    "night_mode": "invert",
    "highlight_color": "黄",
    "highlight_opacity": 1.0,
    "remember_position": True,
    "selection_mode": "text",
    "ui_font_scale": 100,
    "shortcuts": {},
}

PREFERENCE_KEYS = ("night_mode", "highlight_color", "highlight_opacity",
                   "comfort_preset", "comfort_weight", "comfort_contrast", "comfort_brightness",
                   "sidebar_visible", "remember_position", "selection_mode", "ui_font_scale",
                   "shortcuts")


def validated(cfg: dict) -> dict:
    out = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    if out["text_presentation"] not in ("reading", "proof"):
        out["text_presentation"] = DEFAULTS["text_presentation"]
    if out["comfort_preset"] not in (*PRESETS, "custom"):
        out["comfort_preset"] = "standard"
    for key, lo, hi in (("comfort_weight", 0, 100), ("comfort_contrast", 60, 140),
                        ("comfort_brightness", -30, 30)):
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            value = DEFAULTS[key]
        out[key] = max(lo, min(hi, round(value)))
    if out["night_mode"] not in ("off", "invert", "soft"):
        out["night_mode"] = DEFAULTS["night_mode"]
    if out["selection_mode"] not in ("text", "rectangle"):
        out["selection_mode"] = DEFAULTS["selection_mode"]
    value = out["ui_font_scale"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        out["ui_font_scale"] = DEFAULTS["ui_font_scale"]
    else:
        out["ui_font_scale"] = max(70, min(200, round(value)))
    if out["highlight_color"] not in ("黄", "绿", "蓝", "粉", "橙", "紫"):
        out["highlight_color"] = DEFAULTS["highlight_color"]
    try:
        opacity = float(out["highlight_opacity"])
        if not math.isfinite(opacity):
            raise ValueError("nonfinite opacity")
        out["highlight_opacity"] = max(.1, min(1.0, opacity))
    except (ValueError, TypeError):
        out["highlight_opacity"] = DEFAULTS["highlight_opacity"]
    for key in ("sidebar_visible", "remember_position", "window_maximized"):
        if not isinstance(out[key], bool):
            out[key] = DEFAULTS[key]
    for key, minimum in (("window_width", 480), ("window_height", 320),
                         ("sidebar_width", 160)):
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            out[key] = DEFAULTS[key]
        elif not minimum <= value <= 16384:
            out[key] = DEFAULTS[key]
        else:
            out[key] = int(value)
    from . import shortcuts
    try:
        bindings = shortcuts.effective(out["shortcuts"])
        out["shortcuts"] = {action: bindings[action] for action in out["shortcuts"]
                            if action in shortcuts.ACTIONS}
    except (ValueError, TypeError):
        out["shortcuts"] = {}
    return out


def load() -> dict[str, Any]:
    """读配置。任何异常都退回默认值。"""
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # 只接受已知键,防止旧版残留键污染
            for k in DEFAULTS:
                if k in data:
                    cfg[k] = data[k]
    except Exception:
        pass
    return validated(cfg)


def save(cfg: dict[str, Any]) -> bool:
    """写配置。失败返回 False,不抛异常。"""
    tmp = None
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=CONFIG_DIR, prefix=".config-",
                                         suffix=".tmp", delete=False) as f:
            tmp = f.name
            json.dump(validated(cfg),
                      f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)      # 原子替换,避免写一半崩溃留下坏文件
        return True
    except Exception:
        return False
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def get(key: str, default: Any = None) -> Any:
    return load().get(key, DEFAULTS.get(key, default))


def set_(key: str, value: Any) -> bool:
    return update({key: value})


def update(values: dict) -> bool:
    """只更新调用者拥有的键,避免阅读窗口关掉时覆盖设置面板的改动。"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            cfg = load()
            cfg.update(values)
            return save(cfg)
    except Exception:
        return False
