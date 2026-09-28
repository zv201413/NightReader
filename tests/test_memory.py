"""Memory maintenance must release real caches and respect worker ownership."""
import gc
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import weakref
from unittest.mock import patch

import fitz

from nightread.docworker import (DocWorker, Task, OPEN, CLOSE, RENDER, MEMORY_STATS,
                                 SEARCH, STORE_LIMIT_BYTES)
from nightread.memstats import MemoryReporter, allocator_stats, process_stats, store_size


def request(worker, kind, **kwargs):
    ready, results = threading.Event(), []
    def done(result):
        results.append(result)
        ready.set()
    worker.submit(Task(kind, kwargs, done))
    assert ready.wait(15)
    result = results.pop()
    assert result.ok, result.tb
    return result.value


class TestWorkerMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='nightreader-memory-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_real_store_shrink_and_close_on_worker(self):
        path = self.path('scan.pdf')
        # 24 distinct image pages: a page set this size costs more than the
        # configured store limit on the pinned MuPDF, so the shrink below has
        # something real to reclaim. Re-rendering the same 8 pages instead
        # saturates the store just under the limit and proves nothing.
        with fitz.open() as doc:
            for color in range(24):
                pix = fitz.Pixmap(fitz.csRGB, (0, 0, 1800, 1800), False)
                pix.clear_with(200 + color)
                doc.new_page().insert_image(fitz.Rect(0, 0, 595, 842), pixmap=pix)
            doc.save(path, deflate=True)
        del pix
        # Observe the thread of every cache operation, not just the Result callback.
        threads = []
        original = fitz.TOOLS.store_shrink
        def shrink(percent):
            threads.append(threading.current_thread().name)
            return original(percent)
        patcher = patch.object(fitz.TOOLS, 'store_shrink', shrink)
        patcher.start()
        self.addCleanup(patcher.stop)
        worker = DocWorker()
        worker._post_to_ui = lambda fn: fn()
        worker.start()
        try:
            request(worker, OPEN, path=str(path))
            for pno in range(8):
                rendered = request(worker, RENDER, page_no=pno, zoom=3)
                self.assertEqual(rendered.width, 1785)
                del rendered
            stats = request(worker, MEMORY_STATS)
            self.assertIsNone(stats['store_error'])
            self.assertGreater(stats['store_shrinks'], 0)
            self.assertLessEqual(stats['store_bytes'], STORE_LIMIT_BYTES)
            self.assertGreater(stats['store_bytes'], 0)
            # Text extraction can populate the same store. Exercise a large-store
            # text-task completion deterministically, without relying on a specific
            # embedded font decoder in the locally available PDF fixture.
            before = []
            def cache_heavy_search(**kwargs):
                # Render every distinct page: the store only grows when new
                # pages are touched, so this exceeds the configured target
                # regardless of how much a single page costs on this build.
                for pno in range(24):
                    worker._op_render(pno, 1)
                before.append(store_size()[0])
                return {'hits': []}
            with patch.object(worker, '_op_search', side_effect=cache_heavy_search):
                request(worker, SEARCH, needle='test')
            # The store must really exceed the configured target, otherwise the
            # shrink that follows proves nothing. Compare against the worker's
            # own limit, which is a fixed policy value, rather than a hardcoded
            # byte count that varies between MuPDF builds.
            self.assertGreater(before[0], worker._store_limit)
            self.assertLessEqual(request(worker, MEMORY_STATS)['store_bytes'],
                                 STORE_LIMIT_BYTES)
            request(worker, CLOSE)
            self.assertEqual(request(worker, MEMORY_STATS)['store_bytes'], 0)
            self.assertEqual(set(threads), {'nightread-docworker'})
        finally:
            worker.stop()

    def test_worker_does_not_pin_last_render(self):
        path = self.path('one.pdf')
        with fitz.open() as doc:
            doc.new_page()
            doc.save(path)
        worker = DocWorker()
        worker._post_to_ui = lambda fn: fn()
        worker.start()
        try:
            request(worker, OPEN, path=str(path))
            pix = request(worker, RENDER, page_no=0, zoom=1)
            ref = weakref.ref(pix)
            del pix
            # Leave queue.get blocked; a second task would hide retention by
            # overwriting the loop's previous value/result on the old code.
            deadline = time.monotonic() + 1
            while ref() is not None and time.monotonic() < deadline:
                gc.collect()
                time.sleep(.01)
            self.assertIsNone(ref())
        finally:
            worker.stop()

    def test_store_failure_and_opt_out_do_not_break_rendering(self):
        worker = DocWorker()
        with patch('nightread.memstats.store_size',
                   side_effect=RuntimeError('unavailable')):
            worker._maintain_store()
            self.assertEqual(worker._store_error, 'RuntimeError')
        os.environ['NIGHTREAD_STORE_SHRINK'] = 'off'
        self.addCleanup(os.environ.pop, 'NIGHTREAD_STORE_SHRINK', None)
        worker = DocWorker()
        with patch('fitz.TOOLS.store_shrink') as shrink:
            worker._maintain_store(clear=True)
            shrink.assert_not_called()

    def test_store_size_stub_fallback_detects_resources(self):
        fitz.TOOLS.store_shrink(100)
        self.assertEqual(store_size()[0], 0)
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((40, 40), 'cache size')
            pix = page.get_pixmap()
            with patch('fitz.TOOLS.store_size', return_value=None):
                size, source = store_size()
            self.assertGreater(size, 0)
            self.assertEqual(source, 'debug_store')
            self.assertGreater(pix.width, 0)
        fitz.TOOLS.store_shrink(100)

    def test_diagnostics_opt_in_and_process_units(self):
        os.environ.pop('NIGHTREAD_MEMSTATS', None)
        self.assertIsNone(MemoryReporter.start_if_enabled(None, None))
        stats = process_stats()
        self.assertEqual(stats['rss_swap_bytes'],
                         stats['VmRSS_bytes'] + stats['VmSwap_bytes'])
        summary = allocator_stats()
        self.assertTrue(summary is None or summary['arena_system_bytes'] > 0)
        json.dumps(stats)


if __name__ == '__main__':
    unittest.main()