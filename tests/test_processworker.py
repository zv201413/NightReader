"""The process boundary preserves edits, pixels, cancellation and lifecycle."""
import os
import shutil
import tempfile
import threading
import time
import unittest

import fitz

from nightread.docworker import (Task, OPEN, RENDER, GET_TOC, SET_TOC_ITEM, SAVE,
                                ADD_ANNOT, GET_ANNOTS, SEARCH, MEMORY_STATS)
from nightread.processworker import ProcessDocWorker


def request(worker, kind, **kwargs):
    done, result = threading.Event(), []
    worker.submit(Task(kind, kwargs, lambda r: (result.append(r), done.set())))
    assert done.wait(15), kind
    assert result[0].ok, result[0].tb
    return result[0]


def make_pdf(path, pages=1):
    with fitz.open() as doc:
        for _ in range(pages):
            p = doc.new_page()
            p.insert_text((40, 50), 'Searchable original words')
        doc.set_toc([[1, 'Original bookmark', 1]])
        doc.save(path)


class TestProcessDocWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='nightreader-processworker-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.worker = ProcessDocWorker()
        self.worker._post_to_ui = lambda fn: fn()
        self.worker.start()

        def stop():
            self.worker.stop(timeout=10)
            self.assertFalse(self.worker._thread.is_alive())
            self.assertTrue(self.worker._process is None
                            or self.worker._process.poll() is not None)
            self.assertIsNone(self.worker._pixels_fd)

        self.addCleanup(stop)

    def test_process_roundtrip_pixels_unsaved_edits_and_repeated_save(self):
        path = os.path.join(self.tmp, 'sample.pdf')
        make_pdf(path)
        request(self.worker, OPEN, path=str(path))
        with fitz.open(path) as doc:
            expected = doc[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).samples
            original_text = doc[0].get_text()
        result = request(self.worker, RENDER, page_no=0, zoom=1.5).value
        self.assertEqual(result.samples, expected)
        # The transport buffer is released, while the delivered pixmap stays valid.
        self.assertEqual(os.fstat(self.worker._pixels_fd).st_size, 0)
        request(self.worker, RENDER, page_no=0, zoom=.75)
        self.assertEqual(result.samples, expected)
        self.assertNotEqual(self.worker.pid, os.getpid())
        self.assertIsNone(self.worker._doc)
        for title in ('First edit', 'Second edit'):
            before = self.worker.generation
            request(self.worker, SET_TOC_ITEM, index=0, title=title)
            self.assertEqual(request(self.worker, GET_TOC).value[0][1], title)
            request(self.worker, SAVE)
            self.assertGreater(self.worker.generation, before)
            with fitz.open(path) as doc:
                self.assertEqual(doc.get_toc()[0][1], title)
                self.assertEqual(doc[0].get_text(), original_text)
        request(self.worker, SEARCH, needle='original')
        request(self.worker, ADD_ANNOT, page=1, start=0, end=5)
        self.assertEqual(len(request(self.worker, GET_ANNOTS, page=1).value), 1)
        rendered = request(self.worker, RENDER, page_no=0, zoom=1.5,
                           highlight_masks=True).value
        self.assertTrue(rendered['highlight_masks'])
        self.assertNotEqual(rendered['pixmap'].samples, expected)
        with fitz.open(path) as doc:
            # annotation is still an unsaved edit
            self.assertEqual(list(doc[0].annots()), [])
        stats = request(self.worker, MEMORY_STATS).value
        self.assertEqual(stats['pid'], self.worker.pid)
        self.assertGreater(stats['process']['rss_swap_bytes'], 0)

    def test_child_failure_returns_errors_without_automatic_restart(self):
        path = os.path.join(self.tmp, 'sample.pdf')
        make_pdf(path)
        request(self.worker, OPEN, path=str(path))
        self.worker._process.terminate()
        self.worker._process.wait(timeout=5)
        old_pid = self.worker.pid
        for kind in (RENDER, SAVE):
            done, results = threading.Event(), []
            self.worker.submit(Task(kind,
                                    {'page_no': 0, 'zoom': 1} if kind == RENDER else {},
                                    lambda r: (results.append(r), done.set())))
            self.assertTrue(done.wait(5))
            self.assertFalse(results[0].ok)
            self.assertEqual(self.worker.pid, old_pid)

    def test_cancel_search_reaches_child_while_rpc_is_waiting(self):
        path = os.path.join(self.tmp, 'many.pdf')
        make_pdf(path, pages=1200)
        request(self.worker, OPEN, path=str(path))
        done, results = threading.Event(), []
        self.worker.submit(Task(SEARCH, {'needle': 'original'},
                                lambda r: (results.append(r), done.set())))
        time.sleep(.04)
        self.worker.cancel_search()
        self.assertTrue(done.wait(10))
        self.assertTrue(results[0].ok, results[0].tb)
        self.assertTrue(results[0].value['cancelled'])
        self.assertEqual(request(self.worker, GET_TOC).value[0][1], 'Original bookmark')


if __name__ == '__main__':
    unittest.main()