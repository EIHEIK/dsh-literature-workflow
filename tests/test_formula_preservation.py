"""Original vector formula preservation, synthetic PDF evidence, zero APIs."""
from pathlib import Path
import sys
import tempfile
import unittest

import pymupdf as fitz

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'runtime'))
from core import digest
from formula_preservation import preserve,embed_original_bitmap


class FormulaPreservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.test-output');self.root=Path(self.tmp.name)
        self.source=self.root/'source.pdf';self.mono=self.root/'mono.pdf';self.output=self.root/'out.pdf'
        self.detections={1:[{'kind':'isolate_formula','confidence':.9,'bbox':[58,156,260,175]},
                            {'kind':'formula_caption','confidence':.9,'bbox':[498,156,520,175]}]}
    def tearDown(self):self.tmp.cleanup()

    def make(self,path,formula,overlap=False):
        doc=fitz.open();page=doc.new_page()
        page.insert_text((60,100),'The following equation defines the gated output.',fontsize=11)
        page.insert_text((60,170),formula,fontsize=12);page.insert_text((500,170),'(2)',fontsize=11)
        page.insert_text((60,230),'The symbols are described in the next paragraph.',fontsize=11)
        page.draw_rect(fitz.Rect(400,300,450,350),color=(1,0,0),fill=(1,0,0))
        if overlap:page.insert_text((60,160),'翻译正文不能被公式区域覆盖删除',fontname='china-s',fontsize=11)
        doc.save(path);doc.close()

    def test_corrupted_formula_is_original_vectors_and_pixels_with_other_data_intact(self):
        self.make(self.source,'y = h*y_NN + (1-h)*y_LF');self.make(self.mono,'y = g*y_NN + (1-g*y_LF')
        frozen=digest(self.source);result=preserve(self.source,self.mono,[1],self.output,self.detections)
        self.assertEqual(result['uncertain'],[]);self.assertEqual(len(result['restored']),1)
        self.assertTrue(result['restored'][0]['pixelsEqual']);self.assertEqual(digest(self.source),frozen)
        with fitz.open(self.output) as out,fitz.open(self.mono) as before:
            text=out[0].get_text();self.assertIn('y = h*y_NN + (1-h)*y_LF',text)
            self.assertNotIn('y = g*y_NN',text)
            clip=fitz.Rect(390,290,460,360)
            self.assertEqual(out[0].get_pixmap(clip=clip).samples,before[0].get_pixmap(clip=clip).samples)

    def test_prose_overlap_and_uncorroborated_detection_are_not_replaced(self):
        self.make(self.source,'y = h*y_NN + (1-h)*y_LF');self.make(self.mono,'y = g*y_NN',overlap=True)
        result=preserve(self.source,self.mono,[1],self.output,self.detections)
        self.assertFalse(result['restored']);self.assertTrue(result['uncertain'])
        with fitz.open(self.output) as out:self.assertIn('翻译正文不能被公式区域覆盖删除',out[0].get_text())
        self.detections[1]=[{'kind':'isolate_formula','confidence':.4,'bbox':[58,156,260,175]}]
        other=self.root/'other.pdf';result=preserve(self.source,self.mono,[1],other,self.detections)
        self.assertEqual(result['uncertain'][0]['reason'],'low_confidence_without_number')

    def test_only_empty_padding_is_trimmed_to_keep_neighboring_translation(self):
        self.make(self.source,'y = h*y_NN + (1-h)*y_LF');self.make(self.mono,'y = g*y_NN')
        doc=fitz.open(self.mono);doc[0].insert_text((60,188),'邻接正文必须完整保留',fontname='china-s',fontsize=11)
        near=self.root/'near.pdf';doc.save(near);doc.close()
        result=preserve(self.source,near,[1],self.output,self.detections)
        self.assertEqual(len(result['restored']),1);self.assertFalse(result['uncertain'])
        with fitz.open(self.output) as doc:self.assertIn('邻接正文必须完整保留',doc[0].get_text())

    def test_bitmap_fallback_embeds_exact_source_pixels_at_600_dpi(self):
        self.make(self.source,'y = h*y_NN + (1-h)*y_LF');self.make(self.mono,'y = g*y_NN')
        with fitz.open(self.source) as source,fitz.open(self.mono) as target:
            rect=fitz.Rect(55,150,525,180)
            proof=embed_original_bitmap(source[0],target[0],rect)
            self.assertTrue(proof['sourcePixelsEmbeddedExactly']);self.assertEqual(proof['dpi'],600)
            image=fitz.Pixmap(target,target[0].get_images()[-1][0])
            expected=source[0].get_pixmap(matrix=fitz.Matrix(600/72,600/72),clip=rect,alpha=False)
            self.assertEqual(image.samples,expected.samples)
