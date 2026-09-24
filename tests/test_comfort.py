import os
import unittest
from unittest.mock import patch

import fitz
import numpy as np
import test_text_layer_recovery as fixtures
import test_text_presentations as controls
from nightread import comfort,config,darkmode


class TestComfortPixels(unittest.TestCase):
    def base(self):
        pix=fitz.Pixmap(fitz.csRGB,(0,0,80,60),False)
        pix.clear_with(255)
        a=np.frombuffer(pix.samples_mv,np.uint8).reshape(60,80,3)
        a[10:50,30]=0
        return pix

    def test_neutral_is_identical_and_weight_expands_strokes_without_editing_source(self):
        base=self.base();original=base.samples
        for mode in darkmode.MODES:
            self.assertEqual(darkmode.render_page(base,mode,comfort_params=(0,100,0)).samples,
                             darkmode.render_page(base,mode).samples)
        bold=darkmode.render_page(base,'off',comfort_params=(100,100,0))
        a=np.frombuffer(base.samples,np.uint8).reshape(60,80,3)
        b=np.frombuffer(bold.samples,np.uint8).reshape(60,80,3)
        self.assertEqual(np.count_nonzero(a[:,:,0]==0),40)
        self.assertGreater(np.count_nonzero(b[:,:,0]==0),100)
        self.assertEqual(base.samples,original)
        self.assertEqual((bold.width,bold.height,bold.n),(80,60,3))

    def test_tones_adjust_contrast_and_highlight_color_is_preserved(self):
        base=self.base()
        gentle=darkmode.render_page(base,'invert',comfort_params=comfort.PRESETS['gentle'][1])
        values=np.frombuffer(gentle.samples,np.uint8)
        self.assertGreater(values.min(),0)
        self.assertLess(values.max(),255)
        a=np.frombuffer(base.samples_mv,np.uint8).reshape(60,80,3)
        a[20:30,40:60]=(255,255,0)
        mask={'x':40,'y':20,'width':20,'height':10,'alpha':bytes([255])*200,'peak':255}
        for mode in darkmode.MODES:
            shown=darkmode.render_page(base,mode,[mask],comfort_params=(70,120,-15))
            pixels=np.frombuffer(shown.samples,np.uint8).reshape(60,80,3)
            np.testing.assert_array_equal(pixels[20:30,40:60],a[20:30,40:60])

    def test_invalid_custom_values_are_bounded(self):
        cfg=config.validated({'comfort_preset':'bad','comfort_weight':float('nan'),
                              'comfort_contrast':999,'comfort_brightness':-99})
        self.assertEqual((cfg['comfort_preset'],cfg['comfort_weight'],cfg['comfort_contrast'],cfg['comfort_brightness']),
                         ('standard',0,140,-30))


@unittest.skipUnless(os.environ.get('DISPLAY'),'requires GTK display')
class TestComfortPanel(unittest.TestCase):
    setUp=fixtures.TestTextRecovery.setUp
    scan=fixtures.TestTextRecovery.scan
    pump=controls.TestPresentationControl.pump
    wait=controls.TestPresentationControl.wait

    def test_preview_save_persist_and_proof_mode_is_unaffected(self):
        from nightread.window import MainWindow
        from nightread.settings import SettingsWindow
        for key,value in (('CONFIG_DIR',self.tmp.name),('CONFIG_PATH',self.tmp.name+'/config.json')):
            p=patch.object(config,key,value);p.start();self.addCleanup(p.stop)
        config.save(dict(config.DEFAULTS,window_width=980,window_height=700))
        path=self.scan('scan.pdf')
        reader=MainWindow(path=str(path));self.addCleanup(reader.destroy);reader.show_all()
        self.wait(lambda:reader._path and 0 in reader.view._rendered and not reader.view._pending)
        reader.cycle_view_mode()
        self.wait(lambda:0 in reader.view._rendered and not reader.view._pending)
        get_pixels=lambda:reader.view._rendered[0].get_pixels()
        standard=get_pixels()
        base=reader.view._pixmaps[0].samples
        panel=SettingsWindow();self.addCleanup(panel.destroy);panel.show_all()
        initial_preview=panel.comfort_preview.get_pixbuf().get_pixels()
        panel.comfort_preset.set_active_id('clear')
        self.assertNotEqual(panel.comfort_preview.get_pixbuf().get_pixels(),initial_preview)
        panel.save_button.clicked()
        self.wait(lambda:reader.view.comfort_params==(30,110,0))
        self.assertNotEqual(get_pixels(),standard)
        self.assertEqual(reader.view._pixmaps[0].samples,base)
        reader.text_presentation_combo.set_active_id('proof')
        self.wait(lambda:0 in reader.view._rendered and not reader.view._pending)
        proof=get_pixels()
        panel.comfort_weight.set_value(55)
        panel.comfort_contrast.set_value(125)
        panel.comfort_brightness.set_value(3)
        self.assertEqual(panel.comfort_preset.get_active_id(),'custom')
        panel.save_button.clicked()
        self.wait(lambda:reader.view.comfort_params==(55,125,3))
        self.assertEqual(get_pixels(),proof)
        again=SettingsWindow();self.addCleanup(again.destroy)
        self.assertEqual(again.comfort_preset.get_active_id(),'custom')
        self.assertEqual(tuple(s.get_value_as_int() for s in again._comfort_spins),(55,125,3))
        self.assertFalse(reader._dirty)
