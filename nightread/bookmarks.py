"""大纲(书签)读写与编辑模型 —— 服务 M2 的双击原地编辑。

对应 `docs/stages/M2-bookmarks.md`,遵守契约 K1(保存三步式)与 K2(改标题用 set_toc_item)。

设计要点:
    · 本模块是**纯数据模型**,不持有 fitz.Document —— 文档操作走 docworker(K3)。
    · 树结构用扁平列表 + level 字段表示(PyMuPDF 的 toc 本就是这种形态),
      而非真正的树。因为 `set_toc()` 需要的就是扁平列表,来回转换反而是 bug 温床。
    · 编辑操作先在本地模型上做,标脏;`Ctrl+S` 才写回文档。

层级规则(M2 要求):
    合法层级 = 当前层级 + 1,不能跳级(禁止 1→3)。
    夹紧函数 clamp_level() 在 set_toc 之前统一收口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

MAX_TITLE_LEN = 200          # 超长截断阈值(M2 实现要点)


@dataclass
class Bookmark:
    """一条书签。level 从 1 起;page 从 1 起(PyMuPDF 的 toc 口径)。"""
    level: int
    title: str
    page: int

    def copy(self) -> "Bookmark":
        return Bookmark(self.level, self.title, self.page)


def from_toc(toc: list) -> list[Bookmark]:
    """PyMuPDF get_toc(simple=True) 的 [(level, title, page), ...] → [Bookmark]。

    ⚠️ 实测 get_toc(simple=False) 返回的是 **list 不是 dict**,
    故这里统一只吃 simple=True 的三元组形态。
    """
    out: list[Bookmark] = []
    for e in toc:
        if isinstance(e, (list, tuple)) and len(e) >= 3:
            out.append(Bookmark(int(e[0]), str(e[1]), int(e[2])))
    return out


def to_toc(marks: list[Bookmark]) -> list:
    """[Bookmark] → PyMuPDF set_toc() 接受的三元组列表。"""
    return [[b.level, b.title, b.page] for b in marks]


def validate_title(title: str) -> tuple[bool, str, str]:
    """校验标题。返回 (是否接受, 处理后的标题, 拒绝原因)。

    规则(M2 要求):
        · 空标题(或纯空白)→ 拒绝,由调用方回滚
        · 超长 → 截断到 MAX_TITLE_LEN 并提示
    """
    t = title.strip()
    if not t:
        return False, "", "标题不能为空"
    if len(t) > MAX_TITLE_LEN:
        return True, t[:MAX_TITLE_LEN], f"标题超长,已截断到 {MAX_TITLE_LEN} 字"
    return True, t, ""


def clamp_level(marks: list[Bookmark]) -> list[Bookmark]:
    """夹紧层级,禁止跳级(1→3)。

    规则:第 i 条的 level 最大为「前一条的 level + 1」;首条最大为 1。
    这是 set_toc 的硬约束 —— 违反会导致大纲结构错乱甚至抛异常。
    """
    out: list[Bookmark] = []
    prev = 0
    for b in marks:
        lvl = max(1, min(b.level, prev + 1 if prev else 1))
        out.append(Bookmark(lvl, b.title, b.page))
        prev = lvl
    return out


class BookmarkModel:
    """可编辑的大纲模型。UI 持有本对象,不持有文档。"""

    def __init__(self) -> None:
        self.marks: list[Bookmark] = []
        self.dirty = False

    # ---------------- 载入 ----------------

    def load(self, toc: list) -> None:
        self.marks = from_toc(toc)
        self.dirty = False

    def as_toc(self) -> list:
        return to_toc(clamp_level(self.marks))

    def __len__(self) -> int:
        return len(self.marks)

    def counts(self) -> tuple[int, int]:
        """返回 (level1 条数, level2 条数),供 M2-1 判据。"""
        l1 = sum(1 for b in self.marks if b.level == 1)
        l2 = sum(1 for b in self.marks if b.level == 2)
        return l1, l2

    # ---------------- 编辑 ----------------

    def rename(self, index: int, title: str) -> tuple[bool, str]:
        """改标题。返回 (是否成功, 提示信息)。失败时不动模型。"""
        if not (0 <= index < len(self.marks)):
            return False, "序号越界"
        ok, t, msg = validate_title(title)
        if not ok:
            return False, msg
        if self.marks[index].title != t:
            self.marks[index].title = t
            self.dirty = True
        return True, msg

    def add_sibling(self, index: int) -> int:
        """在 index 之后插入同级条目。返回新条目索引。

        页码取被插入项的页码;若列表为空则插到末尾,页码 1。
        """
        if not self.marks:
            self.marks.append(Bookmark(1, "新书签", 1))
            self.dirty = True
            return 0
        if not (0 <= index < len(self.marks)):
            index = len(self.marks) - 1
        ref = self.marks[index]
        pos = index + 1
        self.marks.insert(pos, Bookmark(ref.level, "新书签", ref.page))
        self.dirty = True
        return pos

    def add_child(self, index: int) -> int:
        """在 index 之下插入子级条目(层级 +1,但不许跳级)。"""
        if not (0 <= index < len(self.marks)):
            return -1
        ref = self.marks[index]
        # 夹紧:子级最多比同级下一条更深的位置;此处简单处理为"父级+1"
        child_level = min(ref.level + 1, 2) if ref.level == 1 else ref.level
        pos = index + 1
        self.marks.insert(pos, Bookmark(child_level, "新子书签", ref.page))
        self.dirty = True
        return pos

    def delete(self, index: int) -> bool:
        """删除条目。若删的是父级,其后续更深层级条目一并提升到该层级。"""
        if not (0 <= index < len(self.marks)):
            return False
        lvl = self.marks[index].level
        del self.marks[index]
        # 把紧随其后的更深条目提升一级,避免产生孤儿
        i = index
        while i < len(self.marks) and self.marks[i].level > lvl:
            self.marks[i].level = max(1, self.marks[i].level - 1)
            i += 1
        self.dirty = True
        return True

    def move_up(self, index: int) -> int:
        """上移。返回新索引(未动则返回原索引)。"""
        if index <= 0 or index >= len(self.marks):
            return index
        self.marks[index - 1], self.marks[index] = \
            self.marks[index], self.marks[index - 1]
        self.dirty = True
        return index - 1

    def move_down(self, index: int) -> int:
        """下移。返回新索引。"""
        if index < 0 or index >= len(self.marks) - 1:
            return index
        self.marks[index + 1], self.marks[index] = \
            self.marks[index], self.marks[index + 1]
        self.dirty = True
        return index + 1

    def mark_clean(self) -> None:
        self.dirty = False
