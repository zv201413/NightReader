"""主窗口:Paned(书签树 | 阅读区) + 工具栏 + 状态栏。

对应 `docs/stages/M1-skeleton.md` 与 `docs/stages/M2-bookmarks.md`。

K3 的关键体现在本文件:
    本文件**不允许**出现 `self.doc` / `fitz.open` / `page.get_pixmap()`。
    一切文档操作都通过 `self.worker.submit(Task(...), callback)` 投递,
    结果在回调里更新 UI。验收判据 M1-6 就是 grep 本文件确认这一点。

M2 新增:
    · 书签双击**原地**编辑(CellRendererText editable + row-activated)
    · 标脏(标题栏加 *)与 Ctrl+S 保存(K1 三步式)
    · 未保存拦截(关闭 / 换文件)
    · 右键最小菜单:新增同级 / 新增子级 / 删除 / 上移 / 下移

M3/M4/M5/M6 新增:
    · 夜读两模式(off/invert/soft),热键 D 三态循环,底图不可变(K4/K5)
    · 全文搜索栏(Ctrl+F),字符索引,双基准(M4/K6)
    · 高亮批注(选字 + Ctrl+H),右键删除,持 page 引用(K7)
    · 全局异常兜底弹窗、精简重写入口、状态栏汇总
"""

from __future__ import annotations

import os
from typing import Optional

import fitz

try:
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, Gdk, GLib, Pango, Gio
    _HAVE_GTK = True
except Exception:
    Gtk = Gdk = GLib = Pango = None
    _HAVE_GTK = False

from . import config
from . import shortcuts
from . import darkmode
from .bookmarks import BookmarkModel
from .docworker import (Task, OPEN, CLOSE, RENDER, RENDER_TEXT, GET_TOC,
                        SET_TOC_ITEM, SET_TOC, SAVE, SEARCH, BUILD_INDEX,
                        ADD_ANNOT, DEL_ANNOT, GET_ANNOTS, SELECT_TEXT)
from .processworker import ProcessDocWorker
from .continuous import ContinuousView, ZOOM_MIN, ZOOM_MAX


class MainWindow(Gtk.ApplicationWindow if _HAVE_GTK else object):
    """主窗口。

    数据流:
        UI 事件 → worker.submit(Task(...), callback=on_xxx)
                → 独立文档子进程串行执行(唯一碰文档的地方)
                → GLib.idle_add 回 UI 线程 → callback → 更新控件
    """

    def __init__(self, app=None, path: Optional[str] = None):
        if not _HAVE_GTK:
            raise RuntimeError("GTK 不可用")
        super().__init__(application=app)
        self.set_title("nightread")

        self.cfg = config.load()
        self.text_presentation = self.cfg["text_presentation"]
        from .settings import apply_ui_font_scale
        apply_ui_font_scale(self.cfg["ui_font_scale"])
        self._restore_id = 0
        self._selection_request = None
        self._selection_inflight = False
        self._settings_window = None
        self._config_monitor = None
        self._prefs_reload_id = 0
        self._close_after_save = False
        self._shortcut_buttons = {}
        self._shortcut_map = {}
        self._text_fallbacks = {}
        self._text_warnings = {}
        self._geometry_save_id = 0
        self._window_maximized = self.cfg["window_maximized"]
        self._window_fullscreen = False
        self._normal_size = self._initial_window_size()
        self._sidebar_position = min(self.cfg["sidebar_width"], self._normal_size[0] // 2)
        self.set_default_size(*self._normal_size)

        self.worker = ProcessDocWorker(on_generation_change=self._on_generation_change)
        self.worker.start()

        self.page_count = 0
        self.page_size_w, self.page_size_h = 595.0, 842.0
        self._pending_render_id = 0
        self._render_queue = {}
        self._render_active = False
        self._render_dispatch_id = 0
        self._render_closed = False
        # render 计时用:记录最近一次渲染耗时(M1-2)
        self.last_render_ms = 0.0
        # M2:当前文件路径与脏标记
        self._path: Optional[str] = None
        self._dirty = False
        self._suppress_edit = False       # 程序性刷新树时抑制 edited 回调
        # M4:搜索状态
        self._search_gen = 0              # 当前有效搜索的 request_id
        self._search_hits: list = []
        self._search_idx = -1
        self._search_timer = 0            # 防抖定时器 id
        # M5:批注
        self._sel_anchor = None           # 选字起点(字符索引,**页相关**)
        self._sel_focus = None            # 选字终点
        self._sel_page = None             # 选区所属页(1 起)—— 必须锁页,见 G5
        self._sel_text = ""
        self._sel_ranges = None
        self._sel_context = None
        self._copy_pending = None
        self.last_incr_bytes = 0
        self.last_save_mode = ""

        self._build_ui()
        from .memstats import MemoryReporter
        self._memory_reporter = MemoryReporter.start_if_enabled(self.worker, self.cont_view)
        self._apply_preferences(self.cfg)
        self._watch_preferences()
        self.connect("destroy", self._on_destroy)
        self.connect("delete-event", self._on_delete_event)
        self.connect("configure-event", self._on_window_configure)
        self.connect("window-state-event", self._on_window_state)
        self.paned.connect("notify::position", self._on_sidebar_position)
        if self._window_maximized:
            self.maximize()

        if path:
            self.open_file(path)

    # ================= UI 搭建 =================

    def _build_ui(self) -> None:
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(vbox)

        # A long toolbar must not force the window wider than the user's screen.
        self.toolbar_scroll = Gtk.ScrolledWindow()
        self.toolbar_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.toolbar_scroll.add(self._build_toolbar())
        vbox.pack_start(self.toolbar_scroll, False, False, 0)
        self.searchbar = self._build_searchbar()
        vbox.pack_start(self.searchbar, False, False, 0)

        # 主体:Paned(左书签树 | 右阅读区)
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_position(self._sidebar_position)
        vbox.pack_start(self.paned, True, True, 0)

        # 左:书签树(M2:双击原地编辑)
        # (页码, 标题, 模型扁平索引) —— 第 3 列让 path→idx 不必推算,见 _iter_index
        self.tree_store = Gtk.TreeStore(int, str, int)
        self.bm = BookmarkModel()                     # 可编辑模型(不持文档)
        self.tree = Gtk.TreeView(model=self.tree_store)
        col = Gtk.TreeViewColumn("书签")
        # M2-2 核心:单元格可编辑
        self.cell = Gtk.CellRendererText()
        self.cell.set_property("ellipsize", Pango.EllipsizeMode.END)
        self.cell.set_property("editable", True)      # ← 双击即编辑的开关
        # 提交编辑(回车/失焦)时回调 on_title_edited
        self.cell.connect("edited", self._on_title_edited)
        col.pack_start(self.cell, True)
        col.add_attribute(self.cell, "text", 1)
        col.set_expand(True)
        col.set_min_width(180)
        self.tree.append_column(col)
        self.tree.set_tooltip_column(1)               # 悬停显示完整标题
        self.tree.set_enable_tree_lines(True)
        # 双击行进编辑态(见 _on_row_activated)
        self.tree.connect("row-activated", self._on_row_activated)
        # 右键最小菜单
        self.tree.connect("button-press-event", self._on_tree_button)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.tree)
        sw.set_size_request(160, -1)
        self.sidebar = sw
        self.paned.pack1(sw, resize=False, shrink=False)

        # 右:阅读区
        #
        # 只保留连续视图。原先还有一套"单页模式"(PageView),两条路都能
        # 走会各自往 pixcache 里灌页 —— 用户反馈"缓存 58 页 / 103.7 MB"
        # 就是这么来的。连续视图本身已能覆盖单页的全部用法(翻页、
        # 跳页、搜索定位、拖选批注),故删掉单页模式,只留一条路。
        self.cont_view = ContinuousView(
            on_render_requested=self._request_render,
            on_status=self._set_status,
            on_page_changed=self._on_cont_page_changed)
        self.cont_view.enable_selection(self._on_drag_selection, self._clear_selection)
        self.view = self.cont_view
        self.view.text_presentation = self.text_presentation
        self.paned.pack2(self.cont_view.widget, resize=True, shrink=False)

        # 状态栏
        self.statusbar = Gtk.Statusbar()
        self._status_ctx = self.statusbar.get_context_id("main")
        vbox.pack_start(self.statusbar, False, False, 0)

        # M6-4:关键路径套异常兜底(弹窗而非静默/崩溃)
        self.connect("key-press-event", self._guarded_key)
        self.tree.connect("row-activated", self._guarded_activate)
        self.tree.get_selection().connect("changed", self._guarded_selection)

    def _guarded_key(self, w, ev) -> bool:
        """键盘回调的兜底包装。返回 False 让事件继续传播。"""
        try:
            return bool(self._on_key_press(w, ev))
        except Exception as e:
            self._show_error("处理按键", e)
            return False

    def _guarded_selection(self, sel) -> None:
        """书签选中的异常兜底(签名与 row-activated 不同,不能共用包装)。"""
        try:
            self._on_toc_selection_changed(sel)
        except Exception as e:
            self._show_error("跳转书签", e)

    def _guarded_activate(self, tree, path, col) -> None:
        try:
            self._on_row_activated(tree, path, col)
        except Exception as e:
            self._show_error("打开书签", e)

    def _build_toolbar(self) -> "Gtk.Box":
        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        rows.set_margin_top(4)
        rows.set_margin_bottom(4)
        rows.set_margin_start(6)
        rows.set_margin_end(6)
        tb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        rows.pack_start(tb, False, False, 0)

        def btn(label, cb, tip="", action=None):
            b = Gtk.Button(label=label)
            if action:
                self._shortcut_buttons[action] = b
            if tip:
                b.set_tooltip_text(tip)
            b.connect("clicked", cb)
            tb.pack_start(b, False, False, 0)
            return b

        btn("打开", self._on_open_clicked, action="open")

        tb.pack_start(Gtk.Label(label="页码"), False, False, 6)
        self.page_entry = Gtk.Entry()
        self.page_entry.set_width_chars(6)
        self.page_entry.connect("activate", self._on_page_entry)
        tb.pack_start(self.page_entry, False, False, 0)
        self.page_total_label = Gtk.Label(label="/ 0")
        tb.pack_start(self.page_total_label, False, False, 0)

        btn("缩小", lambda *_: self.view.zoom_out(), action="zoom_out")
        btn("放大", lambda *_: self.view.zoom_in(), action="zoom_in")
        btn("适宽", lambda *_: self.view.zoom_reset(), action="zoom_fit")
        self.save_btn = btn("保存", lambda *_: self.save(), action="save")
        self.save_btn.set_sensitive(False)            # 无脏改动时禁用
        self.zoom_label = Gtk.Label(label="1.00×")
        tb.pack_end(self.zoom_label, False, False, 6)

        # 显示与批注控件与上面的翻页/缩放控件同处一行(工具栏可横向滚动)。
        # M3:夜读模式按钮(三态循环,与热键 D 同步)
        self.night_btn = btn("夜读:反相", lambda *_: self.cycle_night_mode(), action="night_mode")

        # M7:文字层开关(Ctrl+T)。只显示可搜索文字,隐藏图片层。
        self.text_btn = btn("文字层", lambda *_: self.cycle_view_mode(), action="text_layer")
        self._sync_text_button()
        self.text_presentation_combo = Gtk.ComboBoxText()
        self.text_presentation_combo.append("reading", "舒适阅读")
        self.text_presentation_combo.append("proof", "OCR校对")
        self.text_presentation_combo.set_active_id(self.text_presentation)
        self.text_presentation_combo.set_tooltip_text(
            "舒适阅读：修复显示，异常扫描页自动显示原页。\n"
            "OCR校对：显示原始文字字形、字号和位置，不使用阅读修复或原页替代。")
        self.text_presentation_combo.connect("changed", self._on_text_presentation_changed)
        tb.pack_start(self.text_presentation_combo, False, False, 0)

        # M4:搜索栏(默认折叠)
        self.search_toggle = Gtk.ToggleButton(label="搜索")
        self._shortcut_buttons["find"] = self.search_toggle
        self.search_toggle.connect("toggled", self._on_search_toggled)
        tb.pack_start(self.search_toggle, False, False, 0)

        # M6:精简重写(Z2 的 GUI 出口)+ 高亮配色(选做)
        self.shrink_btn = btn("精简重写", lambda *_: self.shrink_rewrite(),
                              "回收增量保存累积的历史版本")
        self.color_btn = btn("高亮:黄", lambda *_: self._cycle_highlight_color(),
                             "六色高亮(M6 选做)")
        self.selection_btn = btn("选字:连续", lambda *_: self.cycle_selection_mode(),
                                  action="selection_mode")
        btn("设置", lambda *_: self.show_settings(), action="settings")

        return rows

    def _cycle_highlight_color(self) -> None:
        """在六色之间循环(M6 选做)。"""
        from .annotate import COLORS
        names = list(COLORS)
        cur = getattr(self, "_highlight_color_name", names[0])
        nxt = names[(names.index(cur) + 1) % len(names)] if cur in names else names[0]
        self._highlight_color_name = nxt
        self.set_highlight_color(nxt)
        if hasattr(self, "color_btn"):
            self.color_btn.set_label(f"高亮:{nxt}")

    def _build_searchbar(self) -> "Gtk.Box":
        """M4 搜索栏:输入框 + 上一个/下一个 + 计数。默认隐藏。"""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.set_margin_top(2)
        bar.set_margin_bottom(4)
        bar.set_margin_start(6)
        bar.set_margin_end(6)

        bar.pack_start(Gtk.Label(label="查找"), False, False, 0)
        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text("输入关键词后自动搜索(不做正则转义)")
        self.search_entry.connect("changed", self._on_search_changed)
        self.search_entry.connect("activate", lambda *_: self.search_next())
        bar.pack_start(self.search_entry, True, True, 0)

        self.search_label = Gtk.Label(label="")
        bar.pack_start(self.search_label, False, False, 6)

        b_prev = Gtk.Button(label="上一个")
        b_prev.connect("clicked", lambda *_: self.search_prev())
        bar.pack_start(b_prev, False, False, 0)
        b_next = Gtk.Button(label="下一个")
        b_next.connect("clicked", lambda *_: self.search_next())
        bar.pack_start(b_next, False, False, 0)

        # no_show_all also skips the children; make them visible before hiding
        # only the container so Ctrl+F can reveal a usable input and buttons.
        bar.show_all()
        bar.hide()
        bar.set_no_show_all(True)     # 由 toggle 控制显隐
        return bar

    # ================= K3 桥接:UI → worker =================

    def open_file(self, path: str) -> None:
        """打开文件。UI 线程只投递任务,不碰文档。

        M2:若当前有未保存改动,先弹「保存 / 放弃 / 取消」(S3 补充项)。
        """
        if not os.path.exists(path):
            self._set_status(f"文件不存在: {path}")
            return
        if not self._confirm_discard("打开其它文件"):
            return

        def done(res):
            if not res.ok:
                self._set_status(f"打开失败: {res.error}")
                return
            v = res.value
            self._path = path
            self._mark_clean()
            self.page_count = v["pages"]
            if v.get("page_size"):
                self.page_size_w, self.page_size_h = v["page_size"]
            self.cont_view.set_page_size(self.page_size_w, self.page_size_h)
            self.cont_view.set_page_count(v["pages"], v.get("page_sizes"))
            self.cont_view.fit_to_width()
            self.page_total_label.set_text(f"/ {v['pages']}")
            self._set_status(
                f"已打开 {os.path.basename(path)} · {v['pages']} 页 · "
                f"{v['toc']} 条书签")
            self._load_toc()
            # 跳到最后读到的位置。必须等布局落地再做:此时视口还没分配,
            # fit_zoom 与各页占位高度都还没算出来,goto 会算错位置
            # (实测直接调会跑到第 101 页,而不是配置里记的那一页)。
            last_file = self.cfg.get("last_file", "")
            same_file = bool(last_file) and os.path.realpath(last_file) == os.path.realpath(path)
            self._restore_page = (int(self.cfg.get("last_page", 0) or 0)
                                  if self.cfg["remember_position"] and same_file else 0)
            if self._restore_id:
                GLib.source_remove(self._restore_id)
            self._restore_id = GLib.idle_add(self._restore_last_page)

        self.worker.submit(Task(OPEN, {"path": path}, callback=done))

    def _load_toc(self) -> None:
        """取大纲并填树(M2:同时载入可编辑模型)。"""
        def done(res):
            if not res.ok:
                self._set_status(f"读取书签失败: {res.error}")
                return
            self.bm.load(res.value)
            self._rebuild_tree()

        self.worker.submit(Task(GET_TOC, {}, callback=done))

    def _rebuild_tree(self) -> None:
        """按模型重建树视图。程序性刷新,抑制 edited 回调。"""
        self._suppress_edit = True
        try:
            self.tree_store.clear()
            stack = {}                    # level -> 上一层的 TreeIter
            for i, b in enumerate(self.bm.marks):
                parent = stack.get(b.level - 1) if b.level > 1 else None
                it = self.tree_store.append(parent, [b.page - 1, b.title, i])
                stack[b.level] = it
                for k in [x for x in stack if x > b.level]:
                    del stack[k]
            self.tree.expand_all()
        finally:
            self._suppress_edit = False

    def _iter_index(self, path) -> int:
        """TreeView 的 Gtk.TreePath → 模型里的扁平索引。

        索引在 _rebuild_tree 建树时就存进了第 3 列,这里只是取出来,**不做路径推算**。
        原实现用 `TreePath.new_first()` + `cur.next()` 逐个走着数,而 `next()` 只在
        同级递增,永远走不到 `1:0` 这类子路径:实测 40 条大纲里 21 条子书签返回 -1
        (调用方静默 return,什么都不发生)、17 条顶层书签返回树的顶层序号被误当扁平
        索引(**静默改到别的书签上**),只有前 2 条恰好正确。
        """
        try:
            it = self.tree_store.get_iter(path)
        except (ValueError, TypeError):
            return -1
        idx = self.tree_store.get_value(it, 2)
        if not 0 <= idx < len(self.bm.marks):
            return -1
        return idx

    # ---------------- M2:编辑 ----------------

    def _on_row_activated(self, tree, path, column) -> None:
        """双击书签 = **跳页**(用户反馈"点击书签不跳转",G9)。

        原实现把双击绑成了**进编辑态**(M2-2 判据),同时把跳页塞在
        `if not self.bm.dirty` 后面 —— 结果是:只要文档有未保存改动就
        完全不跳;没改动时双击也是先进编辑框,跳页被编辑态吞掉。
        用户点书签的**第一意图是跳过去看**,编辑是次要的。

        现在:
          单击 → 跳页(选中即跳,见 _on_toc_selection_changed)
          双击 → 跳页 + 进编辑态(想改名仍然双击)
        """
        it = self.tree_store.get_iter(path)
        page = self.tree_store.get_value(it, 0)
        # 先跳页:不受 dirty 影响,也不被编辑态吞掉
        self.view.goto(page)
        # 再进编辑态(双击的次要意图)。
        # 注意 set_cursor 的第二个参数要 **Gtk.TreeViewColumn 对象**,
        # 不是列号 int —— 传 int 会 TypeError(实测踩到)。
        cols = tree.get_columns()
        col = cols[0] if cols else None
        if col is not None:
            tree.set_cursor(path, col, start_editing=True)

    def _on_toc_selection_changed(self, sel) -> None:
        """单击书签即跳页 —— 不用双击。"""
        try:
            model, it = sel.get_selected()
        except (ValueError, TypeError):
            return
        if it is None:
            return
        page = model.get_value(it, 0)
        if isinstance(page, int) and 0 <= page < self.page_count:
            self.view.goto(page)

    def _on_title_edited(self, renderer, path_str, new_text) -> None:
        """CellRendererText 的 edited 回调 —— 编辑提交时到达。"""
        if self._suppress_edit:
            return
        idx = self._iter_index(Gtk.TreePath.new_from_string(path_str))
        if idx < 0:
            return
        ok, msg = self.bm.rename(idx, new_text)
        if not ok:
            # 空标题 → 拒绝并回滚显示
            self._set_status(f"改名被拒: {msg}")
            self._rebuild_tree()
            return
        self._rebuild_tree()
        self._mark_dirty()
        if msg:
            self._set_status(msg)
        else:
            self._set_status(f"已改名: {self.bm.marks[idx].title}（记得保存）")

    def _on_tree_button(self, tree, event) -> bool:
        """右键最小菜单(M2)。"""
        if event.button != 3:
            return False
        path_info = tree.get_path_at_pos(int(event.x), int(event.y))
        if not path_info:
            return False
        path = path_info[0]
        tree.set_cursor(path, None, False)
        idx = self._iter_index(path)

        menu = Gtk.Menu()
        items = [
            ("新增同级", lambda: self._bm_add("sibling", idx)),
            ("新增子级", lambda: self._bm_add("child", idx)),
            ("删除", lambda: self._bm_delete(idx)),
            ("上移", lambda: self._bm_move(idx, -1)),
            ("下移", lambda: self._bm_move(idx, +1)),
        ]
        for label, cb in items:
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", lambda _w, f=cb: f())
            menu.append(mi)
        menu.show_all()
        menu.popup_at_pointer(event)
        return True

    def _bm_add(self, kind: str, idx: int) -> None:
        pos = (self.bm.add_sibling(idx) if kind == "sibling"
               else self.bm.add_child(idx))
        if pos >= 0:
            self._rebuild_tree()
            self._mark_dirty()
            self._set_status("已新增书签（记得保存）")

    def _bm_delete(self, idx: int) -> None:
        if self.bm.delete(idx):
            self._rebuild_tree()
            self._mark_dirty()
            self._set_status("已删除书签（记得保存）")

    def _bm_move(self, idx: int, direction: int) -> None:
        new = self.bm.move_up(idx) if direction < 0 else self.bm.move_down(idx)
        if new != idx:
            self._rebuild_tree()
            self._mark_dirty()

    # ---------------- M2:脏标记与保存 ----------------

    def _mark_dirty(self) -> None:
        self._dirty = True
        self.bm.dirty = True
        self.save_btn.set_sensitive(True)
        base = os.path.basename(self._path) if self._path else "nightread"
        self.set_title(f"*{base} — nightread")

    def _mark_clean(self) -> None:
        self._dirty = False
        self.bm.mark_clean()
        self.save_btn.set_sensitive(False)
        base = os.path.basename(self._path) if self._path else "nightread"
        self.set_title(f"{base} — nightread")

    def save(self) -> None:
        """保存(K1 三步式 + K2 分档)。

        判定改标题还是结构变更:
            · 条数与层级都没变 → 逐条 set_toc_item(K2,增量 ~363 B)
            · 否则 → set_toc 整表重建(增量 ~8.5 KB)
        两条路径都经 worker 的 SAVE 任务,由它在 worker 线程内闭环 K1。
        """
        if not self._dirty or not self._path:
            return
        marks = self.bm.marks

        # 与磁盘现状比对,决定用 K2 还是 set_toc
        def decide(res):
            if not res.ok:
                self._close_after_save = False
                self.set_sensitive(True)
                self._set_status(f"保存前读取失败: {res.error}")
                return
            old = res.value
            same_shape = (len(old) == len(marks) and
                          all(int(o[0]) == m.level for o, m in zip(old, marks)))
            if same_shape:
                # 逐条改名,只发变化的那些(K2:set_toc_item)
                changed = [(i, m.title) for i, (o, m) in enumerate(zip(old, marks))
                           if str(o[1]) != m.title]
                self._do_save_items(changed)
            else:
                self._do_save_toc()

        self.worker.submit(Task(GET_TOC, {}, callback=decide))

    def _do_save_items(self, changed: list) -> None:
        """K2 路径:逐条 set_toc_item,最后保存一次。

        注意:**changed 为空也必须走 SAVE**。批注(M5)不属于大纲,只改批注时
        TOC 一条都没变,但文件确实需要保存。早期实现在这里直接 _mark_clean()
        返回,导致「加了高亮按 Ctrl+S 什么都没写」——高亮下次打开就没了。
        """
        for i, title in changed:
            self.worker.submit(Task(SET_TOC_ITEM, {"index": i, "title": title}))
        # 等所有改名落地后再存(worker 是单线程队列,顺序有保证)
        self._finish_save(len(changed), "set_toc_item")

    def _do_save_toc(self) -> None:
        """结构变更路径:set_toc 整表重建,再保存。"""
        toc = self.bm.as_toc()
        self.worker.submit(Task(SET_TOC, {"toc": toc}))
        self._finish_save(len(toc), "set_toc")

    def _finish_save(self, n: int, mode: str) -> None:
        def done(res):
            if not res.ok:
                self._close_after_save = False
                self.set_sensitive(True)
                self._set_status(f"保存失败: {res.error}")
                return
            v = res.value
            self.last_incr_bytes = v["incr_bytes"]
            self.last_save_mode = mode
            self._mark_clean()
            if self._close_after_save:
                self._close_after_save = False
                self.close()
                return
            self._set_status(
                f"已保存({mode}, {n} 条)· 增量 {v['incr_bytes']} B · "
                f"文件 {v['size_bytes']:,} B · {v['pages']} 页 / {v['toc']} 条")
            # K1 后文档对象全换,代数已变 → 重新拉大纲,刷新当前页
            self._load_toc()
            self.view.goto(self.view.page_no)
        self.worker.submit(Task(SAVE, {}, callback=done))

    def _confirm_discard(self, action: str, close_after_save: bool = False) -> bool:
        """有未保存改动时拦截。返回 True = 继续,False = 取消。"""
        if not self._dirty:
            return True
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True, destroy_with_parent=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text=f"有未保存的书签或批注，是否保存后再{action}？")
        dlg.set_title("未保存的修改")
        dlg.add_button("取消", Gtk.ResponseType.CANCEL)
        dlg.add_button("放弃改动", Gtk.ResponseType.REJECT)
        dlg.add_button("保存", Gtk.ResponseType.ACCEPT)
        resp = dlg.run()
        dlg.destroy()
        if resp == Gtk.ResponseType.ACCEPT:
            if not self._dirty:
                return True  # 弹窗期间,先前发起的异步保存可能已经成功。
            self._close_after_save = close_after_save
            if close_after_save:
                self.set_sensitive(False)  # 等待保存时不再接受新的编辑。
            self.save()
            return False        # 关闭等待保存成功;换文件仍由用户重试。
        if resp == Gtk.ResponseType.REJECT:
            self._mark_clean()
            return True
        return False            # 取消

    def _request_render(self, page_no: int, zoom: float) -> None:
        """Keep at most one render on the worker; reprioritize the rest on GTK.

        Fast scrolling/zooming replaces obsolete requests before they consume
        MuPDF time. Document operations retain their worker queue ordering.
        """
        if self._render_closed:
            return
        want = self.cont_view._visible_window()
        for pno in list(self._render_queue):
            if pno not in want:
                self._render_queue.pop(pno)
                self.cont_view._pending.discard(pno)
        mode = self.cont_view.view_mode
        self._render_queue[page_no] = {"page_no": page_no, "zoom": zoom,
                                      "view_mode": mode, "generation": self.worker.generation,
                                      "text_presentation": self.text_presentation}
        if not self._render_active and not self._render_dispatch_id:
            self._render_dispatch_id = GLib.idle_add(self._dispatch_render)

    def _dispatch_render(self):
        self._render_dispatch_id = 0
        if self._render_closed or self._render_active:
            return False
        cv = self.cont_view
        want = cv._visible_window()
        for pno, meta in list(self._render_queue.items()):
            if (pno not in want or meta["generation"] != self.worker.generation
                    or meta["view_mode"] != cv.view_mode
                    or abs(meta["zoom"] - cv.page_zoom(pno)) > 1e-9
                    or (cv.view_mode == "text" and
                        meta["text_presentation"] != self.text_presentation)):
                self._render_queue.pop(pno)
                cv._pending.discard(pno)
        if not self._render_queue:
            return False
        visible = cv._visible_pages()
        pno = min(self._render_queue, key=lambda p: (
            p not in visible, abs(p - cv.page_no),
            (p - cv.page_no) * cv._scroll_direction < 0))
        meta = self._render_queue.pop(pno)
        kwargs = {"page_no": pno, "zoom": meta["zoom"]}
        kind = RENDER_TEXT if meta["view_mode"] == "text" else RENDER
        if kind == RENDER_TEXT:
            kwargs["presentation"] = meta["text_presentation"]
        else:
            kwargs["highlight_masks"] = True
        self._render_active = True

        def done(result):
            self._render_active = False
            if self._render_closed:
                return
            try:
                if not result.ok:
                    cv._pending.discard(pno)
                self._on_rendered(result)
            finally:
                self._dispatch_render()

        self.worker.submit(Task(kind, kwargs, callback=done, meta=meta))
        return False

    def _deliver_pixmap(self, page_no, zoom, pix, from_cache=False, highlight_masks=(),
                        view_mode=None) -> None:
        """把渲染好的页交给连续视图。"""
        if abs(zoom - self.cont_view.page_zoom(page_no)) > 1e-9:
            return  # 缩放前的在途结果不能覆盖新尺寸,也不能清掉新请求。
        # 模式校验:切到文字层后,仍在途的图片层结果必须丢弃(反之亦然)。
        # 不校验的话屏幕上会一半图片层、一半文字层(实测踩到)。
        if view_mode is not None and view_mode != self.cont_view.view_mode:
            return
        self.cont_view.show_pixmap(pix, page_no=page_no, highlight_masks=highlight_masks)
        self._update_cache_status()

    def _on_rendered(self, res) -> None:
        """worker 渲染完成,回 UI 线程。"""
        if not res.ok:
            self._set_status(f"渲染失败: {res.error}")
            return
        # 代数校验:结果若来自旧文档(期间用户换了文件),直接丢弃
        if res.doc_generation != self.worker.generation:
            return
        meta = getattr(res, "meta", None) or {}
        if (meta.get("view_mode") == "text" and
                meta.get("text_presentation", "reading") != self.text_presentation):
            return

        pix = res.value["pixmap"]
        # 文字层渲染没有高亮遮罩(纯文字,不合成注记)
        masks = res.value.get("highlight_masks", ())
        self.last_render_ms = res.elapsed_ms
        page_no = meta.get("page_no", self.view.page_no)
        zoom = meta.get("zoom", self.view.page_zoom(page_no))
        if (meta.get("view_mode") == "text" and self.view.view_mode == "text"
                and abs(zoom - self.view.page_zoom(page_no)) < 1e-9):
            self._text_fallbacks[page_no] = res.value.get("source_fallback", "")
            self._text_warnings[page_no] = res.value.get("text_warning", "")

        # **不入 pixcache**:连续视图自己按"页窗口"持有常驻页(只留视口
        # 附近 5 页),而 pixcache 是 256 MB 的 LRU —— 滑过的页会一直堆到
        # 撑满,窗口化就白做了(实测积到 162.6 MB / 103.7 MB)。
        # 删掉单页模式后没有第二个消费者,这里彻底不写。
        self._deliver_pixmap(page_no, zoom, pix, from_cache=False, highlight_masks=masks,
                             view_mode=meta.get("view_mode"))
        self.page_entry.set_text(str(self.view.page_no + 1))

    def _restore_last_page(self) -> bool:
        """固定视口同步更新虚拟范围,后续缩放保留页内位置,无需猜延时。"""
        self._restore_id = 0
        pno = getattr(self, "_restore_page", 0)
        self.cont_view.fit_to_width()
        return self._do_restore_page(pno)

    def _do_restore_page(self, pno: int) -> bool:
        """真正执行跳页(等 fit 稳定之后)。"""
        self.cont_view.goto(pno)
        self.page_entry.set_text(str(self.cont_view.page_no + 1))
        self._update_cache_status()
        return False

    def _on_cont_page_changed(self, page_no: int) -> None:
        """滚动导致当前页变化 —— 同步页码框与状态栏。"""
        self.page_entry.set_text(str(page_no + 1))
        self._update_cache_status()

    def _update_cache_status(self) -> None:
        """状态栏:页码 / 缩放 / 常驻页与内存 / 脏标记。

        原来显示的是 pixcache 的统计,但连续模式自带页窗口、不走那个缓存,
        数字会误导(用户看到"缓存 58 页 / 103.7 MB"以为又在漏)。
        现在直接报连续视图真实常驻的页数与字节 —— 滑多远都应恒定。
        """
        cv = self.cont_view
        zoom = cv.page_zoom(cv.page_no)
        self.zoom_label.set_text(f"{zoom:.2f}×")
        dirty = " · ✎未保存" if self._dirty else ""
        mb = cv.resident_bytes() / 1048576
        proof = cv.view_mode == "text" and self.text_presentation == "proof"
        fallback = (self._text_fallbacks.get(cv.page_no, "")
                    if cv.view_mode == "text" and not proof else "")
        display_note = (" · OCR校对：原始文字层" if proof else
                        f" · {fallback}，已显示原页" if fallback else "")
        if proof and self._text_warnings.get(cv.page_no):
            display_note += f" · 检测到{self._text_warnings[cv.page_no]}（按原样显示）"
        self._sync_text_button()
        self._set_status(
            f"第 {cv.page_no + 1}/{self.page_count} 页 · "
            f"{zoom:.2f}× · 渲染 {self.last_render_ms:.0f} ms · "
            f"常驻 {len(cv._pixmaps)} 页 / {mb:.1f} MB{dirty}{display_note}")

    def _on_generation_change(self, gen: int) -> None:
        """文档代数变了 —— 旧对象全部作废。"""
        self._text_fallbacks.clear()
        self._text_warnings.clear()

    # ================= 事件处理 =================

    def _on_open_clicked(self, _btn) -> None:
        dlg = Gtk.FileChooserDialog(
            title="打开 PDF", parent=self,
            action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons("取消", Gtk.ResponseType.CANCEL,
                        "打开", Gtk.ResponseType.ACCEPT)
        dlg.set_select_multiple(True)
        f = Gtk.FileFilter()
        f.set_name("PDF 文件")
        f.add_pattern("*.pdf")
        dlg.add_filter(f)
        if dlg.run() == Gtk.ResponseType.ACCEPT:
            paths = dlg.get_filenames()
            dlg.destroy()
            # 空窗口接第一本;已有文档的窗口(包括未保存修改)保持原样。
            if paths and not self._path:
                self.open_file(paths.pop(0))
            from .app import launch_reader
            for path in paths:
                try:
                    launch_reader(path)
                except Exception as exc:
                    self._show_error("打开新窗口", exc)
        else:
            dlg.destroy()

    def _on_page_entry(self, entry) -> None:
        try:
            n = int(entry.get_text().strip())
        except ValueError:
            entry.set_text(str(self.view.page_no + 1))
            return
        self.view.goto(n - 1)

    def _on_toc_activated(self, tree, path, column) -> None:
        """兼容保留:跳页(M2 起双击由 _on_row_activated 处理编辑)。"""
        it = self.tree_store.get_iter(path)
        page = self.tree_store.get_value(it, 0)
        self.view.goto(page)

    def _on_key_press(self, _w, event) -> bool:
        if shortcuts.editing_text(self.get_focus(), event):
            return False
        action = self._shortcut_map.get(shortcuts.event_key(event))
        if action is None:
            return False
        callbacks = {
            "open": lambda: self._on_open_clicked(None),
            "save": self.save,
            "close": self.close,  # Gtk.Window.close → delete-event → 未保存提醒
            "find": lambda: self.toggle_search(True),
            "copy": self.copy_selection,
            "selection_mode": self.cycle_selection_mode,
            "highlight": self.add_highlight_from_selection,
            "zoom_in": self.view.zoom_in,
            "zoom_out": self.view.zoom_out,
            "zoom_fit": self.view.zoom_reset,
            "next_page": self.view.next_page,
            "prev_page": self.view.prev_page,
            "first_page": lambda: self.view.goto(0),
            "last_page": lambda: self.view.goto(self.page_count - 1),
            "night_mode": self.cycle_night_mode,
            "text_layer": self.cycle_view_mode,
            "next_match": self.search_next,
            "prev_match": self.search_prev,
            "clear": self._dismiss_search,
            "settings": self.show_settings,
        }
        callbacks[action]()
        return True

    def _dismiss_search(self) -> None:
        if self.searchbar.get_visible():
            self.toggle_search(False)
        else:
            self.clear_search()

    def _shortcut_label(self, action: str) -> str:
        return " / ".join(shortcuts.label(binding) for binding in self._bindings[action])

    # ================= M3:夜读 =================

    def cycle_night_mode(self) -> None:
        """热键 D / 按钮:三态循环 off → invert → soft → off。"""
        import time as _t
        t0 = _t.perf_counter()
        mode = darkmode.next_mode(self.view.night_mode)
        self.set_night_mode(mode)
        ms = (_t.perf_counter() - t0) * 1000.0
        self.last_switch_ms = ms
        self._sync_night_button()
        # M3-1 判据计时:从按键到新像素上屏
        self._set_status(
            f"夜读模式:{darkmode.MODE_LABELS[mode]} · 切换 {ms:.1f} ms")

    def _sync_night_button(self) -> None:
        if hasattr(self, "night_btn"):
            self.night_btn.set_label(
                f"夜读:{darkmode.MODE_LABELS.get(self.view.night_mode, '?')}")

    def set_night_mode(self, mode: str) -> None:
        self.view.set_night_mode(mode)
        self.cfg["night_mode"] = mode
        config.set_("night_mode", mode)
        self._sync_night_button()

    # ================= M7:文字层(图片层隐藏) =================

    def _on_text_presentation_changed(self, combo):
        mode = combo.get_active_id()
        if mode not in ("reading", "proof") or mode == self.text_presentation:
            return
        self.text_presentation = mode
        self.view.text_presentation = mode
        self.cfg["text_presentation"] = mode
        # Remember the choice for new windows, without changing another
        # already-open window that may be comparing the two presentations.
        config.set_("text_presentation", mode)
        self._text_fallbacks.clear()
        self._text_warnings.clear()
        self.view.set_view_mode("text", force=True)
        self._sync_text_button()
        self._update_cache_status()

    def cycle_view_mode(self) -> None:
        """热键 Ctrl+T / 按钮:图片层 ⇄ 文字层。

        文字层=只画 PDF 里可搜索的文字,页面的图片(扫描件底图)不出现。
        纯查看操作,不改文档、不标脏 —— 用户可以随时切回去。
        """
        import time as _t
        t0 = _t.perf_counter()
        try:
            mode = self.view.toggle_view_mode()
        except Exception as e:
            self._show_error("切换文字层", e)
            return
        ms = (_t.perf_counter() - t0) * 1000.0
        self._sync_text_button()
        if mode == "text":
            note = ("OCR校对：原始文字层" if self.text_presentation == "proof" else
                    "舒适阅读：异常扫描页自动显示原页")
            self._set_status(f"{note} · 切换 {ms:.1f} ms")
        else:
            self._set_status(f"文字层:关 · 显示原页面 · 切换 {ms:.1f} ms")

    def _sync_text_button(self) -> None:
        if hasattr(self, "text_btn"):
            # _build_toolbar 早于 self.view 建立,取不到时按默认"关"显示
            view = getattr(self, "view", None)
            on = getattr(view, "view_mode", "image") == "text"
            proof = on and self.text_presentation == "proof"
            fallback = (self._text_fallbacks.get(view.page_no, "") if on and not proof else "")
            text = ("文字层:校对" if proof else "文字层:原页" if fallback else
                    "文字层:开" if on else "文字层")
            if self.text_btn.get_label() != text:
                self.text_btn.set_label(text)
            self.text_btn.set_tooltip_text(
                "显示原始文字字形、字号和位置，不使用阅读修复或原页替代。" if proof else
                f"{fallback}；此页自动显示原始页面，保留公式与排版。" if fallback else
                "显示已有文字；异常扫描页自动显示原页。Ctrl+T 切换。")
            # set_label may replace the child. Reserve width on the new label
            # so toggling cannot resize the window and trigger fit-to-width.
            label = self.text_btn.get_child()
            width, _ = label.create_pango_layout("文字层:原页").get_pixel_size()
            label.set_size_request(width, -1)

    # ================= M4:搜索 =================

    def toggle_search(self, show: bool) -> None:
        self.search_toggle.set_active(show)
        # The toggled handler owns visibility and search state. Ctrl+F should
        # still focus the entry when the bar is already open.
        if show:
            self.search_entry.grab_focus()

    def _on_search_toggled(self, btn) -> None:
        show = btn.get_active()
        self.searchbar.set_visible(show)
        if show:
            self.search_entry.grab_focus()
            self._on_search_changed(self.search_entry)
        else:
            self.clear_search()
            self.cont_view.canvas.grab_focus()

    def _on_search_changed(self, _entry) -> None:
        """输入防抖 300 ms(M4)。"""
        # Invalidate the previous query immediately, including during debounce.
        self.clear_search()
        if self.searchbar.get_visible() and self.search_entry.get_text():
            self._search_timer = GLib.timeout_add(300, self._fire_search)

    def _fire_search(self) -> bool:
        self._search_timer = 0
        needle = self.search_entry.get_text()
        if not needle:
            self.clear_search()
            return False
        self._search_gen += 1
        req = self._search_gen

        def done(res):
            # M4-7:陈旧结果丢弃 —— 只有最新一次搜索的结果才上屏
            if req != self._search_gen:
                self._stale_dropped = getattr(self, "_stale_dropped", 0) + 1
                return
            if not res.ok:
                self.search_label.set_text("搜索失败")
                self._set_status(f"搜索失败: {res.error}")
                return
            v = res.value
            self._search_hits = v["hits"]
            self._search_idx = 0 if self._search_hits else -1
            self.last_search_ms = v.get("elapsed_ms", 0.0)
            self.last_search_for = v.get("baseline", 0)
            self.search_label.set_text(
                f"{len(self._search_hits)} 处(索引)"
                f" / {v.get('baseline', 0)} 处(search_for)")
            self._set_status(
                f"「{needle}」命中 {len(self._search_hits)} 处(字符索引)"
                f" · search_for 基准 {v.get('baseline', 0)} 处"
                f" · {v.get('elapsed_ms', 0):.0f} ms")
            if self._search_hits:
                self._goto_hit(0)

        self.worker.submit(Task(SEARCH, {"needle": needle}, callback=done))
        return False

    def clear_search(self) -> None:
        if self._search_timer:
            GLib.source_remove(self._search_timer)
            self._search_timer = 0
        self.worker.cancel_search()
        self._search_gen += 1          # 使在途结果作废(M4-6 取消)
        self._search_hits = []
        self._search_idx = -1
        if hasattr(self, "search_label"):
            self.search_label.set_text("")
        self.view.clear_overlay()

    def search_next(self) -> None:
        if not self._search_hits:
            return
        self._search_idx = (self._search_idx + 1) % len(self._search_hits)
        self._goto_hit(self._search_idx)

    def search_prev(self) -> None:
        if not self._search_hits:
            return
        self._search_idx = (self._search_idx - 1) % len(self._search_hits)
        self._goto_hit(self._search_idx)

    def _goto_hit(self, i: int) -> None:
        hit = self._search_hits[i]
        # G5:跳页会让旧选区(按页编号的下标)失效,主动清掉
        self._clear_selection()
        self.view.goto(hit["page"] - 1)
        self.view.set_overlay(hit.get("rects", []), self.page_count)
        self.search_label.set_text(
            f"{i + 1}/{len(self._search_hits)} 处"
            f" (索引) · {self.last_search_for} 处 (search_for)")

    # ================= M5:高亮批注 =================

    def _clear_selection(self) -> None:
        self._sel_anchor = self._sel_focus = self._sel_page = None
        self._sel_text = ""
        self._sel_ranges = None
        self._sel_context = None
        self._copy_pending = None
        self._selection_request = None

    def _on_drag_selection(self, start_pdf, end_pdf) -> None:
        """合并鼠标预览请求:worker 最多处理一个,只保留最新待处理端点。"""
        self._clear_selection()
        self._selection_request = (self.view._sel_page + 1, start_pdf, end_pdf,
                                   self.view._selection_serial, self.worker.generation,
                                   self.view.selection_mode)
        if not self._selection_inflight:
            self._dispatch_selection()

    def _dispatch_selection(self) -> None:
        page_no, start_pdf, end_pdf, serial, generation, mode = self._selection_request
        self._selection_request = None
        self._selection_inflight = True

        def done(res):
            self._selection_inflight = False
            try:
                if (serial != self.view._selection_serial or
                        generation != self.worker.generation or
                        res.doc_generation != generation):
                    return
                if not res.ok:
                    self._set_status(f"选字失败: {res.error}")
                    return
                v = res.value
                if v.get("start", -1) < 0 or v.get("end", -1) <= v.get("start", -1):
                    # 拖动尚未越过一个字符时保留鼠标锚点,不要中断整个手势。
                    self.view.set_selection_rects([])
                    self._set_status("选区未落在文字上")
                    return
                self._sel_anchor, self._sel_focus = v["start"], v["end"]
                self._sel_page = page_no
                self._sel_ranges = v.get("ranges")
                self._sel_text = v.get("copy_text", v.get("text", ""))
                self._sel_context = (serial, generation)
                self.view.set_selection_rects(v.get("rects", []))
                n = sum(b - a for a, b in (self._sel_ranges or [[v["start"], v["end"]]]))
                txt = v.get("text", "")
                preview = (txt[:18] + "…") if len(txt) > 18 else txt
                hint = self._shortcut_label("highlight") or "请在设置中指定快捷键"
                copy_hint = self._shortcut_label("copy") or "未设置"
                self._set_status(f"已选 {n} 字:「{preview}」· 复制：{copy_hint} · 添加高亮：{hint}")
                if self._copy_pending == self._sel_context:
                    self.copy_selection()
            finally:
                if self._selection_request is not None:
                    self._dispatch_selection()

        self.worker.submit(Task(
            SELECT_TEXT,
            {"page": page_no, "x0": start_pdf[0], "y0": start_pdf[1],
             "x1": end_pdf[0], "y1": end_pdf[1], "mode": mode},
            callback=done))

    def copy_selection(self) -> None:
        """复制经过版本校验的选区值;不在 UI 线程访问 PDF 对象。"""
        context = (self.view._selection_serial, self.worker.generation)
        if (self._sel_text and self._sel_context == context
                and self._sel_page == self.view.page_no + 1):
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text(self._sel_text, -1)
            clipboard.store()
            self._copy_pending = None
            self._set_status(f"已复制 {len(self._sel_text)} 个字符")
        elif self._selection_inflight or self._selection_request is not None:
            # 松开鼠标后立即按 Ctrl+C,等待当前这次选字返回即可复制。
            self._copy_pending = context
            self._set_status("正在读取选中文字…")
        else:
            self._set_status("请先拖选要复制的文字")

    def add_highlight_from_selection(self) -> None:
        """对当前选区加高亮。选区由 M4 的字符索引提供(M5 复用)。"""
        if self._sel_anchor is None or self._sel_focus is None:
            hint = self._shortcut_label("highlight") or "请在设置中指定快捷键"
            self._set_status(f"先拖选一段文字，再添加高亮：{hint}")
            return
        # G5:选区下标是**按页**编的。若用户在拖选后翻了页/搜了索,
        # 当前页已不是选区所在页 —— 此时照旧套用下标会把高亮加到错误的页上
        # (实测:第 7 页选的「总则」,搜索跳到第 3 页后按 Ctrl+H,高亮落在第 3 页)。
        cur = self.view.page_no + 1
        if self._sel_page is not None and self._sel_page != cur:
            self._set_status(
                f"选区在第 {self._sel_page} 页,当前在第 {cur} 页 —— 请重新拖选")
            self._sel_anchor = self._sel_focus = self._sel_page = None
            self.view.clear_overlay()
            return
        a, b = sorted((self._sel_anchor, self._sel_focus))
        if a == b:
            self._set_status("选区为空")
            return

        def done(res):
            if not res.ok:
                self._set_status(f"加高亮失败: {res.error}")
                return
            v = res.value
            self.last_annot_incr = v.get("incr_bytes", 0)
            self._mark_dirty()
            self._set_status(
                f"已加高亮 {v.get('added', 1)} 处 · {v.get('quads_count', 0)} 个 quad")
            self.view.clear_overlay()  # 显示实际高亮颜色,不叠着蓝色选区。
            # 重渲染当前页让高亮可见
            self._force_rerender()

        self.worker.submit(Task(
            ADD_ANNOT,
            {"page": self._sel_page or cur, "start": a, "end": b,
             "ranges": self._sel_ranges,
             "color": self._highlight_color, "opacity": self._highlight_opacity},
            callback=done))

    def delete_annot_at(self, page_no: int, annot_index: int) -> None:
        def done(res):
            if not res.ok:
                self._set_status(f"删除高亮失败: {res.error}")
                return
            self._mark_dirty()
            self._set_status(f"已删除高亮 · 当前 {res.value.get('remaining', 0)} 处")
            self._force_rerender()
        self.worker.submit(Task(
            DEL_ANNOT, {"page": page_no + 1, "annot_index": annot_index},
            callback=done))

    def _force_rerender(self) -> None:
        """保存/批注后让当前页重新渲染,好让新加的高亮立刻可见。

        连续视图按页窗口缓存底图,不清掉的话重渲染拿到的还是旧图
        (批注后高亮不显示)。只丢当前页,相邻页不受影响。
        """
        cv = self.cont_view
        pno = cv.page_no
        cv.invalidate_page(pno)

    # ================= M6:异常兜底 / 精简重写 =================

    def _guard(self, what: str):
        """装饰器:把任意 UI 回调的异常转成弹窗,不让堆栈把进程带走(M6-4)。

        为何需要:GTK 信号回调里抛出的异常不会终止进程,但会被 GLib 吞掉并
        打印到 stderr,用户看到的是"点了没反应"。显式弹窗至少让故障可见。
        """
        def deco(fn):
            def wrapped(*a, **kw):
                try:
                    return fn(*a, **kw)
                except Exception as e:      # noqa: BLE001 —— 兜底就是要抓全部
                    self._show_error(what, e)
                    return None
            wrapped.__name__ = getattr(fn, "__name__", "wrapped")
            return wrapped
        return deco

    def _show_error(self, what: str, exc: BaseException) -> None:
        import traceback
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        try:
            dlg = Gtk.MessageDialog(
                transient_for=self, modal=True, destroy_with_parent=True,
                message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
                text=f"{what}时出错")
            dlg.format_secondary_text(detail)
            dlg.run()
            dlg.destroy()
        except Exception:
            pass
        try:
            self._set_status(f"{what}失败:{detail}")
        except Exception:
            pass

    def shrink_rewrite(self, dry_run: bool = False) -> None:
        """M6:接通「精简重写」(Z2 的 GUI 出口)。

        PDF 增量保存会累积历史版本,文件越来越大。精简重写用另存新文件的方式
        回收这些累积。通过已安装的模块调用,不依赖启动时的工作目录。
        """
        if not self._path:
            self._set_status("尚未打开文件")
            return
        if self._dirty:
            self._set_status("有未保存改动，请先保存再精简")
            return
        import subprocess, sys as _sys
        before = os.path.getsize(self._path)
        cmd = [_sys.executable, "-m", "nightread.shrink", self._path]
        if dry_run:
            cmd.append("--dry-run")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except Exception as e:
            self._show_error("精简重写", e)
            return
        after = os.path.getsize(self._path)
        self.last_shrink = {"before_bytes": before, "after_bytes": after,
                            "returncode": r.returncode}
        if r.returncode != 0:
            self._set_status(f"精简失败:{r.stderr.strip()[:80]}")
            return
        self._set_status(
            f"精简完成:{before:,} → {after:,} B "
            f"({'节省' if after < before else '增加'} {abs(before - after):,} B)")
        self._load_toc()
        self._force_rerender()

    def set_highlight_color(self, name: str) -> None:
        """M6 选做:六色高亮。设置后新加的高亮用该色。"""
        from .annotate import COLORS
        if name in COLORS:
            self._highlight_color_name = name
            self._highlight_color = COLORS[name]
            self.cfg["highlight_color"] = name
            config.set_("highlight_color", name)
            self.color_btn.set_label(f"高亮:{name}")
            self._set_status(f"高亮颜色:{name}")

    def show_settings(self) -> None:
        from .settings import SettingsWindow
        if self._settings_window is None:
            self._settings_window = SettingsWindow(parent=self, on_apply=self._apply_preferences)
            self._settings_window.connect("destroy", self._settings_closed)
        self._settings_window.show_all()
        self._settings_window.present()

    def _settings_closed(self, _window) -> None:
        self._settings_window = None

    def cycle_selection_mode(self) -> None:
        mode = "rectangle" if self.view.selection_mode == "text" else "text"
        cfg = dict(self.cfg, selection_mode=mode)
        self._apply_preferences(cfg)
        config.set_("selection_mode", mode)
        self._set_status("区域选择：拖出选框后可复制或添加高亮" if mode == "rectangle"
                         else "连续选字：按文字阅读顺序拖选")

    def _apply_preferences(self, cfg: dict) -> None:
        from .annotate import COLORS
        from .comfort import parameters
        from .settings import apply_ui_font_scale
        for key in config.PREFERENCE_KEYS:
            self.cfg[key] = cfg[key]
        apply_ui_font_scale(cfg["ui_font_scale"])
        self._bindings = shortcuts.effective(cfg["shortcuts"])
        self._shortcut_map = shortcuts.keymap(self._bindings)
        for action, button in self._shortcut_buttons.items():
            button.set_tooltip_text(self._shortcut_label(action) or "快捷键未设置")
        if self.view.night_mode != cfg["night_mode"]:
            self.view.set_night_mode(cfg["night_mode"])
        self._sync_night_button()
        self.view.set_comfort_params(parameters(cfg))
        self.view.set_selection_mode(cfg["selection_mode"])
        self.selection_btn.set_label("选字:区域" if cfg["selection_mode"] == "rectangle" else "选字:连续")
        self._highlight_color_name = cfg["highlight_color"]
        self._highlight_color = COLORS[self._highlight_color_name]
        self._highlight_opacity = cfg["highlight_opacity"]
        self.color_btn.set_label(f"高亮:{self._highlight_color_name}")
        self.sidebar.set_no_show_all(not cfg["sidebar_visible"])
        self.sidebar.set_visible(cfg["sidebar_visible"])

    def _watch_preferences(self) -> None:
        try:
            self._config_monitor = Gio.File.new_for_path(config.CONFIG_PATH).monitor_file(
                Gio.FileMonitorFlags.WATCH_MOVES, None)
            self._config_monitor.connect("changed", self._preferences_changed)
        except GLib.Error:
            pass

    def _preferences_changed(self, *_args) -> None:
        if self._prefs_reload_id:
            GLib.source_remove(self._prefs_reload_id)
        self._prefs_reload_id = GLib.timeout_add(80, self._reload_preferences)

    def _reload_preferences(self) -> bool:
        self._prefs_reload_id = 0
        self._apply_preferences(config.load())
        return False

    # ================= 状态与收尾 =================

    def _initial_window_size(self):
        width, height = self.cfg["window_width"], self.cfg["window_height"]
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0) if display else None
        if monitor:
            work = monitor.get_workarea()
            width = min(width, max(1, work.width - 32))
            height = min(height, max(1, work.height - 48))
        return width, height

    def _queue_geometry_save(self):
        if self._geometry_save_id:
            GLib.source_remove(self._geometry_save_id)
        self._geometry_save_id = GLib.timeout_add(350, self._save_window_geometry)

    def _on_window_configure(self, _window, event):
        # get_size() during destroy reports the dismantled widget's minimum
        # (for example 1653 x 181), not the last visible window dimensions.
        if (not self._window_maximized and not self._window_fullscreen
                and event.width >= 480 and event.height >= 320):
            self._normal_size = (event.width, event.height)
            self._queue_geometry_save()
        return False

    def _on_window_state(self, _window, event):
        if event.changed_mask & Gdk.WindowState.MAXIMIZED:
            self._window_maximized = bool(event.new_window_state & Gdk.WindowState.MAXIMIZED)
        if event.changed_mask & Gdk.WindowState.FULLSCREEN:
            self._window_fullscreen = bool(event.new_window_state & Gdk.WindowState.FULLSCREEN)
        self._queue_geometry_save()
        return False

    def _on_sidebar_position(self, paned, _pspec):
        if self.get_mapped() and paned.get_position() >= 160:
            self._sidebar_position = paned.get_position()
            self._queue_geometry_save()

    def _window_geometry(self):
        return {"window_width": self._normal_size[0],
                "window_height": self._normal_size[1],
                "window_maximized": self._window_maximized,
                "sidebar_width": self._sidebar_position}

    def _save_window_geometry(self):
        self._geometry_save_id = 0
        config.update(self._window_geometry())
        return False

    def _set_status(self, text: str) -> None:
        self.statusbar.pop(self._status_ctx)
        self.statusbar.push(self._status_ctx, text)

    def _on_destroy(self, _w) -> None:
        self._render_closed = True
        self._render_queue.clear()
        if self._render_dispatch_id:
            GLib.source_remove(self._render_dispatch_id)
            self._render_dispatch_id = 0
        if self._memory_reporter is not None:
            self._memory_reporter.close()
        self.clear_search()
        if self._geometry_save_id:
            GLib.source_remove(self._geometry_save_id)
            self._geometry_save_id = 0
        if self._settings_window is not None:
            self._settings_window.destroy()
        if self._config_monitor is not None:
            self._config_monitor.cancel()
        if self._prefs_reload_id:
            GLib.source_remove(self._prefs_reload_id)
            self._prefs_reload_id = 0
        if self._restore_id:
            GLib.source_remove(self._restore_id)
            self._restore_id = 0
        try:
            config.update({**self._window_geometry(), "zoom": self.view.zoom,
                           "last_page": self.view.page_no if self.cfg["remember_position"] else 0,
                           "last_file": self._path or ""})
        except Exception:
            pass
        self.worker.submit(Task(CLOSE, {}))
        self.worker.stop()

    def _on_delete_event(self, _w, _ev) -> bool:
        """关窗前拦截未保存改动(M2-9)。返回 True = 阻止关闭。"""
        if self._close_after_save:
            return True
        if self._dirty and not self._confirm_discard("关闭窗口", close_after_save=True):
            return True          # 取消:不关
        return False
