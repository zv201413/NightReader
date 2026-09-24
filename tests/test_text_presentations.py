"""Reading improvements and faithful OCR inspection are separate display modes."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import fitz
import numpy as np
import test_text_layer_recovery as fixtures
from nightread import config
from nightread.docworker import OPEN,RENDER_TEXT


class TestProofRendering(unittest.TestCase):
    setUp=fixtures.TestTextRecovery.setUp
    task=fixtures.TestTextRecovery.task
    scan=fixtures.TestTextRecovery.scan

    def test_proof_exposes_original_text_after_reading_fallback(self):
        for kind in ('math','size'):
            with self.subTest(kind=kind):
                path=self.scan(kind+'.pdf',kind)
                self.task(OPEN,path=str(path))
                self.assertTrue(self.task(RENDER_TEXT,page_no=0,zoom=1.5)['source_fallback'])
                proof=self.task(RENDER_TEXT,page_no=0,zoom=1.5,presentation='proof')
                self.assertFalse(proof.get('source_fallback'))
                with fitz.open(path) as source:
                    original=source[0].get_pixmap(matrix=fitz.Matrix(1.5,1.5))
                    for xref in source[0].get_contents():
                        stream=source.xref_stream(xref)
                        # Independent PDF oracle: remove the scan's image
                        # invocation and reveal unchanged hidden text operators.
                        source.update_stream(xref,b'' if b' Do' in stream else
                                             stream.replace(b'3 Tr',b'0 Tr').replace(b'BT',b'BT\n0 0 0 rg'))
                    expected=source[0].get_pixmap(matrix=fitz.Matrix(1.5,1.5))
                # MuPDF's RGB text device and PDF gray-color conversion differ
                # at antialiased edges, but must paint the same original glyphs.
                a=np.frombuffer(proof['pixmap'].samples,np.uint8).reshape(-1,3).min(axis=1)<220
                b=np.frombuffer(expected.samples,np.uint8).reshape(-1,3).min(axis=1)<220
                self.assertGreater((a&b).sum()/(a|b).sum(),.90)
                for mask_a,mask_b in ((a.reshape(expected.height,expected.width),
                                       b.reshape(expected.height,expected.width)),):
                    ya,xa=np.where(mask_a);yb,xb=np.where(mask_b)
                    for got,want in zip((xa.min(),xa.max(),ya.min(),ya.max()),
                                        (xb.min(),xb.max(),yb.min(),yb.max())):
                        self.assertLessEqual(abs(int(got)-int(want)),1)
                self.assertNotEqual(proof['pixmap'].samples,original.samples)


@unittest.skipUnless(os.environ.get('DISPLAY'),'requires GTK display')
class TestPresentationControl(unittest.TestCase):
    setUp=fixtures.TestTextRecovery.setUp
    scan=fixtures.TestTextRecovery.scan

    def pump(self,seconds=.04):
        from gi.repository import GLib
        loop=GLib.MainLoop()
        GLib.timeout_add(int(seconds*1000),loop.quit)
        loop.run()

    def wait(self,condition):
        import time
        deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            self.pump()
            if condition():return
        self.fail('presentation did not render')

    def test_switch_keeps_anchor_rejects_stale_results_and_windows_can_compare(self):
        from nightread.window import MainWindow
        for key,value in (('CONFIG_DIR',self.tmp.name),('CONFIG_PATH',self.tmp.name+'/config.json')):
            ctx=patch.object(config,key,value);ctx.start();self.addCleanup(ctx.stop)
        config.save(dict(config.DEFAULTS,window_width=980,window_height=700))
        path=self.scan('bad.pdf')
        w=MainWindow(path=str(path))
        self.addCleanup(w.destroy)
        w.show_all()
        self.wait(lambda:w._path and 0 in w.view._rendered and not w.view._pending)
        w.cycle_view_mode()
        self.wait(lambda:w._text_fallbacks.get(0) and 0 in w.view._rendered and not w.view._pending)
        self.assertEqual(w.text_btn.get_label(),'文字层:原页')
        before=(w.view.page_no,w.view.zoom,w.view._scroll_anchor())
        reading=w.view._pixmaps[0].samples
        w.text_presentation_combo.set_active_id('proof')
        self.wait(lambda:0 in w.view._rendered and not w.view._pending)
        self.assertEqual(w.text_btn.get_label(),'文字层:校对')
        self.assertNotEqual(w.view._pixmaps[0].samples,reading)
        self.assertEqual((w.view.page_no,w.view.zoom,w.view._scroll_anchor()),before)
        self.assertEqual(config.load()['text_presentation'],'proof')
        stale=SimpleNamespace(ok=True,doc_generation=w.worker.generation,
                              meta={'view_mode':'text','text_presentation':'reading','page_no':0,'zoom':w.view.zoom},
                              value={'pixmap':object(),'source_fallback':'stale'},elapsed_ms=0)
        with patch.object(w,'_deliver_pixmap') as deliver:
            w._on_rendered(stale)
            deliver.assert_not_called()
        other=MainWindow()
        self.addCleanup(other.destroy)
        other.show_all()
        self.assertEqual(other.text_presentation,'proof')
        w.text_presentation_combo.set_active_id('reading')
        self.wait(lambda:w._text_fallbacks.get(0) and 0 in w.view._rendered and not w.view._pending)
        self.pump(.2)
        self.assertEqual(w.view._pixmaps[0].samples,reading)
        self.assertEqual(other.text_presentation,'proof')
        self.assertFalse(w._dirty)
