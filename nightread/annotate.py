"""高亮批注 —— M5。

契约 K7:`docs/00-shared.md` §5 —— annot 生命周期。

K7 的坑:annot 是**绑定到 page 对象**的。如果这样写:

    a = list(doc[page_no].annots())[0]      # ❌ doc[...] 是临时对象
    a.type                                   # FzErrorArgument code=4:
                                             #   annotation not bound to any page

`doc[page_no]` 产生的 Page 是临时的,表达式结束后即被回收,annot 随之解绑。
正确做法是先把 page 存进局部变量,让它在整个 annot 操作期间存活:

    pg = doc[page_no]                        # ✅ 引用在作用域内存活
    for a in pg.annots():
        ...

本模块只提供**纯函数与数据模型**;真正碰文档的操作在 docworker.py 的
AddAnnot/DelAnnot/GetAnnots 里(那里已按 K7 持有 page 引用)。

选字粒度:复用 M4 的 `textindex`(字符级),**不用 `get_text("words")`** ——
实测一个 word 可能是 38 字的整句(S2),拿它当选中单位会一次选一大段。
"""

from __future__ import annotations

from dataclasses import dataclass

# 首版单色黄(M5 要求);六色选择移到 M6 选做
DEFAULT_COLOR = (1.0, 1.0, 0.0)
HIGHLIGHT_TYPES = ("Highlight",)

# M6 选做:六色
COLORS = {
    "黄": (1.0, 1.0, 0.0),
    "绿": (0.0, 1.0, 0.0),
    "蓝": (0.0, 0.6, 1.0),
    "粉": (1.0, 0.5, 0.8),
    "橙": (1.0, 0.6, 0.0),
    "紫": (0.7, 0.4, 1.0),
}


@dataclass
class AnnotInfo:
    """一条注记的描述(不持有 fitz 对象 —— 出了 worker 线程就只是数据)。"""
    page: int
    index: int
    type: str
    rect: list

    @property
    def is_highlight(self) -> bool:
        return self.type in HIGHLIGHT_TYPES


def normalize_selection(a: int, b: int) -> tuple[int, int]:
    """把选区的两个端点排成 (start, end),并保证 start < end。"""
    lo, hi = (a, b) if a <= b else (b, a)
    return lo, hi


def selection_is_empty(a, b) -> bool:
    return a is None or b is None or a == b
