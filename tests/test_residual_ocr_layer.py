"""Residual white replacement glyphs must not spoil comfortable scan reading."""
import hashlib
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import fitz
import numpy as np
from nightread import config
from nightread.docworker import OPEN, RENDER_TEXT, SEARCH
import test_text_layer_recovery as fixtures
import test_text_presentations as controls


class TestResidualOCR(unittest.TestCase):
    setUp = fixtures.TestTextRecovery.setUp
    task = fixtures.TestTextRecovery.task

    def make_pdf(self, name='residual.pdf', scan=True, hidden=True, residue=True, leader=False):
        path = Path(self.tmp.name) / name
        with fitz.open() as original:
            pg = original.new_page(width=300, height=400)
            pg.insert_text((65, 110), 'ORIGINAL SCAN', fontsize=16)
            pg.insert_text((65, 150), 'Correct page layout', fontsize=12)
            image = pg.get_pixmap(matrix=fitz.Matrix(2, 2))
        with fitz.open() as doc:
            page = doc.new_page(width=300, height=400)
            if scan:
                page.insert_image(page.rect, pixmap=image)
            if residue:
                page.insert_text((8, 20), '1.0.1\n' + '\n'.join(['········'] * 9),
                                 fontsize=8, color=(1, 1, 1))
            if hidden:
                page.insert_text((30, 45), 'OCR CONTENT\nReflowed at wrong location',
                                 fontsize=10, render_mode=3)
            if leader:
                page.insert_text((30, 230), 'Contents ' + '·' * 45 + ' 3', fontsize=10)
            doc.save(path)
        return path

    def test_reading_uses_original_scan_and_proof_reveals_both_original_text_layers(self):
        path = self.make_pdf()
        digest = hashlib.sha256(path.read_bytes()).digest()
        self.task(OPEN, path=str(path))
        # Open proof first: diagnostics must not silently repair or replace it.
        proof = self.task(RENDER_TEXT, page_no=0, zoom=2, presentation='proof')
        self.assertEqual(proof['text_warning'], '残留乱码文字')
        self.assertFalse(proof.get('source_fallback'))
        with fitz.open(path) as doc:
            original = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2))
            for xref in doc[0].get_contents():
                stream = doc.xref_stream(xref)
                # Independent PDF oracle: strip only the image and reveal the
                # original white text + hidden OCR without changing text geometry.
                doc.update_stream(xref, b'' if b' Do' in stream else
                                  stream.replace(b'1 1 1 rg', b'0 0 0 rg').replace(b'3 Tr', b'0 Tr'))
            expected = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2))
        a = np.frombuffer(proof['pixmap'].samples, np.uint8).reshape(-1, 3).min(axis=1) < 220
        b = np.frombuffer(expected.samples, np.uint8).reshape(-1, 3).min(axis=1) < 220
        self.assertGreater((a & b).sum() / (a | b).sum(), .90)
        reading = self.task(RENDER_TEXT, page_no=0, zoom=2, presentation='reading')
        self.assertEqual(reading['source_fallback'], '残留乱码文字')
        self.assertEqual(reading['pixmap'].samples, original.samples)
        self.assertNotEqual(reading['pixmap'].samples, proof['pixmap'].samples)
        self.assertEqual(len(self.task(SEARCH, needle='CONTENT')['hits']), 1)
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), digest)

    def test_native_text_and_ordinary_toc_leaders_are_not_replaced(self):
        for name, options in (('no-scan', {'scan': False}), ('no-ocr', {'hidden': False}),
                              ('normal', {'residue': False}),
                              ('toc', {'residue': False, 'leader': True})):
            with self.subTest(case=name):
                path = self.make_pdf(name + '.pdf', **options)
                self.task(OPEN, path=str(path))
                result = self.task(RENDER_TEXT, page_no=0, zoom=1)
                self.assertFalse(result.get('source_fallback'))


@unittest.skipUnless(os.environ.get('DISPLAY'), 'requires GTK display')
class TestResidualOCRUI(unittest.TestCase):
    setUp = fixtures.TestTextRecovery.setUp
    make_pdf = TestResidualOCR.make_pdf
    pump = controls.TestPresentationControl.pump
    wait = controls.TestPresentationControl.wait

    def test_proof_explains_diagnostic_without_fallback_and_clears_on_new_file(self):
        from nightread.window import MainWindow
        for key, value in (('CONFIG_DIR', self.tmp.name),
                           ('CONFIG_PATH', self.tmp.name + '/config.json')):
            context = patch.object(config, key, value)
            context.start()
            self.addCleanup(context.stop)
        config.save(dict(config.DEFAULTS, window_width=980, window_height=700))
        path = self.make_pdf()
        w = MainWindow(path=str(path))
        self.addCleanup(w.destroy)
        w.show_all()
        self.wait(lambda: w._path and w.view._rendered and not w.view._pending)
        w.cycle_view_mode()
        self.wait(lambda: w._text_fallbacks.get(0) and not w.view._pending)
        self.assertEqual(w.text_btn.get_label(), '文字层:原页')
        with patch.object(w, '_set_status', wraps=w._set_status) as status:
            w.text_presentation_combo.set_active_id('proof')
            self.wait(lambda: w._text_warnings.get(0) and not w.view._pending)
            self.assertIn('残留乱码文字（按原样显示）', status.call_args.args[0])
        self.assertEqual(w.text_btn.get_label(), '文字层:校对')
        self.assertFalse(w._text_fallbacks.get(0))
        normal = self.make_pdf('normal.pdf', residue=False)
        w.open_file(str(normal))
        self.wait(lambda: w._path == str(normal) and w.view._rendered and not w.view._pending)
        self.assertFalse(w._text_warnings.get(0))
        self.assertFalse(w._dirty)
