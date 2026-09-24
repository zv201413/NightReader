"""夜读像素变换 —— M3 的两模式(off / invert / soft)。

契约 K4:`docs/00-shared.md` §5。

关键点:
    · **底图不可变**。worker 渲染出的日间 pixmap 是底图;所有变换在
      `fitz.Pixmap(pix.colorspace, pix)` 深拷贝上进行(K5),绝不就地改底图。
      否则切回 off 时只能靠"再反相一次"还原,而那是错的(会有舍入损失)。
    · `soft` 走浮点**双端**映射 `[26, 224]`。只转浮点而上端仍取 255 时黑字会变纯白,
      夜读依然刺眼 —— 双端是实测确定的舒适区间。
    · 以 numpy 就地写 `samples_mv` 实现,单页毫秒级,不必进缓存。

关于 LUT 的两点偏差(已记入 R3 回执 F1):
    计划书 M3-3 判据写「0/64/128/192/255 映射为 224/174/125/75/26」,
    但 K4 给的公式是 `(LO + inv * (HI - LO) / 255.0).astype(np.uint8)`。
    `astype` 对正数是**截断**,精确值 124.611765 / 74.917647 截断后是 124 / 74,
    不是 125 / 75。两者只能取一个:
        · 逐字遵守 K4 公式(RULE-5)→ 124 / 74
        · 用 np.round 去凑判据数字   → 125 / 75
    本实现选**逐字遵守 K4 公式**(RULE-5「代码契约逐字遵守」优先于判据表的
    示例数字),回执里两个值都给出,并同时给出 np.round 版本供规划方对照。
"""

from __future__ import annotations

import numpy as np
import fitz

# 模式常量
OFF = "off"
INVERT = "invert"
SOFT = "soft"

MODES = (OFF, INVERT, SOFT)
MODE_LABELS = {OFF: "日间", INVERT: "反相", SOFT: "柔化"}
DEFAULT_MODE = INVERT

# K4:柔化的双端映射区间
SOFT_LO = 26
SOFT_HI = 224


def next_mode(mode: str) -> str:
    """三态循环:off → invert → soft → off(M3 要求热键 D 按此顺序)。"""
    try:
        i = MODES.index(mode)
    except ValueError:
        return DEFAULT_MODE
    return MODES[(i + 1) % len(MODES)]


def soft_lut() -> np.ndarray:
    """K4 的 soft 映射表(256 项),供判据 M3-3 直接取五点。

    逐字实现 K4 公式:先反相到浮点,再双端映射,最后 astype 截断。
    """
    x = np.arange(256, dtype=np.float32)
    inv = 255.0 - x
    return (SOFT_LO + inv * (SOFT_HI - SOFT_LO) / 255.0).astype(np.uint8)


# 预计算一张 256 项表,变换时直接查表(比每像素浮点运算快,且结果与公式一致)
_SOFT_TABLE = soft_lut()


def deep_copy(pix: fitz.Pixmap) -> fitz.Pixmap:
    """K5 深拷贝:**保持通道数**。

    ❌ `fitz.Pixmap(pix)` 会静默追加 alpha(n=3 → n=4,+33% 字节),
       随后字节流长度不等,判据 M3-5/M3-7 会假失败。
    """
    return fitz.Pixmap(pix.colorspace, pix)


def apply_mode(pix: fitz.Pixmap, mode: str) -> fitz.Pixmap:
    """把日间底图 pix 按 mode 变换成一个**新的** Pixmap;底图不被修改。

    mode == off 时返回底图本身的深拷贝,保证调用方拿到的永远是"自己可处置"的对象,
    同时 M3-5(切回 off 与原图逐像素一致)可直接比对字节。
    """
    if mode == OFF:
        return deep_copy(pix)

    out = deep_copy(pix)
    buf = np.frombuffer(out.samples_mv, dtype=np.uint8)
    buf = buf.reshape(out.height, out.width, out.n)

    if mode == INVERT:
        # 纯反相:白底→纯黑,黑字→纯白
        np.subtract(255, buf, out=buf)
    elif mode == SOFT:
        # K4 双端柔化:查预计算表,就地写入
        np.take(_SOFT_TABLE, buf, out=buf)
    else:
        raise ValueError(f"未知夜读模式: {mode!r}")

    return out


def is_valid(mode: str) -> bool:
    return mode in MODES


def render_page(base: fitz.Pixmap, mode: str, highlight_masks=(), comfort_params=None) -> fitz.Pixmap:
    """夜读变换后保留 PDF 高亮区域的原色和原文字,与日间/文件一致。

    使用注记实际外观的 alpha,保留斜四边形、圆弧端点和抗锯齿边缘。
    alpha 除以峰值去掉注记自身透明度:颜色和透明度已经在 base 中合成。
    高亮内保留原字色,避免反相后的白字在明黄色底上失去对比度。
    """
    prepared = base
    if comfort_params:
        from .comfort import thicken, adjust_tones
        prepared = thicken(base, comfort_params[0])
    out = apply_mode(prepared, mode)
    if comfort_params:
        adjust_tones(out, comfort_params[1], comfort_params[2])
    if (mode == OFF and not comfort_params) or not highlight_masks:
        return out
    original = np.frombuffer(base.samples_mv, dtype=np.uint8).reshape(base.height, base.width, base.n)
    shown = np.frombuffer(out.samples_mv, dtype=np.uint8).reshape(out.height, out.width, out.n)
    for mask in highlight_masks:
        x, y, w, h = (mask[k] for k in ("x", "y", "width", "height"))
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(base.width, x + w), min(base.height, y + h)
        if x1 <= x0 or y1 <= y0:
            continue
        alpha = np.frombuffer(mask["alpha"], dtype=np.uint8).reshape(h, w)
        coverage = alpha[y0-y:y1-y, x0-x:x1-x].astype(np.float32) / mask["peak"]
        coverage = coverage[..., None]
        dst = shown[y0:y1, x0:x1]
        src = original[y0:y1, x0:x1]
        dst[:] = np.rint(dst * (1 - coverage) + src * coverage).astype(np.uint8)
    return out


# ---- 供判据使用的自检小工具(不参与渲染) ----

def lut_samples() -> dict:
    """M3-3 用的五点实测值(走 K4 公式,astype 截断)。"""
    t = _SOFT_TABLE
    return {int(v): int(t[v]) for v in (0, 64, 128, 192, 255)}


def lut_samples_rounded() -> dict:
    """同上的「先四舍五入再取整」版本 —— 用于回执里与计划书示例数字(125/75)对照。

    注意必须在**浮点阶段**四舍五入,不能对已截断的 uint8 表再 round(那是恒等操作)。
    """
    x = np.arange(256, dtype=np.float32)
    inv = 255.0 - x
    exact = SOFT_LO + inv * (SOFT_HI - SOFT_LO) / 255.0
    r = np.round(exact).astype(np.uint8)
    return {int(v): int(r[v]) for v in (0, 64, 128, 192, 255)}
