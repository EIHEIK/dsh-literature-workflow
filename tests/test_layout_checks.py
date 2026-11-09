from pathlib import Path
import sys
import tempfile
import unittest
import pymupdf as fitz

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from layout_checks import check_composition
from pdf_ops import compose


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.test-output');self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def make_pdf(self,name,pages,fontsize=11,invisible=False):
        path=self.root/name;doc=fitz.open()
        for _ in range(pages):
            page=doc.new_page();page.insert_text((40,60),'中文正文排版测试',fontname='china-s',fontsize=fontsize)
            if invisible:page.insert_text((40,90),'隐藏的原文不算可见正文',fontname='china-s',fontsize=2,render_mode=3)
        doc.save(path);doc.close();return path
    def test_supplement_stays_with_source_and_continuations(self):
        source=self.make_pdf('source.pdf',2);mono=self.make_pdf('mono.pdf',3);folder=self.root/'result';folder.mkdir()
        layout=compose(source,mono,[1,1,2],[{'sourcePage':1,'title':'图一','text':'轴标签：速度与时间'}],folder)
        self.assertEqual([r['sourcePage'] for r in layout['rows']],[1,1,1,2])
        self.assertEqual([r['kind'] for r in layout['rows']],['translation','translation','supplement','translation'])
        self.assertEqual(check_composition(folder,layout,[1,2])['hardFailures'],[])
    def test_tiny_visible_chinese_blocks_delivery(self):
        source=self.make_pdf('source.pdf',1);mono=self.make_pdf('mono.pdf',1,fontsize=3.2);folder=self.root/'result';folder.mkdir()
        layout=compose(source,mono,[1],[],folder)
        self.assertTrue(any(i['kind']=='generated_chinese_font' for i in check_composition(folder,layout,[1])['hardFailures']))
    def test_large_glyph_failure_inventory_keeps_counts_in_bounded_report(self):
        from layout_checks import summarize_failures
        import json
        failures=[{'kind':'generated_chinese_font','document':'cn','page':1,'size':3.2 if n%2 else 8.5,'bbox':[n,0,n+1,1]} for n in range(20000)]
        grouped=summarize_failures(failures)
        self.assertEqual(len(grouped),1);self.assertEqual(grouped[0]['occurrences'],20000)
        self.assertEqual(sum(grouped[0]['sizeCounts'].values()),20000);self.assertEqual(grouped[0]['size'],3.2)
        self.assertLess(len(json.dumps(grouped)),2000)
    def test_legacy_supplement_page_is_grouped_with_its_source(self):
        source=self.make_pdf('source.pdf',2);mono=self.make_pdf('mono.pdf',3);folder=self.root/'result';folder.mkdir()
        layout=compose(source,mono,[1,2,1],[],folder)
        self.assertEqual([r['sourcePage'] for r in layout['rows']],[1,1,2])
        self.assertFalse(any(i['kind']=='page_mapping' for i in check_composition(folder,layout,[1,2])['hardFailures']))
    def test_fully_off_page_text_is_not_visible_but_partial_clip_is_rejected(self):
        source=self.make_pdf('source.pdf',1)
        for name,x,expected in [('outside',-100,False),('partial',-3,True)]:
            path=self.root/(name+'.pdf');doc=fitz.open();p=doc.new_page()
            p.insert_text((40,60),'清晰中文正文',fontname='china-s',fontsize=11)
            p.insert_text((x,100),'中',fontname='china-s',fontsize=3.2 if not expected else 11)
            doc.save(path);doc.close();folder=self.root/name;folder.mkdir()
            layout=compose(source,path,[1],[],folder);qa=check_composition(folder,layout,[1])
            self.assertEqual(any(i['kind']=='visible_text_clipped' for i in qa['hardFailures']),expected)
            if not expected:self.assertFalse(qa['hardFailures'])
    def test_invisible_source_layer_does_not_misclassify_visible_text(self):
        source=self.make_pdf('source.pdf',1);mono=self.make_pdf('mono.pdf',1,invisible=True);folder=self.root/'result';folder.mkdir()
        layout=compose(source,mono,[1],[],folder);qa=check_composition(folder,layout,[1])
        self.assertEqual(qa['hardFailures'],[])
        self.assertGreater(qa['pages'][0]['invisibleCharacters'],0)
        self.assertGreaterEqual(qa['pages'][0]['minVisibleChinesePt'],10)


if __name__=='__main__':unittest.main()
