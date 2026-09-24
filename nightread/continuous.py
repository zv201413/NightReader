"""连续阅读:整本书的坐标只存在于 Adjustment,原生窗口始终等于视口。

Gtk.Scrollable 阻止 ScrolledWindow 自动包上会被全书高度撑大的 Viewport。
页图按视口附近的页缓存;绘制、选字都使用同一套页内/视口坐标变换。
"""
from __future__ import annotations

import math
from bisect import bisect_right
from typing import Callable, Optional

import fitz

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, Gdk, GLib, GObject
    _HAVE_GTK = True
except Exception:
    Gtk = Gdk = GLib = GObject = None
    _HAVE_GTK = False

from . import darkmode
from .viewer import ZOOM_MIN, ZOOM_MAX, to_gdkpixbuf

WINDOW_MARGIN = 3
SETTLE_MS = 120
PAGE_GAP = 6


if _HAVE_GTK:
    class _PageCanvas(Gtk.DrawingArea, Gtk.Scrollable):
        # GTK injects the scroller's adjustments through this interface. The
        # drawing area's allocation stays small, regardless of their upper bound.
        hadjustment = GObject.Property(type=Gtk.Adjustment)
        vadjustment = GObject.Property(type=Gtk.Adjustment)
        hscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy,
                                         default=Gtk.ScrollablePolicy.MINIMUM)
        vscroll_policy = GObject.Property(type=Gtk.ScrollablePolicy,
                                         default=Gtk.ScrollablePolicy.MINIMUM)


class ContinuousView:
    def __init__(self, on_render_requested: Callable[[int, float], None],
                 on_status: Optional[Callable[[str], None]] = None,
                 on_page_changed: Optional[Callable[[int], None]] = None):
        if not _HAVE_GTK:
            raise RuntimeError("GTK 不可用")
        self._request_render = on_render_requested
        self._on_status = on_status
        self._on_page_changed = on_page_changed
        self.page_no = self.page_count = 0
        # In fit mode this is the first-page reference; use page_zoom for drawing/input.
        self.zoom = self._fit_zoom = 1.0
        self.night_mode = darkmode.DEFAULT_MODE
        self.selection_mode = "text"
        # 视图模式:"image" = 正常页图;"text" = 只显示文字层(图片层隐藏)。
        # 它只影响**渲染请求**怎么发,绘制/滚动/窗口化那套完全不用改 ——
        # 两种模式拿到的都是 fitz.Pixmap,下游一视同仁。
        self.view_mode = "image"
        self.text_presentation = "reading"
        self.comfort_params = (0, 100, 0)
        self._page_w, self._page_h = 595.0, 842.0
        self._page_sizes = []
        self._auto_fit = True
        self._pixmaps = {}
        self._highlight_masks = {}
        self._rendered = {}  # page_no -> GdkPixbuf, never a full-document widget
        self._pending = set()
        self._heights = []
        self._tops = [0.0]
        self._suppress_scroll = False
        self._settle_id = 0
        self._destroyed = False
        self._overlay_rects = []
        self._selection_rects = []
        self._sel_start = self._sel_rect_pdf = self._sel_page = None
        self._selection_serial = 0
        self._selection_update_id = 0
        self._sel_end = None
        self._on_selection = None
        self._on_selection_cleared = None

        self.canvas = _PageCanvas()
        self.canvas.set_hexpand(True)
        self.canvas.set_vexpand(True)
        self.canvas.set_can_focus(True)
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scroller.add(self.canvas)
        self.canvas.connect("draw", self._on_draw)
        self.canvas.connect("size-allocate", self._on_size_allocate)
        self.canvas.connect("destroy", self._on_destroy)
        self.canvas.connect("realize", self._on_realize)
        self.canvas.add_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                               Gdk.EventMask.BUTTON_RELEASE_MASK |
                               Gdk.EventMask.POINTER_MOTION_MASK |
                               Gdk.EventMask.SCROLL_MASK |
                               Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.canvas.connect("button-press-event", self._on_sel_press)
        self.canvas.connect("button-release-event", self._on_sel_release)
        self.canvas.connect("motion-notify-event", self._on_sel_motion)
        self.scroller.connect("scroll-event", self._on_scroll)
        self.scroller.get_vadjustment().connect("value-changed", self._on_value_changed)
        self.scroller.get_hadjustment().connect("value-changed", self._on_horizontal_changed)

    @property
    def widget(self):
        return self.scroller

    def set_page_count(self, n: int, page_sizes=None) -> None:
        self.page_count = max(0, int(n))
        self._page_sizes = list(page_sizes or [])
        if self._page_sizes:
            if len(self._page_sizes) != self.page_count:
                raise ValueError("页面尺寸数量与页数不一致")
            self._page_w, self._page_h = self._page_sizes[0]
        self.page_no = 0
        self.clear_overlay()
        self._drop_all_rendered()
        self._heights = [0] * self.page_count
        self._rebuild_geometry()
        self._scroll_to_page(0)
        self._ensure_window()

    def set_page_size(self, w: float, h: float) -> None:
        self._page_w, self._page_h = float(w), float(h)
        self._rebuild_geometry()

    def page_size(self, pno: int):
        if 0 <= pno < len(self._page_sizes):
            return self._page_sizes[pno]
        return self._page_w, self._page_h

    def page_zoom(self, pno: int) -> float:
        # Fit width normalizes mixed physical page sizes; manual zoom is absolute.
        return self.zoom * self._page_w / self.page_size(pno)[0] if self._auto_fit else self.zoom

    def show_pixmap(self, pix: fitz.Pixmap, page_no: Optional[int] = None,
                    highlight_masks=()) -> None:
        if self._destroyed:
            return
        pno = self.page_no if page_no is None else int(page_no)
        self._pending.discard(pno)
        if pno not in self._visible_window():
            return
        self._pixmaps[pno] = pix
        self._highlight_masks[pno] = highlight_masks
        if self._heights[pno] != pix.height:
            # Rounding to physical pixels must not move the page being read.
            anchor, offset = self._scroll_anchor()
            self._heights[pno] = pix.height
            self._rebuild_geometry()
            self._set_scroll_value(self._page_top(anchor) + offset)
        self._materialize(pno)

    def goto(self, page_no: int) -> None:
        page_no = max(0, min(int(page_no), max(0, self.page_count - 1)))
        self._set_current_page(page_no)
        self._scroll_to_page(page_no)
        self._ensure_window()
        self.canvas.queue_draw()

    def next_page(self) -> None:
        self.goto(self.page_no + 1)

    def prev_page(self) -> None:
        self.goto(self.page_no - 1)

    def set_zoom(self, zoom: float) -> None:
        self._apply_zoom(max(ZOOM_MIN, min(float(zoom), ZOOM_MAX)), auto_fit=False)

    def zoom_in(self) -> None:
        self.set_zoom(self.page_zoom(self.page_no) * 1.25)

    def zoom_out(self) -> None:
        self.set_zoom(self.page_zoom(self.page_no) / 1.25)

    def zoom_reset(self) -> None:
        self.fit_to_width()

    def fit_to_width(self) -> None:
        if self.scroller.get_allocated_width() > 1:
            self._fit_zoom = self._compute_fit_zoom()
            self._apply_zoom(self._fit_zoom, auto_fit=True)

    @property
    def fit_zoom(self) -> float:
        return self._fit_zoom

    def _compute_fit_zoom(self) -> float:
        # Manual zoom limits must not stop unusually wide pages fitting the view.
        return max(1, self.scroller.get_allocated_width() - 28) / self._page_w

    def _on_size_allocate(self, _widget, _alloc) -> None:
        if self._auto_fit:
            self.fit_to_width()
        self._configure_adjustments()
        self._ensure_window()

    def _apply_zoom(self, zoom: float, auto_fit: bool) -> None:
        if abs(zoom - self.zoom) < 1e-9 and auto_fit == self._auto_fit:
            return
        anchor, offset = self._scroll_anchor()
        old_scale = self.page_zoom(anchor)
        self.zoom = zoom
        self._auto_fit = auto_fit
        offset *= self.page_zoom(anchor) / old_scale
        self._drop_all_rendered()
        self._heights = [0] * self.page_count
        self._rebuild_geometry()
        self._set_scroll_value(self._page_top(anchor) + offset)
        self._ensure_window()
        self.canvas.queue_draw()

    def set_night_mode(self, mode: str) -> None:
        self.night_mode = mode
        for pno in list(self._pixmaps):
            self._materialize(pno)

    def set_view_mode(self, mode: str, force: bool = False) -> str:
        """切换 图片层 / 文字层。

        切换必须**丢掉所有已渲染的页** —— 底图不一样了(_pixmaps 里存的
        是上一模式的渲染结果),不丢的话屏幕上会新旧混着显示。
        丢完立即 _ensure_window() 重新请求,不必等用户滚动。

        返回生效后的模式。
        """
        if mode not in ("image", "text"):
            raise ValueError(f"未知视图模式: {mode!r}")
        if mode == self.view_mode and not force:
            return self.view_mode
        anchor, offset = self._scroll_anchor()   # 保住当前阅读位置
        self.view_mode = mode
        self._drop_all_rendered()
        self._heights = [0] * self.page_count
        self._rebuild_geometry()
        self._set_scroll_value(self._page_top(anchor) + offset)
        self._ensure_window()
        self.canvas.queue_draw()
        return self.view_mode

    def toggle_view_mode(self) -> str:
        return self.set_view_mode("text" if self.view_mode == "image" else "image")

    def cycle_night_mode(self) -> str:
        self.set_night_mode(darkmode.next_mode(self.night_mode))
        return self.night_mode

    def set_overlay(self, rects, page_count=None) -> None:
        if rects and isinstance(rects[0], (list, tuple)):
            self._overlay_rects = [{"page": self.page_no + 1, "rect": r} for r in rects]
        else:
            self._overlay_rects = list(rects or [])
        self.canvas.queue_draw()

    def clear_overlay(self) -> None:
        if self._selection_update_id:
            GLib.source_remove(self._selection_update_id)
            self._selection_update_id = 0
        self._overlay_rects = []
        self._selection_rects = []
        self._sel_start = self._sel_rect_pdf = self._sel_page = None
        self._selection_serial += 1
        if self._on_selection_cleared:
            self._on_selection_cleared()
        self.canvas.queue_draw()

    def set_selection_rects(self, rects) -> None:
        self._selection_rects = list(rects)
        self.canvas.queue_draw()

    def _blank_height(self, pno=None) -> int:
        pno = self.page_no if pno is None else pno
        return max(1, math.ceil(self.page_size(pno)[1] * self.page_zoom(pno)))

    def _rebuild_geometry(self) -> None:
        self._tops = [0.0]
        for pno, h in enumerate(self._heights):
            self._tops.append(self._tops[-1] + (h or self._blank_height(pno)) + PAGE_GAP)
        self._configure_adjustments()

    def _configure_adjustments(self) -> None:
        width, height = self.canvas.get_allocated_width(), self.canvas.get_allocated_height()
        total = max(0, self._tops[-1] - PAGE_GAP)
        content_width = max([self.page_size(pno)[0] * self.page_zoom(pno)
                             for pno in range(self.page_count)] + [0] +
                            [p.width for p in self._pixmaps.values()])
        self._suppress_scroll = True
        try:
            for adj, upper, size in (
                (self.scroller.get_vadjustment(), total, height),
                (self.scroller.get_hadjustment(), content_width, width),
            ):
                adj.configure(min(adj.get_value(), max(0, upper - size)),
                              0, max(upper, size), 40, size * .9, size)
        finally:
            self._suppress_scroll = False

    def _scroll_anchor(self):
        value = self.scroller.get_vadjustment().get_value()
        pno = self._page_at_y(value)
        return pno, value - self._page_top(pno)

    def _page_top(self, pno: int) -> float:
        return self._tops[max(0, min(pno, self.page_count))]

    def _page_at_y(self, y: float) -> int:
        return max(0, min(bisect_right(self._tops, y) - 1, self.page_count - 1))

    def _visible_window(self) -> set:
        if not self.page_count:
            return set()
        adj = self.scroller.get_vadjustment()
        first = min(self.page_no, self._page_at_y(adj.get_value()))
        last = max(self.page_no, self._page_at_y(adj.get_value() + adj.get_page_size()))
        # Usually seven pages; at very small zoom every visible page still renders.
        lo = max(0, min(first, self.page_no - WINDOW_MARGIN))
        hi = min(self.page_count - 1, max(last, self.page_no + WINDOW_MARGIN))
        return set(range(lo, hi + 1))

    def _ensure_window(self) -> None:
        if self._destroyed:
            return
        want = self._visible_window()
        for pno in list(self._rendered):
            if pno not in want:
                self._release(pno)
        for pno in sorted(want, key=lambda n: abs(n - self.page_no)):
            if pno not in self._rendered and pno not in self._pending:
                self._pending.add(pno)
                self._request_render(pno, self.page_zoom(pno))

    def _materialize(self, pno: int) -> None:
        base = self._pixmaps.get(pno)
        if base is not None:
            self._rendered[pno] = to_gdkpixbuf(darkmode.render_page(
                base, self.night_mode, self._highlight_masks.get(pno, ()),
                comfort_params=(self.comfort_params if self.view_mode == "text" and
                                self.text_presentation == "reading" else None)))
            self.canvas.queue_draw()

    def set_comfort_params(self, params):
        if tuple(params) != self.comfort_params:
            self.comfort_params = tuple(params)
            if self.view_mode == "text" and self.text_presentation == "reading":
                for pno in list(self._pixmaps):
                    self._materialize(pno)

    def _release(self, pno: int) -> None:
        self._rendered.pop(pno, None)
        self._pixmaps.pop(pno, None)
        self._highlight_masks.pop(pno, None)
        self._pending.discard(pno)

    def _drop_all_rendered(self) -> None:
        self._rendered.clear()
        self._pixmaps.clear()
        self._highlight_masks.clear()
        self._pending.clear()

    def invalidate_page(self, pno: int) -> None:
        self._release(pno)
        self._ensure_window()
        self.canvas.queue_draw()

    def _page_left(self, pno: int) -> float:
        pix = self._pixmaps.get(pno)
        width = pix.width if pix is not None else self.page_size(pno)[0] * self.page_zoom(pno)
        return max(0, (self.canvas.get_allocated_width() - width) / 2)

    def _on_draw(self, _widget, cr) -> bool:
        cr.set_source_rgb(.10, .10, .10)
        cr.paint()
        if not self.page_count:
            return False
        vy = self.scroller.get_vadjustment().get_value()
        hx = self.scroller.get_hadjustment().get_value()
        first = self._page_at_y(vy)
        last = self._page_at_y(vy + self.canvas.get_allocated_height())
        for pno in range(first, last + 1):
            pix = self._rendered.get(pno)
            if pix is None:
                continue
            left, top = self._page_left(pno) - hx, self._page_top(pno) - vy
            cr.save()
            cr.rectangle(left, top, pix.get_width(), pix.get_height())
            cr.clip()
            Gdk.cairo_set_source_pixbuf(cr, pix, left, top)
            cr.paint()
            cr.translate(left, top)
            zoom = self.page_zoom(pno)
            cr.scale(zoom, zoom)
            cr.set_source_rgba(1, .8, .15, .35)
            for r in self._overlay_rects:
                if r.get("page") in (None, pno + 1):
                    rect = r.get("rect") or r.get("bbox")
                    if rect and len(rect) == 4:
                        x0, y0, x1, y1 = rect
                        cr.rectangle(x0, y0, x1 - x0, y1 - y0)
                        cr.fill()
            if self._sel_page == pno and self._selection_rects:
                cr.set_source_rgba(.2, .55, 1, .35)
                for x0, y0, x1, y1 in self._selection_rects:
                    cr.rectangle(x0, y0, x1 - x0, y1 - y0)
                    cr.fill()
            if (self._sel_page == pno and self.selection_mode == "rectangle"
                    and self._sel_rect_pdf):
                x0, y0, x1, y1 = self._sel_rect_pdf
                cr.set_source_rgba(.3, .7, 1, .9)
                cr.set_line_width(1 / zoom)
                cr.set_dash([4 / zoom, 3 / zoom])
                cr.rectangle(x0, y0, x1 - x0, y1 - y0)
                cr.stroke()
            cr.restore()
        return False

    def _set_current_page(self, pno: int) -> None:
        if pno != self.page_no:
            self.page_no = pno
            self.clear_overlay()
            if self._on_page_changed:
                self._on_page_changed(pno)

    def _set_scroll_value(self, value: float) -> None:
        self._suppress_scroll = True
        try:
            self.scroller.get_vadjustment().set_value(value)
        finally:
            self._suppress_scroll = False
        self.canvas.queue_draw()

    def _scroll_to_page(self, pno: int) -> None:
        self._set_scroll_value(self._page_top(pno))

    def _on_value_changed(self, adj) -> None:
        self.canvas.queue_draw()
        if self._suppress_scroll:
            return
        self._set_current_page(self._page_at_y(adj.get_value() + adj.get_page_size() / 2))
        if self._settle_id:
            GLib.source_remove(self._settle_id)
        self._settle_id = GLib.timeout_add(SETTLE_MS, self._on_settle)

    def _on_horizontal_changed(self, _adj) -> None:
        self.canvas.queue_draw()

    def _on_settle(self) -> bool:
        self._settle_id = 0
        self._ensure_window()
        return False

    def enable_selection(self, on_selection, on_clear=None) -> None:
        self._on_selection = on_selection
        self._on_selection_cleared = on_clear

    def _on_realize(self, widget) -> None:
        cursor = "crosshair" if self.selection_mode == "rectangle" else "text"
        widget.get_window().set_cursor(Gdk.Cursor.new_from_name(widget.get_display(), cursor))

    def set_selection_mode(self, mode: str) -> None:
        if mode != self.selection_mode:
            self.clear_overlay()
            self.selection_mode = mode
            if self.canvas.get_window() is not None:
                self._on_realize(self.canvas)

    def _widget_to_page(self, wx: float, wy: float):
        vy = wy + self.scroller.get_vadjustment().get_value()
        pno = self._page_at_y(vy)
        vx = wx + self.scroller.get_hadjustment().get_value() - self._page_left(pno)
        zoom = self.page_zoom(pno)
        return pno, (vx / zoom, (vy - self._page_top(pno)) / zoom)

    def _on_sel_press(self, _widget, event) -> bool:
        if event.button != 1 or not self.page_count or not self._on_selection:
            return False
        pno, pt = self._widget_to_page(event.x, event.y)
        width, height = self.page_size(pno)
        if not (0 <= pt[0] <= width and 0 <= pt[1] <= height):
            return False
        self.canvas.grab_focus()
        self._set_current_page(pno)
        self.clear_overlay()
        self._sel_page, self._sel_start = pno, pt
        return True

    def _on_sel_motion(self, _widget, event) -> bool:
        if self._sel_start is None:
            return False
        pno, end = self._widget_to_page(event.x, event.y)
        if pno == self._sel_page:
            self._update_selection_rect(end)
            if not self._selection_update_id:
                self._selection_update_id = GLib.timeout_add(30, self._preview_selection)
        return True

    def _update_selection_rect(self, end) -> None:
        start = self._sel_start
        self._sel_end = end
        self._selection_serial += 1
        self._sel_rect_pdf = (min(start[0], end[0]), min(start[1], end[1]),
                              max(start[0], end[0]), max(start[1], end[1]))
        self.canvas.queue_draw()

    def _preview_selection(self) -> bool:
        self._selection_update_id = 0
        if self._sel_start is not None:
            self._on_selection(self._sel_start, self._sel_end)
        return False

    def _on_sel_release(self, _widget, event) -> bool:
        if event.button != 1 or self._sel_start is None:
            return False
        pno, end = self._widget_to_page(event.x, event.y)
        start = self._sel_start
        if self._selection_update_id:
            GLib.source_remove(self._selection_update_id)
            self._selection_update_id = 0
        if pno != self._sel_page:
            self.clear_overlay()
            if self._on_status:
                self._on_status("跨页拖选不支持,请在单页内选择")
            return True
        self._update_selection_rect(end)
        self._sel_start = None
        if abs(end[1] - start[1]) < 1 and abs(end[0] - start[0]) < 1:
            self.clear_overlay()
            return True
        self._on_selection(start, end)
        return True

    @property
    def selection_rect(self):
        return self._sel_rect_pdf

    def _on_scroll(self, widget, event):
        if not event.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        if event.direction == Gdk.ScrollDirection.SMOOTH:
            _ok, _dx, dy = event.get_scroll_deltas()
        elif event.direction == Gdk.ScrollDirection.UP:
            dy = -1
        elif event.direction == Gdk.ScrollDirection.DOWN:
            dy = 1
        else:
            return False
        if dy < 0:
            self.zoom_in()
        elif dy > 0:
            self.zoom_out()
        return True

    def _on_destroy(self, _widget) -> None:
        self._destroyed = True
        self.clear_overlay()
        if self._settle_id:
            GLib.source_remove(self._settle_id)
            self._settle_id = 0
        self._drop_all_rendered()

    def stats(self) -> dict:
        return {"resident_pages": len(self._rendered), "window": len(self._visible_window()),
                "zoom": round(self.page_zoom(self.page_no), 3), "fit_zoom": round(self._fit_zoom, 3)}

    def resident_bytes(self) -> int:
        return sum(p.height * p.stride for p in self._pixmaps.values())
