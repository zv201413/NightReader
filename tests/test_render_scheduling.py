"""Fast navigation must replace queued work before it reaches MuPDF."""
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import patch

from nightread.window import MainWindow


def scheduler():
    jobs = []
    view = SimpleNamespace(page_no=10, zoom=1, view_mode='image',
                           _scroll_direction=1, _pending=set())
    view._visible_window = lambda: set(range(view.page_no - 3, view.page_no + 4))
    view._visible_pages = lambda: {view.page_no}
    view.page_zoom = lambda p: view.zoom
    obj = SimpleNamespace(cont_view=view, text_presentation='reading',
                          worker=SimpleNamespace(generation=1, submit=jobs.append),
                          _render_queue={}, _render_active=False,
                          _render_dispatch_id=0, _render_closed=False,
                          _on_rendered=lambda result: None)
    obj._request_render = MethodType(MainWindow._request_render, obj)
    obj._dispatch_render = MethodType(MainWindow._dispatch_render, obj)
    return obj, view, jobs


def enqueue_window(obj, view):
    for page in sorted(view._visible_window()):
        view._pending.add(page)
        obj._request_render(page, view.zoom)


class TestRenderScheduling(unittest.TestCase):
    def test_fast_scroll_keeps_one_worker_job_and_new_current_page_goes_first(self):
        obj, view, jobs = scheduler()
        with patch('nightread.window.GLib.idle_add', return_value=123):
            enqueue_window(obj, view)
            obj._dispatch_render()
            self.assertEqual([t.kwargs['page_no'] for t in jobs], [10])
            for page in range(11, 51):
                view.page_no = page
                enqueue_window(obj, view)
            self.assertEqual(len(jobs), 1)
            self.assertLessEqual(len(obj._render_queue), 7)
            jobs[0].callback(SimpleNamespace(ok=True))
            self.assertEqual([t.kwargs['page_no'] for t in jobs], [10, 50])
            jobs[1].callback(SimpleNamespace(ok=True))
            # forward prefetch wins the tie
            self.assertEqual(jobs[2].kwargs['page_no'], 51)

    def test_zoom_and_text_mode_replace_pending_requests_and_close_stops_dispatch(self):
        obj, view, jobs = scheduler()
        with patch('nightread.window.GLib.idle_add', return_value=123):
            enqueue_window(obj, view)
            obj._dispatch_render()
            view.zoom = 2
            view.view_mode = 'text'
            obj.text_presentation = 'proof'
            enqueue_window(obj, view)
            jobs[0].callback(SimpleNamespace(ok=True))
            self.assertEqual(jobs[1].kind, 'RenderText')
            self.assertEqual(jobs[1].kwargs,
                             {'page_no': 10, 'zoom': 2, 'presentation': 'proof'})
            obj._render_closed = True
            jobs[1].callback(SimpleNamespace(ok=True))
            self.assertEqual(len(jobs), 2)

    def test_old_document_pending_pages_are_not_rendered_after_reopen(self):
        obj, view, jobs = scheduler()
        with patch('nightread.window.GLib.idle_add', return_value=123):
            enqueue_window(obj, view)
            obj.worker.generation = 2
            obj._dispatch_render()
            self.assertEqual(jobs, [])
            self.assertEqual(obj._render_queue, {})
            self.assertEqual(view._pending, set())


if __name__ == '__main__':
    unittest.main()