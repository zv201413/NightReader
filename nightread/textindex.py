"""字符级文本索引 —— M4 全文搜索与 M5 选字的共同底座。

契约 K6:`docs/00-shared.md` §5。

为什么不用 `page.search_for()` 或 `get_text("words")`:
    · `search_for()` 是 MuPDF 的原生匹配,**遇到跨空白/跨行的词就断**。
      实测「承载力」在样板里 search_for 得 317 处,而文本真值是 327 处 ——
      差的 10 处正是被换行/多空格切断的。
    · `get_text("words")` 的"词"粒度不可靠:实测一个 word 可能是 38 字的整句(S2),
      拿它做选字单位会一次选中一大段。
    · 因此自建**字符级**索引:逐字符拿 `c["c"]` 与 `c["bbox"]`,
      归一化(剔空白)后拼成 `norm_text`,并保留 `norm_idx -> bbox` 映射。
      匹配在 `norm_text` 上做,命中后反查 bbox 合并成矩形。

Z1 的解法正在于此:样板第 7 页标题实际是 `1  总  则`(字间多空格),
`search_for("总则")` 只有 2 处(目录页),而字符索引能命中正文标题 —— 共 3 处。

**不做正则转义**(C5):`search_for` 不接受正则,`re.escape("1.0.1")` 会让
命中数从 8 掉到 0。本模块同理,关键词一律按字面量处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import fitz


@dataclass
class CharBox:
    """归一化文本里的一个字符及其在页面上的位置。"""
    ch: str
    bbox: tuple            # (x0, y0, x1, y1) PDF 坐标
    page: int              # 页号,从 1 起(PyMuPDF 口径)


@dataclass
class PageIndex:
    """单页的字符索引。"""
    page: int
    norm_text: str = ""
    boxes: list = field(default_factory=list)      # 与 norm_text 逐位对应
    lines: list = field(default_factory=list)      # (start, end, bbox),字符命中用
    source_text: str = ""                         # 复制用,保留空白与行分隔
    source_offsets: list = field(default_factory=list)  # norm_idx → source_text 下标

    def __len__(self) -> int:
        return len(self.norm_text)

    def selection_text(self, start: int, end: int) -> str:
        """按同一字符选区复制原文,搜索仍使用剔除空白的 norm_text。"""
        if not 0 <= start < end <= len(self.norm_text):
            return ""
        if not self.source_offsets:
            return self.norm_text[start:end]
        return self.source_text[self.source_offsets[start]:self.source_offsets[end - 1] + 1]


def build_page_index(page: fitz.Pixmap_or_Page, page_no: int) -> PageIndex:
    """按 K6 建单页字符索引。page_no 从 1 起。

    遍历 rawdict 的 blocks → lines → spans → chars,剔除所有空白字符。
    """
    idx = PageIndex(page=page_no)
    raw = page.get_text("rawdict")
    chars: list[str] = []
    boxes: list[tuple] = []
    source: list[str] = []
    source_pos = 0
    for blk in raw.get("blocks", []):
        for line in blk.get("lines", []):
            line_start = len(chars)
            for span in line.get("spans", []):
                for c in span.get("chars", []):
                    ch = c.get("c", "")
                    if not ch:
                        continue
                    offset = source_pos
                    source.append(ch)
                    source_pos += len(ch)
                    if ch.isspace():
                        continue
                    bb = c.get("bbox")
                    if not bb:
                        continue
                    chars.append(ch)
                    boxes.append(tuple(bb))
                    idx.source_offsets.append(offset)
            if len(chars) > line_start:
                line_boxes = boxes[line_start:]
                bounds = (min(b[0] for b in line_boxes), min(b[1] for b in line_boxes),
                          max(b[2] for b in line_boxes), max(b[3] for b in line_boxes))
                idx.lines.append((line_start, len(chars), bounds))
            source.append("\n")
            source_pos += 1
    idx.norm_text = "".join(chars)
    idx.source_text = "".join(source)
    idx.boxes = boxes
    return idx


def selection_range(idx: PageIndex, start: tuple, end: tuple) -> tuple[int, int]:
    """把拖选两端映射成字符间的插入位置,返回连续的半开区间。

    先按行定位,再按字的中点决定落在字前还是字后。跨行拖动会包含
    起始行的后半段、完整中间行和结束行的前半段;反向拖动同理。
    不用两点之间的矩形搜字,否则竖着拖时会漏掉行两侧的文字。
    """
    if not idx.lines:
        return -1, -1

    def boundary(point):
        x, y = point
        def distance(line):
            x0, y0, x1, y1 = line[2]
            return (max(y0 - y, 0, y - y1), max(x0 - x, 0, x - x1))
        a, b, _ = min(idx.lines, key=distance)
        for i in range(a, b):
            box = idx.boxes[i]
            if x < (box[0] + box[2]) / 2:
                return i
        return b

    return tuple(sorted((boundary(start), boundary(end))))


def rectangle_selection(idx: PageIndex, start: tuple, end: tuple) -> tuple[list, str]:
    """区域选字:字符 bbox 与选框有面积交集即命中,逐行保留独立区间。

    不要求字被完整包住,也不能用首末下标连成一段:列外的文字必须排除。
    复制按视觉行从上到下、行内从左到右排列,可跨 PDF 的独立文本块。
    """
    x0, x1 = sorted((start[0], end[0]))
    y0, y1 = sorted((start[1], end[1]))
    if x1 - x0 < .5:
        x0, x1 = (x0 + x1) / 2 - .25, (x0 + x1) / 2 + .25
    if y1 - y0 < .5:
        y0, y1 = (y0 + y1) / 2 - .25, (y0 + y1) / 2 + .25
    chunks = []
    for a, b, _line_bounds in idx.lines:
        hits = [i for i in range(a, b)
                if min(x1, idx.boxes[i][2]) > max(x0, idx.boxes[i][0])
                and min(y1, idx.boxes[i][3]) > max(y0, idx.boxes[i][1])]
        if not hits:
            continue
        ranges = []
        for i in hits:
            if ranges and ranges[-1][1] == i:
                ranges[-1][1] = i + 1
            else:
                ranges.append([i, i + 1])
        boxes = [idx.boxes[i] for i in hits]
        bounds = (min(r[0] for r in boxes), min(r[1] for r in boxes),
                  max(r[2] for r in boxes), max(r[3] for r in boxes))
        chunks.append((bounds, ranges))
    rows = []
    for bounds, ranges in sorted(chunks, key=lambda c: (c[0][1], c[0][0])):
        row = next((row for row in reversed(rows)
                    if min(row["bottom"], bounds[3]) - max(row["top"], bounds[1])
                    >= .5 * min(row["bottom"] - row["top"], bounds[3] - bounds[1])), None)
        if row is None:
            row = {"top": bounds[1], "bottom": bounds[3], "chunks": []}
            rows.append(row)
        row["chunks"].append((bounds, ranges))
    selected, text_lines = [], []
    for row in rows:
        line_ranges = [r for _bounds, ranges in sorted(row["chunks"], key=lambda c: c[0][0])
                       for r in ranges]
        selected.extend(line_ranges)
        text_lines.append(" ".join(idx.selection_text(a, b) for a, b in line_ranges))
    return selected, "\n".join(text_lines)


def merge_boxes(boxes: list, page_height: float = 0.0,
                tol: float = 2.0, x_gap: float = 24.0) -> list:
    """把逐字符 bbox 合并成行级矩形,供高亮使用。

    合并规则:同一行(y 区间重叠)且水平间隙不超过 x_gap 的字符并成一个矩形。

    x_gap 取 24pt 而非默认的 2pt,是实测需要:样板第 7 页正文标题原文是
    `1  总  则`,「总」与「则」之间隔了 16.08pt 的排版空白。用 2pt 容差会得到
    两个碎片矩形,而用户看到的是一个标题 —— 计划书 M4-2 给的参考值
    `[285.6, 95.2, 333.7, 111.2]` 正是一个合并后的整框(宽 48.1pt)。
    24pt 既能跨过这种字间空白,又不会把左右两栏并到一起(栏距远大于 24pt)。
    """
    if not boxes:
        return []
    rects = [fitz.Rect(b) for b in boxes]
    rects.sort(key=lambda r: (round(r.y0, 1), r.x0))
    out: list[fitz.Rect] = []
    for r in rects:
        if out:
            last = out[-1]
            same_line = abs(last.y0 - r.y0) <= tol and abs(last.y1 - r.y1) <= tol
            adjacent = (r.x0 <= last.x1 + x_gap and
                        last.x0 <= r.x1 + x_gap)
            if same_line and adjacent:
                # y 排序会把略高的右侧字符放在前面(如样板「表 C」)。
                # 合并须取左右边界的并集,否则左边的字会从高亮中消失。
                last.x0 = min(last.x0, r.x0)
                last.x1 = max(last.x1, r.x1)
                last.y0 = min(last.y0, r.y0)
                last.y1 = max(last.y1, r.y1)
                continue
        out.append(fitz.Rect(r))
    return [[round(v, 2) for v in (r.x0, r.y0, r.x1, r.y1)] for r in out]


class TextIndex:
    """全书字符索引。逐页缓存,支持增量构建与取消。"""

    def __init__(self) -> None:
        self.pages: dict[int, PageIndex] = {}
        self.total_chars = 0

    def add_page(self, page_no: int, idx: PageIndex) -> None:
        self.pages[page_no] = idx
        self.total_chars = sum(len(p) for p in self.pages.values())

    def __len__(self) -> int:
        return len(self.pages)

    def has(self, page_no: int) -> bool:
        return page_no in self.pages

    # ---------------- 匹配 ----------------

    def find(self, needle: str) -> list[dict]:
        """在已建索引的页里找 needle(字面量,不做正则转义)。

        返回 [{page, start, end, rects}] —— start/end 是**归一化文本**的下标区间,
        M5 的选字也用同一套下标。
        """
        if not needle:
            return []
        out: list[dict] = []
        n = len(needle)
        for pno in sorted(self.pages):
            idx = self.pages[pno]
            text = idx.norm_text
            if not text:
                continue
            pos = text.find(needle)
            while pos != -1:
                boxes = idx.boxes[pos:pos + n]
                out.append({
                    "page": pno,
                    "start": pos,
                    "end": pos + n,
                    "rects": merge_boxes(boxes),
                })
                pos = text.find(needle, pos + 1)
        return out

    def char_range_rects(self, page_no: int, start: int, end: int) -> list:
        """取某页归一化下标区间的合并矩形(M5 选字用)。"""
        idx = self.pages.get(page_no)
        if idx is None:
            return []
        boxes = idx.boxes[start:end]
        return merge_boxes(boxes)

    def char_ranges_rects(self, page_no: int, ranges: list) -> list:
        """区域预览与 PDF 批注共用同一组独立字符区间。"""
        idx = self.pages[page_no]
        rects = []
        for a, b in ranges:
            if not 0 <= a < b <= len(idx):
                raise ValueError("选区字符下标越界")
            rects.extend(merge_boxes(idx.boxes[a:b]))
        return rects

    def page_text(self, page_no: int) -> str:
        idx = self.pages.get(page_no)
        return idx.norm_text if idx else ""


def baseline_search_for(doc, needle: str) -> int:
    """MuPDF 原生匹配的命中数(M4 的对照基准,不是本工具的答案)。

    用于回执里的双基准报告。**不做正则转义**。
    """
    n = 0
    for pg in doc:
        n += len(pg.search_for(needle))
    return n
