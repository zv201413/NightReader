"""Eviction must preserve complete searches, exact geometry and copied spacing."""
import unittest

import fitz

from nightread.textindex import TextIndex, build_page_index, PackedBoxes
from nightread.memstats import index_stats


class TestBoundedIndex(unittest.TestCase):
    def test_bounded_geometry_reload_matches_complete_index(self):
        with fitz.open() as doc:
            for n in range(24):
                doc.new_page().insert_text((40, 40), f'page {n:02d}  A B\nC D 1.0.1')
            loads = []
            def load(pno):
                loads.append(pno)
                return build_page_index(doc[pno - 1], pno)
            bounded = TextIndex(max_pages=3, loader=load)
            complete = TextIndex()
            for pno in range(1, 25):
                idx = load(pno)
                bounded.add_page(pno, idx)
                complete.add_page(pno, idx)
            self.assertEqual(len(bounded), 24)
            self.assertEqual(len(bounded.pages), 3)
            self.assertEqual(bounded.total_chars, complete.total_chars)
            self.assertEqual(bounded.find('ABCD'), complete.find('ABCD'))
            self.assertEqual(bounded.find('1.0.1'), complete.find('1.0.1'))
            self.assertEqual(len(bounded.pages), 3)
            idx = bounded.get_page(1)
            a = idx.norm_text.index('ABCD')
            self.assertEqual(idx.selection_text(a, a + 4), 'A B\nC D')
            self.assertEqual(bounded.char_range_rects(1, a, a + 4),
                             complete.char_range_rects(1, a, a + 4))
            loads.clear()
            self.assertEqual(bounded.find('never present'), [])
            self.assertEqual(loads, [])
            self.assertEqual(bounded.find('ABCD', cancelled=lambda: len(loads) >= 2), [])
            self.assertEqual(len(loads), 2)
            self.assertLess(index_stats(bounded)['estimated_bytes'],
                            index_stats(complete)['estimated_bytes'])

    def test_packed_boxes_exactly_match_mupdf_character_coordinates(self):
        with fitz.open() as doc:
            page = doc.new_page()
            page.insert_text((40.1234, 71.4321), 'precise coordinates', fontsize=12.34)
            idx = build_page_index(page, 1)
            expected = [tuple(c['bbox']) for block in page.get_text('rawdict')['blocks']
                        for line in block.get('lines', []) for span in line['spans']
                        for c in span['chars'] if not c['c'].isspace()]
            self.assertIsInstance(idx.boxes, PackedBoxes)
            self.assertEqual(list(idx.boxes), expected)
            self.assertEqual(idx.boxes[::-1], expected[::-1])
            self.assertEqual(idx.boxes.data.itemsize * len(idx.boxes.data),
                             16 * len(idx))


if __name__ == '__main__':
    unittest.main()