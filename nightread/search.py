"""搜索 UI 与结果模型 —— M4。

本模块只放**数据模型**与少量纯函数,GTK 控件留在 window.py
(与 bookmarks.py 的分工一致:模型与视图分开)。

双基准的意义(Z1):
    本工具报两个命中数 —— 字符索引真值与 `search_for` 基准。
    **字符索引值才是正确答案**:`search_for` 遇到跨空白/跨行就断,
    样板里「承载力」少报 10 处、「地基」少报 14 处、「桥涵」少报 2 处。
    报基准是为了让差额可解释,不是为了让两者相等。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SearchHit:
    """一条命中。page 从 1 起;rects 为该命中在页面上的合并矩形(PDF 坐标)。"""
    page: int
    start: int                 # 归一化文本下标(与 textindex 口径一致)
    end: int
    rects: list = field(default_factory=list)

    def context(self, full_text: str = "", width: int = 16) -> str:
        """给结果列表用的上下文片段。"""
        if not full_text:
            return f"第 {self.page} 页"
        a = max(0, self.start - width)
        b = min(len(full_text), self.end + width)
        frag = full_text[a:b].replace("\n", " ")
        lead = "…" if a > 0 else ""
        tail = "…" if b < len(full_text) else ""
        return f"第 {self.page} 页  {lead}{frag}{tail}"


@dataclass
class SearchResult:
    """一次搜索的完整结果(供回执记录双基准)。"""
    needle: str
    hits: list = field(default_factory=list)
    baseline: int = 0          # search_for 的命中数
    elapsed_ms: float = 0.0
    pages_indexed: int = 0
    pages_total: int = 0

    @property
    def count(self) -> int:
        return len(self.hits)

    def page_list(self) -> list:
        seen, out = set(), []
        for h in self.hits:
            if h.page not in seen:
                seen.add(h.page)
                out.append(h.page)
        return out

    def summary(self) -> str:
        return (f"「{self.needle}」{self.count} 处(字符索引)"
                f" / {self.baseline} 处(search_for)"
                f" · {self.elapsed_ms:.0f} ms")


def dedupe_overlapping(hits: list) -> list:
    """同一页同一位置的重叠命中只留一条(防止相邻关键词重复计)。"""
    out = []
    for h in hits:
        dup = False
        for o in out:
            if o.page == h.page and not (h.end <= o.start or h.start >= o.end):
                dup = True
                break
        if not dup:
            out.append(h)
    return out
