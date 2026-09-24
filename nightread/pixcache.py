"""按字节预算的 LRU 缓存 —— K8。

对应 `docs/stages/M1-skeleton.md` 契约 K8。

为什么按字节而不是按条数:
    单页成本随缩放急剧变化 —— 1× 时 1.4 MB,8× 时 91.7 MB,相差 65 倍。
    若按"最多缓存 N 页"来限,8× 下 N=50 会吃掉 4.5 GB。
    按字节预算则天然自适应:高缩放时自动少缓存几页。

契约要点:
    BUDGET_BYTES = 256 * 1024 * 1024
    key  = (page_no, round(zoom, 3))
    cost = pix.height * pix.stride      # 按真实 stride 计,不按 w*h*3 估
    cost > BUDGET_BYTES 时不缓存,直接渲染返回

关于 cost 用 stride 而非 w*h*3:
    stride 是每行实际字节数,可能含行末填充。用 w*h*3 会低估,
    导致实际内存超出预算。实测 n=3 时 stride == w*3,但 n=4(含 alpha)
    以及某些宽度下 stride > w*n,所以按 stride 计才准。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Optional

import fitz

BUDGET_BYTES = 256 * 1024 * 1024        # 256 MB,契约规定值


def pix_cost(pix: fitz.Pixmap) -> int:
    """一个 Pixmap 的真实内存占用(字节)。

    用 height * stride 而非 width * height * n:
    stride 已含行末填充,是 MuPDF 实际分配的每行字节数。
    """
    return pix.height * pix.stride


class PixCache:
    """LRU 字节预算缓存。线程安全(UI 线程与 worker 都可能访问)。

    统计量:
        hits / misses       命中率
        bytes               当前占用
        evictions           因超预算被淘汰的次数
    """

    def __init__(self, budget: int = BUDGET_BYTES):
        self.budget = budget
        self._d: "OrderedDict[tuple, fitz.Pixmap]" = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.peak_bytes = 0
        self.oversize_skips = 0          # 单页超预算、不缓存的次数

    # ---------------- 查询 ----------------

    @staticmethod
    def make_key(page_no: int, zoom: float) -> tuple:
        """缓存键。zoom 四舍五入到 3 位,避免浮点误差造成同一缩放出多个键。"""
        return (page_no, round(zoom, 3))

    def get(self, page_no: int, zoom: float) -> Optional[fitz.Pixmap]:
        k = self.make_key(page_no, zoom)
        with self._lock:
            if k in self._d:
                self._d.move_to_end(k)      # LRU:命中即移到队尾
                self.hits += 1
                return self._d[k]
            self.misses += 1
            return None

    def put(self, page_no: int, zoom: float, pix: fitz.Pixmap) -> bool:
        """存入。单页超预算则拒收(返回 False),由调用方直接返回该页图。"""
        cost = pix_cost(pix)
        k = self.make_key(page_no, zoom)
        with self._lock:
            if cost > self.budget:
                # K8: cost > BUDGET_BYTES 时不缓存,直接渲染返回
                self.oversize_skips += 1
                return False
            if k in self._d:
                self._bytes -= pix_cost(self._d[k])
                del self._d[k]
            self._d[k] = pix
            self._bytes += cost
            # 淘汰直到回到预算内
            while self._bytes > self.budget and self._d:
                _, old = self._d.popitem(last=False)
                self._bytes -= pix_cost(old)
                self.evictions += 1
            if self._bytes > self.peak_bytes:
                self.peak_bytes = self._bytes
            return True

    # ---------------- 维护 ----------------

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self._bytes = 0

    def invalidate_page(self, page_no: int) -> int:
        """丢弃某页所有缩放的缓存(该页被修改后调用)。返回丢弃条数。"""
        with self._lock:
            keys = [k for k in self._d if k[0] == page_no]
            for k in keys:
                self._bytes -= pix_cost(self._d.pop(k))
            return len(keys)

    def drop_other_generation(self, keep_page: Optional[int] = None) -> int:
        """代数变化(换文档)后调用:清掉不相关的缓存。"""
        return self.clear() or 0

    # ---------------- 统计 ----------------

    @property
    def bytes(self) -> int:
        with self._lock:
            return self._bytes

    def __len__(self) -> int:
        with self._lock:
            return len(self._d)

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._d),
                "bytes": self._bytes,
                "budget": self.budget,
                "peak_bytes": self.peak_bytes,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": (self.hits / total) if total else 0.0,
                "evictions": self.evictions,
                "oversize_skips": self.oversize_skips,
            }

    def stats_line(self) -> str:
        """一行状态栏文本(M1-5 要求缓存可观测)。"""
        s = self.stats()
        return (f"缓存 {s['entries']} 页 / {s['bytes']/1048576:.1f} MB"
                f"(预算 {s['budget']//1048576} MB)· 命中 {s['hit_rate']*100:.0f}%")
