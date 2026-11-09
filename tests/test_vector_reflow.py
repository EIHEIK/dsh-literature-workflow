from pathlib import Path
import sys
import tempfile
import unittest
import pymupdf as fitz

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from core import digest
from vector_reflow import reflow
from pdf_ops import compose
from layout_checks import check_composition
from pdf_regions import visible_images


class VectorReflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.test-output');self.root=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()

    def test_opaque_single_pixel_image_stays_protected(self):
        doc=fitz.open();p=doc.new_page()
        pix=fitz.Pixmap(fitz.csRGB,fitz.IRect(0,0,1,1),False);pix.clear_with(80)
        p.insert_image(fitz.Rect(20,20,120,120),pixmap=pix)
        self.assertEqual(len(visible_images(p)),1);doc.close()

    def test_native_formula_and_figure_survive_readable_continuation(self):
        source=self.root/'input.pdf';doc=fitz.open();p=doc.new_page()
        p.insert_text((40,60),'小字号中文正文',fontname='china-s',fontsize=8)
        p.insert_text((110,60),'F = m a; 20 m/s',fontsize=8)
        p.draw_rect(fitz.Rect(300,180,360,230),color=(1,0,0),fill=(1,0,0))
        pix=fitz.Pixmap(fitz.csRGB,fitz.IRect(0,0,10,10),False);pix.clear_with(160)
        p.insert_image(fitz.Rect(400,180,440,230),pixmap=pix);doc.save(source);doc.close();before=digest(source)
        output=self.root/'out.pdf';mapping,evidence=reflow(source,[1],output)
        self.assertTrue(evidence);self.assertEqual(digest(source),before);self.assertEqual(set(mapping),{1})
        with fitz.open(output) as out,fitz.open(source) as original:
            text=''.join(p.get_text() for p in out)
            self.assertIn('小字号中文正文',text);self.assertIn('20 m/s',text);self.assertIn('F = m a',text)
            clip=fitz.Rect(280,160,460,250)
            self.assertEqual(original[0].get_pixmap(clip=clip).samples,out[0].get_pixmap(clip=clip).samples)
            self.assertEqual(len(out[0].get_image_info()),1)
            self.assertFalse(visible_images(out[1]),'Off-clip source figures must not masquerade as paragraph images')
        folder=self.root/'composed';folder.mkdir();layout=compose(source,output,mapping,[],folder)
        self.assertFalse(check_composition(folder,layout,[1])['hardFailures'])

    def test_uncertain_clipped_text_is_kept_and_still_fails(self):
        source=self.root/'clipped.pdf';doc=fitz.open();p=doc.new_page()
        p.insert_text((-3,50),'中文',fontname='china-s',fontsize=8);doc.save(source);doc.close()
        output=self.root/'out.pdf';mapping,evidence=reflow(source,[1],output)
        self.assertEqual(evidence,[])
        folder=self.root/'composed';folder.mkdir();layout=compose(source,output,mapping,[],folder)
        self.assertTrue(check_composition(folder,layout,[1])['hardFailures'])

    def test_two_columns_keep_left_continuation_before_right_column(self):
        source=self.root/'columns.pdf';doc=fitz.open();p=doc.new_page()
        for x,y,text in [(40,60,'左栏第一段'),(40,130,'左栏第二段'),(330,60,'右栏第一段'),(330,130,'右栏第二段')]:
            p.insert_text((x,y),text,fontname='china-s',fontsize=8)
        doc.save(source);doc.close();output=self.root/'out.pdf'
        reflow(source,[1],output)
        with fitz.open(output) as result:text=''.join(page.get_text() for page in result)
        self.assertLess(text.index('左栏第二段'),text.index('右栏第一段'))

    def test_short_spanning_metadata_stays_before_both_body_columns(self):
        source=self.root/'bands.pdf';doc=fitz.open();p=doc.new_page()
        top='摘要接续内容位于页面中间并横跨栏间距需要先于正文读取'
        copyright='版权声明位于两栏正文上方不得插入左右栏的跨页句中间'
        for x,y,text in [(505,30,'作者页眉'),(190,55,top),(190,85,copyright),
                          (40,140,'左栏正文第一段'),(40,220,'左栏正文第二段'),
                          (330,140,'右栏接续左栏末句')]:
            p.insert_text((x,y),text,fontname='china-s',fontsize=8)
        doc.save(source);doc.close();output=self.root/'out.pdf'
        reflow(source,[1],output)
        with fitz.open(output) as result:text=''.join(page.get_text() for page in result)
        self.assertLess(text.index('作者页眉'),text.index(top))
        self.assertLess(text.index(top),text.index('左栏正文第一段'))
        self.assertLess(text.index(copyright),text.index('左栏正文第一段'))
        self.assertLess(text.index('左栏正文第二段'),text.index('右栏接续左栏末句'))
