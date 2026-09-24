"""Bad ToUnicode mappings and damaged hidden OCR must not become wrong glyphs/formulas."""
import hashlib
from pathlib import Path
import tempfile
import time
import unittest

import fitz
import numpy as np
from gi.repository import GLib
from nightread.docworker import (DocWorker, Task, OPEN, CLOSE, RENDER_TEXT,
                                SELECT_TEXT, SEARCH, _scan_text_problem)
from nightread import darkmode


class TestTextRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='nr_text_recovery_')
        self.addCleanup(self.tmp.cleanup)
        self.worker=DocWorker()
        self.worker.start()
        self.addCleanup(self.worker.stop)

    def task(self, kind, **kw):
        results=[]
        self.worker.submit(Task(kind,kw,callback=results.append))
        context=GLib.MainContext.default()
        deadline=time.monotonic()+8
        while not results and time.monotonic()<deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(.005)
        self.assertTrue(results,kind)
        self.assertTrue(results[0].ok,str(results[0].error))
        return results[0].value

    def test_unicode_repairs_wrong_native_glyphs_without_moving_origins(self):
        source=Path(self.tmp.name)/'wrong-font.pdf'
        expected=Path(self.tmp.name)/'expected.pdf'
        chars='总则 '
        with fitz.open() as doc:
            page=doc.new_page(width=300,height=200)
            page.insert_text((40,90),'ABC',fontsize=30,render_mode=3)
            font=page.get_fonts()[0][0]
            cmap=doc.get_new_xref()
            doc.update_object(cmap,'<<>>')
            mappings='\n'.join(f'<{ord(a):02x}> <{ord(b):04x}>' for a,b in zip('ABC',chars))
            doc.update_stream(cmap,('begincmap\n1 begincodespacerange\n<00> <ff>\nendcodespacerange\n'
                                   '3 beginbfchar\n'+mappings+'\nendbfchar\nendcmap').encode())
            doc.xref_set_key(font,'ToUnicode',f'{cmap} 0 R')
            doc.save(source)
        with fitz.open(source) as doc:
            self.assertEqual(doc[0].get_text().strip(),'总则')
            raw=doc[0].get_text('rawdict')['blocks'][0]['lines'][0]['spans'][0]['chars']
            origins=[c['origin'] for c in raw]
        fallback=fitz.Font('cjk')
        original=fitz.Font('helv')
        with fitz.open() as doc:
            page=doc.new_page(width=300,height=200)
            page.insert_font(fontname='fallback',fontbuffer=fallback.buffer)
            for a,b,origin in zip('AB',chars,origins):
                scale=original.glyph_advance(ord(a))/fallback.glyph_advance(ord(b))
                point=fitz.Point(origin)
                page.insert_text(point,b,fontsize=30,fontname='fallback',
                                 morph=(point,fitz.Matrix(scale,1)))
            doc.save(expected)
            oracle=page.get_pixmap(matrix=fitz.Matrix(2,2))
        digest=hashlib.sha256(source.read_bytes()).digest()
        self.task(OPEN,path=str(source))
        result=self.task(RENDER_TEXT,page_no=0,zoom=2)
        actual=result['pixmap']
        a=np.frombuffer(actual.samples,np.uint8).reshape(actual.height,actual.width,3).min(axis=2)<220
        b=np.frombuffer(oracle.samples,np.uint8).reshape(oracle.height,oracle.width,3).min(axis=2)<220
        self.assertGreater(b.sum(),300)
        # The reference embeds the fallback font into a PDF, whose matrix
        # serialization/hinting shifts some edge pixels. Require the same ink
        # shape and independently pin both glyphs to within one pixel.
        self.assertGreater((a&b).sum()/(a|b).sum(),.90)
        for left,right in zip(origins,origins[1:]):
            x0,x1=int(left[0]*2),int(right[0]*2)
            ay,ax=np.where(a[:,x0:x1])
            by,bx=np.where(b[:,x0:x1])
            self.assertTrue(len(ax) and len(bx))
            for actual_bound,expected_bound in zip((ax.min(),ax.max(),ay.min(),ay.max()),
                                                   (bx.min(),bx.max(),by.min(),by.max())):
                self.assertLessEqual(abs(int(actual_bound)-int(expected_bound)),1)
        # The C glyph maps to whitespace and must not leave a box or letter.
        self.assertFalse(a[:,int(origins[2][0]*2)+1:].any())
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(),digest)
        # OCR proof mode exposes the PDF's actual embedded glyphs, including
        # a bad mapping, instead of silently applying the reading repair.
        proof=self.task(RENDER_TEXT,page_no=0,zoom=2,presentation='proof')['pixmap']
        with fitz.open() as raw:
            page=raw.new_page(width=300,height=200)
            page.insert_text((40,90),'ABC',fontsize=30,color=(0,0,0))
            native=page.get_pixmap(matrix=fitz.Matrix(2,2))
        ink_proof=np.frombuffer(proof.samples,np.uint8).reshape(-1,3).min(axis=1)<220
        ink_native=np.frombuffer(native.samples,np.uint8).reshape(-1,3).min(axis=1)<220
        self.assertGreater((ink_proof&ink_native).sum()/(ink_proof|ink_native).sum(),.90)

    def scan(self, name, broken='math', rotation=0):
        path=Path(self.tmp.name)/name
        with fitz.open() as visible:
            pg=visible.new_page(width=300,height=400)
            pg.insert_text((50,80),'TEXT',fontsize=18)
            pg.insert_text((50,120),'2.72',fontsize=14)
            pg.draw_line((45,125),(90,125),color=(0,0,0))
            pg.insert_text((50,145),'1+0.7',fontsize=14)
            image=pg.get_pixmap(matrix=fitz.Matrix(2,2))
        with fitz.open() as doc:
            pg=doc.new_page(width=300,height=400)
            pg.insert_image(pg.rect,pixmap=image)
            for y in (200,225,250,275):
                pg.insert_text((30,y),'ordinary text',fontsize=12,render_mode=3)
            if broken=='math':
                pg.insert_text((45,115),r'$x_{1}=2.72$',fontsize=12,render_mode=3)
            elif broken=='size':
                pg.insert_text((40,85),'A',fontsize=18,render_mode=3)
                pg.insert_text((62,90),'B',fontsize=45,render_mode=3)
            elif broken=='native':
                pg.insert_text((45,115),r'$x_{1}=2.72$',fontsize=12)
            else:
                pg.insert_text((45,115),'2.72',fontsize=12,render_mode=3)
            pg.set_rotation(rotation)
            doc.save(path)
        return path

    def test_bad_math_and_conflicting_sizes_use_exact_source_pixels(self):
        for broken in ('math','size'):
            for rotation in (0,90):
                with self.subTest(broken=broken,rotation=rotation):
                    path=self.scan(f'{broken}-{rotation}.pdf',broken,rotation)
                    digest=hashlib.sha256(path.read_bytes()).digest()
                    self.task(OPEN,path=str(path))
                    result=self.task(RENDER_TEXT,page_no=0,zoom=1.5)
                    self.assertTrue(result['source_fallback'])
                    with fitz.open(path) as doc:
                        expected=doc[0].get_pixmap(matrix=fitz.Matrix(1.5,1.5))
                    self.assertEqual(result['pixmap'].samples,expected.samples)
                    for mode in ('off','invert','soft'):
                        self.assertEqual(darkmode.render_page(result['pixmap'],mode).samples,
                                         darkmode.render_page(expected,mode).samples)
                    self.assertEqual(hashlib.sha256(path.read_bytes()).digest(),digest)

    def test_normal_hidden_text_and_visible_math_do_not_trigger_fallback(self):
        for kind in ('normal','native'):
            with self.subTest(kind=kind):
                path=self.scan(kind+'.pdf',kind)
                self.task(OPEN,path=str(path))
                result=self.task(RENDER_TEXT,page_no=0,zoom=1)
                self.assertFalse(result.get('source_fallback'))
                with fitz.open(path) as doc:
                    self.assertNotEqual(result['pixmap'].samples,doc[0].get_pixmap().samples)

    def test_fallback_keeps_search_and_decision_is_reset_for_next_document(self):
        path=self.scan('broken.pdf')
        self.task(OPEN,path=str(path))
        self.assertTrue(self.task(RENDER_TEXT,page_no=0,zoom=1)['source_fallback'])
        self.assertEqual(len(self.task(SEARCH,needle='ordinary')['hits']),4)
        next_path=self.scan('next.pdf','normal')
        self.task(OPEN,path=str(next_path))
        self.assertFalse(self.task(RENDER_TEXT,page_no=0,zoom=1).get('source_fallback'))
