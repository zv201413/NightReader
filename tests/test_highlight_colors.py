"""保存的高亮颜色在日间/夜读中一致,原始像素和文件不受显示变换影响。"""
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path

import fitz
import numpy as np

from nightread import darkmode
from nightread.annotate import COLORS
from nightread.docworker import DocWorker, Task, OPEN, RENDER


class TestHighlightColors(unittest.TestCase):
    def request(self, worker, kind, **kwargs):
        ready = threading.Event()
        results = []
        def done(result):
            results.append(result)
            ready.set()
        worker.submit(Task(kind, kwargs, callback=done))
        self.assertTrue(ready.wait(10), "worker timed out")
        self.assertTrue(results[0].ok, results[0].tb)
        return results[0].value

    def test_all_colors_opacities_and_rotated_pages_keep_day_pixels(self):
        with tempfile.TemporaryDirectory(prefix="nr_colors_") as tmp:
            for rotation in (0, 90):
                for opacity in (1, .4):
                    path = Path(tmp) / f"{rotation}-{opacity}.pdf"
                    with fitz.open() as doc:
                        page = doc.new_page(width=240, height=240)
                        for i, color in enumerate(COLORS.values()):
                            y = 30 + i * 30
                            page.insert_text((30, y), f"Color {i}", fontsize=14)
                            annot = page.add_highlight_annot(fitz.Rect(28, y-15, 100, y+4))
                            annot.set_colors(stroke=color)
                            annot.set_opacity(opacity)
                            annot.update()
                        page.set_rotation(rotation)
                        doc.save(path)
                    before = hashlib.sha256(path.read_bytes()).hexdigest()
                    worker = DocWorker()
                    worker._post_to_ui = lambda fn: fn()
                    worker.start()
                    try:
                        self.request(worker, OPEN, path=str(path))
                        result = self.request(worker, RENDER, page_no=0, zoom=1.4,
                                              highlight_masks=True)
                        base, masks = result["pixmap"], result["highlight_masks"]
                        self.assertEqual(len(masks), 6)
                        original = bytes(base.samples)
                        day = np.frombuffer(original, dtype=np.uint8).reshape(base.height, base.width, 3)
                        coverage = np.zeros(day.shape[:2], dtype=bool)
                        for mask in masks:
                            x, y, w, h = (mask[k] for k in ("x", "y", "width", "height"))
                            alpha = np.frombuffer(mask["alpha"], dtype=np.uint8).reshape(h, w)
                            core = alpha == mask["peak"]
                            self.assertGreater(core.sum(), 30)
                            coverage[y:y+h, x:x+w] |= core
                        # 实际覆盖彩色背景,不能把空掩码/只覆盖白纸当成通过。
                        colorful = day.max(axis=2) - day.min(axis=2) > 50
                        self.assertGreater(np.count_nonzero(coverage & colorful), 500)
                        for mode in ("off", "invert", "soft"):
                            shown = darkmode.render_page(base, mode, masks)
                            data = np.frombuffer(shown.samples, dtype=np.uint8).reshape(day.shape)
                            np.testing.assert_array_equal(data[coverage], day[coverage])
                            if mode != "off":
                                self.assertFalse(np.array_equal(data[0, 0], day[0, 0]))
                            self.assertEqual(base.samples, original)
                    finally:
                        worker.stop()
                    self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
