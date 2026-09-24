"""独占 Document 的单线程任务队列 —— K3 的载体。

对应 `docs/stages/M1-skeleton.md` 契约 K3。

为什么必须有这一层:
    PyMuPDF 的 Document / Page / Pixmap **不是线程安全的**。一旦 UI 线程
    与后台线程同时碰同一个 Document,轻则拿到失效对象,重则段错误
    (整个进程消失,没有 Python 异常可捕获)。

    所以本模块是**唯一**允许持有 fitz 文档对象的地方。UI 线程只能通过
    `submit()` 投递任务、在回调里拿结果,永远不直接碰文档。

契约要点(M1 文件内联的 K3):
    · 单 threading.Thread + queue.Queue
    · submit(task) -> 回调
    · 结果回 UI 一律经 GLib.idle_add
    · 每个结果带 (doc_generation, request_id)

M1 阶段实现的任务类型覆盖后续阶段所需的全部:
    Open Close Render GetToc SetTocItem SetToc
    BuildIndex Search AddAnnot DelAnnot Save Shrink
M1 只用到前三个,其余在 M2–M5 填入实现;此处先把分发骨架立好,
后续阶段"只往里加任务类型,不改结构"。
"""

from __future__ import annotations

import math
import queue
import re
import statistics
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import fitz

try:
    from gi.repository import GLib
    _HAVE_GLIB = True
except Exception:  # 便于无 GUI 环境下做单元测试
    _HAVE_GLIB = False


# --------------------------------------------------------------------------
# 任务定义
# --------------------------------------------------------------------------

@dataclass
class Task:
    """一个文档操作请求。

    kind     任务类型(见下方常量)
    kwargs   传给处理函数的参数
    callback 结果回调。签名 callback(result: Result)。
             若为 None 则只执行不通知(如 Close)。
    """
    kind: str
    kwargs: dict = field(default_factory=dict)
    callback: Optional[Callable[["Result"], None]] = None
    # 调用方自用的透传字段(原样回填到 Result.meta)。
    # 连续滚动会同时请求多页,回调里靠它知道这一份结果属于哪一页。
    meta: Optional[dict] = None


@dataclass
class Result:
    """任务结果。

    ok       是否成功
    value    返回值(成功时)
    error    异常对象(失败时)
    tb       异常堆栈文本,便于定位
    doc_generation  结果产生时的文档代数 —— 与请求代数不符即为"在途作废"
    request_id      请求序号,用于丢弃过期结果
    elapsed_ms      该任务耗时
    """
    ok: bool
    value: Any = None
    error: Optional[BaseException] = None
    tb: str = ""
    doc_generation: int = 0
    request_id: int = 0
    elapsed_ms: float = 0.0
    meta: Optional[dict] = None


# 任务类型常量 —— K3 要求 worker 至少支持这些
OPEN = "Open"
CLOSE = "Close"
RENDER = "Render"
RENDER_TEXT = "RenderText"
SEARCH = "Search"
BUILD_INDEX = "BuildIndex"
GET_TOC = "GetToc"
SET_TOC_ITEM = "SetTocItem"
SET_TOC = "SetToc"
ADD_ANNOT = "AddAnnot"
DEL_ANNOT = "DelAnnot"
GET_ANNOTS = "GetAnnots"
SELECT_TEXT = "SelectText"
SAVE = "Save"
SHRINK = "Shrink"


class _TextOnlyDevice(fitz.mupdf.FzDevice2):
    """Forward original glyphs to a draw device, including invisible OCR text.

    MuPDF supplies the original fonts, glyph positions and complete transform.
    Images, paths, shadings and their clipping/masking operations are not
    forwarded. Text is opaque black so white/invisible text is readable too.
    A geometry marker avoids painting paired fill/stroke-and-clip text twice.
    Borrowed callback pointers are consumed synchronously, never retained.
    """

    def __init__(self, draw, repair=True):
        super().__init__()
        self.draw = draw
        self.repair = repair
        self.colorspace = fitz.mupdf.fz_device_rgb()
        self.params = fitz.mupdf.FzColorParams()
        self.spans = 0  # Number of text drawing batches, not extracted spans.
        self._painted_transform = None
        self._painted_bounds = None
        self._font_mapping = {}
        self._fallback_fonts = {}
        self.use_virtual_fill_text()
        self.use_virtual_stroke_text()
        self.use_virtual_ignore_text()
        self.use_virtual_clip_text()
        self.use_virtual_clip_stroke_text()

    def _fill(self, text, ctm):
        repaired = self._repair_text(text) if self.repair else None
        fitz.mupdf.ll_fz_fill_text(
            self.draw.m_internal, repaired.m_internal if repaired else text,
            ctm, self.colorspace.m_internal,
            (0, 0, 0), 1, self.params.internal())
        self.spans += 1
        self._remember(text, ctm)

    def _remember(self, text, ctm):
        self._painted_transform = (ctm.a, ctm.b, ctm.c, ctm.d, ctm.e, ctm.f)
        bounds = fitz.mupdf.ll_fz_bound_text(text, None, ctm)
        self._painted_bounds = (bounds.x0, bounds.y0, bounds.x1, bounds.y1)

    def _repair_text(self, text):
        """Use Unicode for unmapped glyphs, keeping each original text origin.

        Some OCR PDFs contain unrelated glyph IDs / fonts without a Unicode
        cmap. Their ToUnicode text is readable but the glyphs are not. Native
        fonts still win when their mapping agrees. Shaped scripts and ligature
        continuations must not be mistaken for broken nominal glyph mappings.
        """
        mupdf = fitz.mupdf
        records, changed = [], False
        raw = text.head
        while raw:
            span = mupdf.FzTextSpan(raw)
            font = span.font()
            for i in range(raw.len):
                item = span.items(i)
                replacement = None
                u = item.ucs
                # Broken subsets sometimes use .notdef (a box) for spaces.
                # Positions are explicit, so omitting these glyphs preserves
                # spacing without painting a misleading visible character.
                if 0 <= u <= 0x10ffff and chr(u).isspace():
                    changed = True
                    continue
                continuation = i + 1 < raw.len and span.items(i + 1).gid < 0
                if (item.gid >= 0 and 0 < u <= 0x10ffff
                        and not continuation):
                    key = (int(font.m_internal.this), u)
                    if key not in self._font_mapping:
                        # Keep the font reference with its cached mapping, so
                        # a freed font's pointer cannot be reused for this key.
                        self._font_mapping[key] = (font, mupdf.fz_encode_character(font, u))
                    encoded = self._font_mapping[key][1]
                    nominal = (u < 128 or 0x3000 <= u <= 0x9fff
                               or 0xff00 <= u <= 0xffef)
                    if not encoded or (nominal and encoded != item.gid):
                        name = 'helv' if u < 128 else 'cjk'
                        if name not in self._fallback_fonts:
                            self._fallback_fonts[name] = fitz.Font(name)
                        fallback = self._fallback_fonts[name]
                        glyph = fallback.has_glyph(u)
                        if glyph:
                            replacement = (fallback.this, glyph)
                            changed = True
                records.append((span, item, replacement))
            raw = raw.next
        if not changed:
            return None

        repaired = mupdf.FzText()
        for span, item, replacement in records:
            base, font = span.trm(), span.font()
            trm = mupdf.FzMatrix(base.a, base.b, base.c, base.d, item.x, item.y)
            glyph = item.gid
            if replacement:
                font, glyph = replacement
                old_advance = mupdf.fz_advance_glyph(span.font(), item.gid, span.m_internal.wmode)
                new_advance = mupdf.fz_advance_glyph(font, glyph, span.m_internal.wmode)
                if old_advance > 0 and new_advance > 0:
                    scale = old_advance / new_advance
                    if span.m_internal.wmode:
                        trm.c *= scale
                        trm.d *= scale
                    else:
                        trm.a *= scale
                        trm.b *= scale
                # Malformed OCR fonts can encode a single digit several ems
                # wide. Shrink the replacement within that cell, never move
                # following characters or expand beyond their original space.
                width, height = math.hypot(trm.a, trm.b), math.hypot(trm.c, trm.d)
                if item.ucs < 128 and not span.m_internal.wmode and width > height * 1.5:
                    trm.a *= height / width
                    trm.b *= height / width
            mupdf.fz_show_glyph(repaired, font, trm, glyph, item.ucs,
                               span.m_internal.wmode, span.m_internal.bidi_level,
                               span.m_internal.markup_dir, span.m_internal.language)
        return repaired

    def fill_text(self, ctx, text, ctm, colorspace, color, alpha, params):
        self._fill(text, ctm)

    def stroke_text(self, ctx, text, stroke, ctm, colorspace, color, alpha, params):
        # The stroke API takes a C float pointer, unlike ll_fz_fill_text.
        mupdf = fitz.mupdf
        repaired = self._repair_text(text) if self.repair else None
        black = mupdf.new_floats(3)
        try:
            for i in range(3):
                mupdf.floats_setitem(black, i, 0)
            mupdf.ll_fz_stroke_text(
                self.draw.m_internal, repaired.m_internal if repaired else text, stroke, ctm,
                self.colorspace.m_internal, black, 1, self.params.internal())
            self.spans += 1
            self._remember(text, ctm)
        finally:
            mupdf.delete_floats(black)

    def ignore_text(self, ctx, text, ctm):
        self._fill(text, ctm)

    def clip_text(self, ctx, text, ctm, scissor):
        # Clip-only text (PDF render mode 7) must also become readable.
        # Modes 4/5/6 already sent fill/stroke callbacks for the same glyphs.
        # MuPDF can clone the text between stroke and clip, so compare the
        # geometry too. Consume this marker after the immediately paired clip.
        bounds = fitz.mupdf.ll_fz_bound_text(text, None, ctm)
        if (self._painted_bounds != (bounds.x0, bounds.y0, bounds.x1, bounds.y1)
                or self._painted_transform != (ctm.a, ctm.b, ctm.c, ctm.d, ctm.e, ctm.f)):
            self._fill(text, ctm)
        self._painted_bounds = self._painted_transform = None

    def clip_stroke_text(self, ctx, text, stroke, ctm, scissor):
        self.clip_text(ctx, text, ctm, scissor)


def _scan_text_problem(page):
    """Detect unusable hidden OCR on a scanned page; never guess new formulas.

    Native text and ordinary hidden text still use the glyph renderer. Only a
    substantial scan with broken math markup, conflicting adjacent title sizes,
    or a narrow column of replacement glyphs uses the source page. This is a
    conservative display check, not OCR.
    """
    if not page.get_images():
        return "", 0
    traces = page.get_texttrace()
    hidden = [s for s in traces if s["type"] == 3 and s["chars"]]
    if not hidden:
        return "", len(traces)
    bounds = fitz.Rect(0, 0, page.cropbox.width, page.cropbox.height)
    covered = sum((fitz.Rect(info["bbox"]) & bounds).get_area()
                  for info in page.get_image_info())
    if covered < bounds.get_area() * .65:
        return "", len(traces)
    for span in traces:
        chars = [c[0] for c in span["chars"]
                 if 0 <= c[0] <= 0x10ffff and not chr(c[0]).isspace()]
        # Failed CJK insertion into a Latin font can leave white middle dots
        # beside a newer OCR layer. Revealing text exposes both layers. A long,
        # narrow column dominated by replacement glyphs is not a TOC leader.
        replacements = sum(c in (0xb7, 0xfffd) for c in chars)
        box = fitz.Rect(span["bbox"])
        if (len(chars) >= 24 and replacements >= len(chars) * .65
                and box.width < bounds.width * .35
                and box.height > span["size"] * 4):
            return "残留乱码文字", len(traces)
    text = "\n".join("".join(chr(c[0]) for c in s["chars"]
                             if 0 <= c[0] <= 0x10ffff) for s in hidden)
    if re.search(r"\\[A-Za-z]{2,}|[_^]\s*\{", text) or text.count("$") >= 2:
        return "公式文字不完整", len(traces)
    typical = statistics.median(s["size"] for s in hidden)
    for span in hidden:
        if len(span["chars"]) > 2 or span["size"] < typical * 2.2:
            continue
        a = fitz.Rect(span["bbox"])
        for other in hidden:
            if other is span or len(other["chars"]) > 2 or span["size"] < other["size"] * 1.8:
                continue
            b = fitz.Rect(other["bbox"])
            overlap = min(a.y1, b.y1) - max(a.y0, b.y0)
            gap = max(a.x0 - b.x1, b.x0 - a.x1, 0)
            if overlap > min(a.height, b.height) * .4 and gap < span["size"] * 2:
                return "相邻文字字号失真", len(traces)
    return "", len(traces)


class DocWorker:
    """单线程文档 worker。

    生命周期:
        w = DocWorker(on_generation_change=...)   # 建线程(未开文档)
        w.submit(Task(OPEN, {"path": p}, cb))
        ...
        w.stop()                                  # 停线程并关文档
    """

    def __init__(self, on_generation_change: Optional[Callable[[int], None]] = None):
        self._q: "queue.Queue[Optional[Task]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._doc: Optional[fitz.Document] = None
        self._path: Optional[str] = None
        self._generation = 0
        self._req = 0
        self._on_gen_change = on_generation_change
        self._stopped = threading.Event()
        # 文档对象只在 worker 线程碰,故用普通属性即可;此锁只保护代数的读写
        self._lock = threading.Lock()
        # M4:字符索引缓存(worker 线程独占)与取消令牌
        self._index = None
        self._text_page_decisions = {}
        self._search_token = 0

    def cancel_search(self) -> None:
        """M4-6:请求中止在途的索引构建/搜索。UI 线程调用,不阻塞。"""
        self._search_token += 1

    # ---------------- 对外 API(UI 线程调用) ----------------

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def submit(self, task: Task) -> int:
        """投递任务,返回 request_id。UI 线程调用,不阻塞。"""
        with self._lock:
            self._req += 1
            rid = self._req
        # 把 request_id 塞进 kwargs 让处理函数能拿到
        task.kwargs.setdefault("_request_id", rid)
        self._q.put(task)
        return rid

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stopped.clear()
        self._thread = threading.Thread(target=self._run, name="nightread-docworker",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        """停线程。先 Close 再投毒丸。"""
        self._q.put(None)
        if self._thread:
            self._thread.join(timeout=timeout)

    # ---------------- worker 线程内部 ----------------

    def _bump_generation(self) -> int:
        """文档代数自增。任何"换文档/使旧对象失效"的操作都要调。

        UI 侧收到结果时比对代数:不符说明结果来自旧文档,直接丢弃。
        """
        self._text_page_decisions.clear()
        with self._lock:
            self._generation += 1
            g = self._generation
        if self._on_gen_change:
            self._post_to_ui(lambda: self._on_gen_change(g))
        return g

    @staticmethod
    def _post_to_ui(fn: Callable[[], None]) -> None:
        """把回调送回 UI 线程。有 GLib 就走主循环,没有就直接调(测试用)。"""
        if _HAVE_GLIB:
            GLib.idle_add(fn)
        else:
            fn()

    def _run(self) -> None:
        while not self._stopped.is_set():
            task = self._q.get()
            if task is None:                       # 毒丸
                break
            t0 = time.perf_counter()
            rid = task.kwargs.get("_request_id", 0)
            try:
                value = self._dispatch(task)
                res = Result(ok=True, value=value, doc_generation=self.generation,
                             request_id=rid, meta=task.meta,
                             elapsed_ms=(time.perf_counter() - t0) * 1000)
            except Exception as e:  # noqa: BLE001 - 任何异常都要变成 Result,不能杀死 worker
                res = Result(ok=False, error=e, tb=traceback.format_exc(),
                             doc_generation=self.generation, request_id=rid,
                             meta=task.meta,
                             elapsed_ms=(time.perf_counter() - t0) * 1000)
            if task.callback:
                # 闭包捕获 res,避免循环变量问题
                self._post_to_ui(lambda r=res, cb=task.callback: cb(r))
        # 收尾:关文档
        try:
            if self._doc is not None:
                self._doc.close()
                self._doc = None
        except Exception:
            pass

    # ---------------- 任务分发 ----------------

    def _dispatch(self, task: Task) -> Any:
        k = task.kind
        kw = {x: y for x, y in task.kwargs.items() if not x.startswith("_")}

        if k == OPEN:
            return self._op_open(**kw)
        if k == CLOSE:
            return self._op_close()
        if k == RENDER:
            return self._op_render(**kw)
        if k == RENDER_TEXT:
            return self._op_render_text(**kw)
        if k == GET_TOC:
            return self._op_get_toc(**kw)
        if k == SET_TOC_ITEM:
            return self._op_set_toc_item(**kw)
        if k == SET_TOC:
            return self._op_set_toc(**kw)
        if k == SEARCH:
            return self._op_search(**kw)
        if k == BUILD_INDEX:
            return self._op_build_index(**kw)
        if k == ADD_ANNOT:
            return self._op_add_annot(**kw)
        if k == DEL_ANNOT:
            return self._op_del_annot(**kw)
        if k == GET_ANNOTS:
            return self._op_get_annots(**kw)
        if k == SELECT_TEXT:
            return self._op_select_text(**kw)
        if k == SAVE:
            return self._op_save(**kw)
        if k == SHRINK:
            return self._op_shrink(**kw)
        raise ValueError(f"未知任务类型: {k}")

    # ---------------- 各操作实现 ----------------

    def _require_doc(self) -> fitz.Document:
        if self._doc is None:
            raise RuntimeError("尚未打开文档")
        return self._doc

    def _op_open(self, path: str) -> dict:
        if self._doc is not None:
            self._doc.close()
            self._doc = None
            self._bump_generation()
        d = fitz.open(path)
        self._doc = d
        self._path = path
        self._index = None          # 换文件 → 字符索引作废
        self._search_token += 1     # 中止在途搜索(C6)
        self._bump_generation()
        # Only page geometry crosses to GTK; all PDF objects stay in this worker.
        page_sizes = [(p.rect.width, p.rect.height) for p in d]
        return {"path": path, "pages": len(d), "toc": len(d.get_toc()),
                "page_size": page_sizes[0] if page_sizes else None,
                "page_sizes": page_sizes,
                "generation": self.generation}

    def _op_close(self) -> dict:
        if self._doc is not None:
            self._doc.close()
            self._doc = None
            self._path = None
            self._index = None
            self._search_token += 1
            self._bump_generation()
        return {"closed": True}

    def _op_render(self, page_no: int, zoom: float, highlight_masks: bool = False):
        """渲染一页为 Pixmap。

        只在本线程做。返回的 Pixmap 由调用方持有,可跨线程传递
        (它已是独立的内存副本,不再引用文档内部结构)。
        """
        d = self._require_doc()
        if not (0 <= page_no < len(d)):
            raise IndexError(f"页码越界: {page_no}(共 {len(d)} 页)")
        page = d[page_no]
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        if not highlight_masks:
            return pix
        masks = []
        for annot in page.annots():
            if annot.type[0] != fitz.PDF_ANNOT_HIGHLIGHT:
                continue
            if annot.flags & (fitz.PDF_ANNOT_IS_INVISIBLE | fitz.PDF_ANNOT_IS_HIDDEN |
                              fitz.PDF_ANNOT_IS_NO_VIEW):
                continue
            appearance = annot.get_pixmap(matrix=mat, alpha=True)
            alpha = bytes(appearance.samples_mv[appearance.n - 1::appearance.n])
            peak = max(alpha, default=0)
            if peak:
                masks.append({"x": appearance.x - pix.x, "y": appearance.y - pix.y,
                              "width": appearance.width, "height": appearance.height,
                              "alpha": alpha, "peak": peak})
        return {"pixmap": pix, "highlight_masks": masks}

    def _op_render_text(self, page_no: int, zoom: float, presentation: str = "reading"):
        """Render text, retaining the source scan if its hidden OCR is damaged.

        Rendering uses the worker's original page without editing it or
        reconstructing text. MuPDF retains fonts, spacing, baselines, crop box
        and rotation; normal invisible OCR glyphs are painted as black text.
        Broken math or inconsistent title sizes fall back to the original page.
        """
        if presentation not in ("reading", "proof"):
            raise ValueError(f"未知文字层显示方式: {presentation}")
        d = self._require_doc()
        if not (0 <= page_no < len(d)):
            raise IndexError(f"页码越界: {page_no}(共 {len(d)} 页)")
        src = d[page_no]
        if page_no not in self._text_page_decisions:
            self._text_page_decisions[page_no] = _scan_text_problem(src)
        reason, spans = self._text_page_decisions[page_no]
        if reason and presentation == "reading":
            result = self._op_render(page_no, zoom, highlight_masks=True)
            result.update(spans=spans, source_fallback=reason)
            return result

        mupdf = fitz.mupdf
        pix = fitz.Pixmap(fitz.csRGB, (src.rect * fitz.Matrix(zoom, zoom)).irect, False)
        pix.clear_with(255)
        draw = mupdf.fz_new_draw_device(mupdf.FzMatrix(1, 0, 0, 1, 0, 0), pix.this)
        try:
            device = _TextOnlyDevice(draw, repair=presentation == "reading")
            try:
                mupdf.fz_run_page_contents(
                    src.this, device, mupdf.FzMatrix(zoom, 0, 0, zoom, 0, 0),
                    mupdf.FzCookie())
            finally:
                mupdf.fz_close_device(device)
        finally:
            mupdf.fz_close_device(draw)
        return {"pixmap": pix, "spans": device.spans, "text_warning": reason}

    def _op_get_toc(self, simple: bool = True) -> list:
        d = self._require_doc()
        return d.get_toc(simple=simple)

    def _op_set_toc_item(self, index: int, title: str) -> dict:
        """改大纲某条标题 —— K2:用 set_toc_item,不要重建整个大纲。"""
        d = self._require_doc()
        d.set_toc_item(index, title=title)
        return {"index": index, "title": title}

    def _op_set_toc(self, toc: list) -> dict:
        d = self._require_doc()
        d.set_toc(toc)
        return {"entries": len(toc)}

    def _op_search(self, needle: str, **kw) -> dict:
        """M4:全文搜索。返回双基准(字符索引真值 + search_for 基准)。

        **不做正则转义**(C5)—— `search_for` 不接受正则,
        `re.escape("1.0.1")` 会让命中数从 8 掉到 0。

        取消机制(C6):逐页检查取消令牌,
        换关键词/换文件/Esc 时立即中止。
        """
        import time as _t
        from .textindex import TextIndex, build_page_index, baseline_search_for

        d = self._require_doc()
        t0 = _t.perf_counter()
        my_token = self._search_token

        # 已建的索引用缓存的,缺的页按需补建(当前页优先由调用方决定顺序)
        ti = self._index
        if ti is None:
            ti = TextIndex()
            self._index = ti
        # Selection and cancelled searches can leave a partial index. Complete
        # its missing pages before treating it as a whole-document search.
        for pno in range(1, len(d) + 1):
            if my_token != self._search_token:
                return {"hits": [], "baseline": 0, "cancelled": True,
                        "elapsed_ms": (_t.perf_counter() - t0) * 1000.0}
            if not ti.has(pno):
                ti.add_page(pno, build_page_index(d[pno - 1], pno))

        hits = ti.find(needle)
        elapsed = (_t.perf_counter() - t0) * 1000.0
        return {
            "hits": hits,
            "baseline": baseline_search_for(d, needle),
            "elapsed_ms": elapsed,
            "pages_indexed": len(ti),
            "pages_total": len(d),
            "total_chars": ti.total_chars,
            "cancelled": False,
        }

    def _op_build_index(self, **kw) -> dict:
        """M4:后台建全书字符索引。返回字符总数与耗时。"""
        import time as _t
        from .textindex import TextIndex, build_page_index
        d = self._require_doc()
        t0 = _t.perf_counter()
        ti = TextIndex()
        for pno in range(1, len(d) + 1):
            ti.add_page(pno, build_page_index(d[pno - 1], pno))
        self._index = ti
        return {"pages": len(ti), "total_chars": ti.total_chars,
                "elapsed_ms": (_t.perf_counter() - t0) * 1000.0}

    def _op_add_annot(self, page: int, start: int, end: int,
                      color=(1.0, 1.0, 0.0), opacity: float = 1.0, ranges=None, **kw) -> dict:
        """M5:按归一化字符下标区间加高亮。**全程持有 page 引用**(K7)。"""
        from .textindex import TextIndex, build_page_index
        d = self._require_doc()
        if not (1 <= page <= len(d)):
            raise IndexError(f"页码越界: {page}")
        # K7:page 引用必须在整个 annot 操作期间存活
        pg = d[page - 1]
        # 取该页索引(没有就现建)
        ti = self._index
        if ti is None or not ti.has(page):
            if ti is None:
                ti = TextIndex()
                self._index = ti
            ti.add_page(page, build_page_index(pg, page))
        rects = (ti.char_range_rects(page, start, end) if ranges is None
                 else ti.char_ranges_rects(page, ranges))
        if not rects:
            return {"added": 0, "quads_count": 0, "rects": []}
        # K7 关键:用 pg 的 quads,并在 pg 存活期间完成
        quads = [fitz.Rect(r).quad for r in rects]
        annot = pg.add_highlight_annot(quads)
        annot.set_colors(stroke=tuple(color))
        annot.set_opacity(float(opacity))
        annot.update()
        n = 0
        for _a in pg.annots():          # pg 仍存活,不会解绑
            n += 1
        return {"added": 1, "quads_count": len(quads), "rects": rects,
                "page_annots": n}

    def _op_del_annot(self, page: int, annot_index: int = 0, **kw) -> dict:
        """M5:删除某页第 annot_index 个高亮。**全程持有 page 引用**(K7)。"""
        d = self._require_doc()
        if not (1 <= page <= len(d)):
            raise IndexError(f"页码越界: {page}")
        pg = d[page - 1]                # K7:保持引用
        anns = list(pg.annots())
        if not (0 <= annot_index < len(anns)):
            return {"deleted": 0, "remaining": len(anns)}
        pg.delete_annot(anns[annot_index])
        # 重新枚举,确认剩余数
        remaining = 0
        for _a in pg.annots():
            remaining += 1
        return {"deleted": 1, "remaining": remaining}

    def _op_get_annots(self, page: int, **kw) -> list:
        """M5:列出某页注记。**持有 page 引用**(K7),否则 annot 会解绑。"""
        d = self._require_doc()
        if not (1 <= page <= len(d)):
            return []
        pg = d[page - 1]
        out = []
        for a in pg.annots():
            try:
                out.append({"type": a.type[1] if isinstance(a.type, tuple) else str(a.type),
                            "rect": [round(v, 2) for v in a.rect]})
            except Exception:
                continue
        return out

    def _op_select_text(self, page: int, x0: float, y0: float,
                        x1: float, y1: float, mode: str = "text", **kw) -> dict:
        """M5:以鼠标两端定位字符,预览和批注共用同一连续文字区间。"""
        from .textindex import TextIndex, build_page_index, selection_range, rectangle_selection
        d = self._require_doc()
        if not (1 <= page <= len(d)):
            raise IndexError(f"页码越界: {page}")
        pg = d[page - 1]
        ti = self._index
        if ti is None or not ti.has(page):
            if ti is None:
                ti = TextIndex()
                self._index = ti
            ti.add_page(page, build_page_index(pg, page))
        idx = ti.pages[page]
        if mode == "rectangle":
            ranges, copy_text = rectangle_selection(idx, (x0, y0), (x1, y1))
        elif mode == "text":
            a, b = selection_range(idx, (x0, y0), (x1, y1))
            ranges = [[a, b]] if 0 <= a < b else []
            copy_text = idx.selection_text(a, b)
        else:
            raise ValueError(f"未知选字方式: {mode}")
        if not ranges:
            return {"start": -1, "end": -1, "text": ""}
        return {"start": min(a for a, b in ranges), "end": max(b for a, b in ranges),
                "ranges": ranges, "text": "".join(idx.norm_text[a:b] for a, b in ranges),
                "copy_text": copy_text, "rects": ti.char_ranges_rects(page, ranges)}

    def _op_save(self) -> dict:
        """保存 —— K1 三步式闭环(saveIncr → close → reopen)。

        这是**唯一**允许的保存路径。同一 Document 上第二次 saveIncr 会静默
        写坏文件(第二次只写 0–192 B,重开变成 0 页 0 条),且不抛异常。

        返回保存增量字节数,供回执 M2-5 判据使用。
        """
        import os as _os
        d = self._require_doc()
        path = self._path
        before = _os.path.getsize(path)
        d.saveIncr()
        after = _os.path.getsize(path)
        d.close()
        # K1 第二步:立即重开(绝不能在同一 Document 上再存一次)
        self._doc = fitz.open(path)
        self._index = None          # 重开后页面对象全换,索引必须重建
        # 第三步:代数自增 → 所有在途结果作废,UI 侧据此丢弃旧回调
        self._bump_generation()
        return {
            "path": path,
            "incr_bytes": after - before,
            "size_bytes": after,
            "pages": len(self._doc),
            "toc": len(self._doc.get_toc()),
            "generation": self.generation,
        }

    def _op_shrink(self, path: Optional[str] = None) -> dict:
        raise NotImplementedError("Shrink 在 M6 接入")

    # ---------------- K1 三步式闭环 ----------------

    def save_and_reopen(self) -> None:
        """worker 线程内执行 K1 三步式:saveIncr → close → reopen。

        ⚠️ 必须只在 worker 线程调用。K1(最高危契约):
            同一 Document 上连续两次 saveIncr 会**静默写坏文件**。
            正确写法是每次保存后立即 close 再 reopen。
        """
        d = self._require_doc()
        path = self._path
        d.saveIncr()
        d.close()
        self._doc = fitz.open(path)
        # 文档对象全换了,代数自增 → 在途结果作废
        self._bump_generation()
