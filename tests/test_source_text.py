from pathlib import Path
import sys
import unittest
import pymupdf as fitz

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from source_text import ordered_text,normalize_evidence


class SourceTextTests(unittest.TestCase):
    def test_both_columns_remain_contiguous_beneath_a_spanning_paragraph(self):
        doc=fitz.open();p=doc.new_page()
        p.insert_textbox(fitz.Rect(190,35,550,85),'A spanning abstract continuation before the body.',fontsize=11)
        p.insert_textbox(fitz.Rect(40,120,275,250),'Left paragraph first line.\nLeft paragraph second line.\nLeft paragraph final line.',fontsize=11)
        p.insert_textbox(fitz.Rect(315,120,550,250),'Right paragraph first line.\nRight paragraph second line.',fontsize=11)
        text=normalize_evidence(ordered_text(p));doc.close()
        self.assertIn('Left paragraph first line. Left paragraph second line. Left paragraph final line.',text)
        self.assertLess(text.index('spanning'),text.index('Left paragraph'))
        self.assertLess(text.index('Left paragraph final'),text.index('Right paragraph'))

    def test_only_printing_ligatures_and_whitespace_are_normalized(self):
        self.assertEqual(normalize_evidence('low-ﬁdelity\nﬂow'), 'low-fidelity flow')
        self.assertNotEqual(normalize_evidence('20°'),normalize_evidence('−20°'))
        self.assertNotEqual(normalize_evidence('x²'),normalize_evidence('x2'))
