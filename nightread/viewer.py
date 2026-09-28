"""渲染视图:滚动/缩放/坐标映射/命中测试 + K5 的 Pixmap→GdkPixbuf 转换。

对应 `docs/stages/M1-skeleton.md`。

本模块做两件事:
  1. 把 worker 渲染出的 fitz.Pixmap 转成 GTK 能显示的 GdkPixbuf(K5 转换)
  2. 承载阅读区的滚动与缩放交互,并提供屏幕坐标→页面坐标的映射
     (M2 的书签命中、M5 的高亮选字都要用)

**本模块绝不持有 fitz.Document 对象** —— K3 要求 UI 线程零文档对象。
它只接收 worker 渲染好的 Pixmap(独立内存副本),或通过回调请求渲染。
"""

from __future__ import annotations

from typing import Callable, Optional

import fitz
import numpy as np

try:
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gtk, Gdk, GdkPixbuf, GLib
    _HAVE_GTK = True
except Exception:
    Gtk = Gdk = GdkPixbuf = GLib = None
    _HAVE_GTK = False

from . import darkmode

# ==========================================================================
# 一、K5:Pixmap → GdkPixbuf
# ==========================================================================

def deep_copy(pix: fitz.Pixmap) -> fitz.Pixmap:
    """K5 合规的深拷贝 —— 保持通道数,不追加 alpha。

    ✅ fitz.Pixmap(pix.colorspace, pix)   # n=3
    ❌ fitz.Pixmap(pix)                   # 静默追加 alpha,n=4,+33%
    """
    return fitz.Pixmap(pix.colorspace, pix)


def to_gdkpixbuf(pix: fitz.Pixmap):
    """把 fitz.Pixmap 转成 GdkPixbuf。

    两个 K5 要点:
      · has_alpha 用 bool(pix.alpha),不写死 —— n=3→False / n=4→True
      · rowstride 用 pix.stride,不写死 w*3

    ⚠️ GdkPixbuf.new_from_data **不复制** data,只引用。必须保证 data
    在 pixbuf 存活期间不被 GC 回收,否则花屏或崩溃。这里把 bytes 副本
    挂到 pixbuf 属性上保活。
    """
    if not _HAVE_GTK:
        raise RuntimeError("GdkPixbuf 不可用(需 PyGObject)")

    data = bytes(pix.samples)          # 独立副本,不受 pix 生命周期影响
    pb = GdkPixbuf.Pixbuf.new_from_data(
        data,
        GdkPixbuf.Colorspace.RGB,
        bool(pix.alpha),
        8,
        pix.width,
        pix.height,
        pix.stride,
        None, None,
    )
    pb._nightread_data = data          # 保活
    return pb


def pixmap_bytes(pix: fitz.Pixmap) -> int:
    """Pixmap 真实内存占用(与 K8 的 cost 口径一致)。"""
    return pix.height * pix.stride


def to_cairo_surface(pix: fitz.Pixmap):
    """Own one display buffer; RGB24 is native-endian xRGB, four bytes/pixel.

    No borrowed Pixmap pointer escapes. The RGB fast path avoids the temporary
    Pixbuf and its bytes copy; alpha uses the existing GDK conversion semantics.
    """
    import cairo
    import sys
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32 if pix.alpha else cairo.FORMAT_RGB24,
                                 pix.width, pix.height)
    if pix.n == 3 and not pix.alpha:
        src = np.frombuffer(pix.samples_mv, np.uint8).reshape(pix.height, pix.stride)
        src = src[:, :pix.width * 3].reshape(pix.height, pix.width, 3)
        dst = np.frombuffer(surface.get_data(), np.uint8).reshape(
            pix.height, surface.get_stride() // 4, 4)[:, :pix.width]
        if sys.byteorder == "little":
            dst[:, :, :3] = src[:, :, ::-1]
            dst[:, :, 3] = 255
        else:
            dst[:, :, 0] = 255
            dst[:, :, 1:] = src
        surface.mark_dirty()
    else:
        cr = cairo.Context(surface)
        Gdk.cairo_set_source_pixbuf(cr, to_gdkpixbuf(pix), 0, 0)
        cr.paint()
    return surface


# ==========================================================================
# 二、阅读区视图
# ==========================================================================

ZOOM_MIN = 0.25
ZOOM_MAX = 8.0
ZOOM_STEP = 1.25
