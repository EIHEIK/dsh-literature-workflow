import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
import pymupdf as fitz
from core import digest,read_json
from repair_ops import inventory,validate_response,apply_repairs

ROOT=Path(__file__).resolve().parents[1]


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.test-output');self.root=Path(self.tmp.name);self.mono=self.root/'mono.pdf'
        doc=fitz.open();p=doc.new_page()
        p.insert_text((50,50),'气动力是矢量。速度为 20 m/s。',fontname='china-s',fontsize=3.2)
        p.insert_text((50,150),'F = m a',fontsize=11)
        p.draw_rect(fitz.Rect(320,200,360,240),color=(1,0,0),fill=(1,0,0))
        pix=fitz.Pixmap(fitz.csRGB,fitz.IRect(0,0,10,10),False);pix.clear_with(180)
        p.insert_image(fitz.Rect(400,200,450,250),pixmap=pix)
        p=doc.new_page();p.insert_text((50,50),'第二页保持不变。',fontname='china-s',fontsize=11)
        doc.save(self.mono);doc.close()
        self.blocks=inventory(self.mono,[1,2]);self.block=next(b for b in self.blocks if b['minimumSize']<4)
        self.plan={'candidateId':'fixture','monoSha256':digest(self.mono),'sourcePages':[1,2],
                   'sourceTexts':{'1':'Aerodynamic force is a vector. Speed is 20 m/s.','2':'Second page is unchanged.'},
                   'repairRound':1,'blocks':self.blocks}
        self.responses={p:{'candidateId':'fixture','sourcePage':p,'updates':[],'supplements':[],'unresolved':[]} for p in [1,2]}

    def tearDown(self):self.tmp.cleanup()

    def change(self,text='气动力是矢量。速度为 20 m/s。'):
        return {'blockId':self.block['id'],'originalSha256':self.block['sha256'],
                'sourceQuote':self.plan['sourceTexts']['1'],'replacement':text,'reason':'fixture repair'}

    def test_small_text_reflows_and_source_groups_stay_adjacent(self):
        before=digest(self.mono);output=self.root/'repaired'
        apply_repairs(self.mono,[1,2],self.plan,self.responses,output,[])
        self.assertEqual(digest(self.mono),before)
        self.assertEqual(read_json(output/'mapping.json')['sourcePages'],[1,1,2])
        with fitz.open(output/'mono.pdf') as doc:
            text=''.join(p.get_text() for p in doc)
            self.assertIn('20 m/s',text);self.assertIn('F = m a',text)
            cjk_sizes=[s['size'] for p in doc for s in p.get_texttrace() if any(0x3400<=c[0]<=0x9fff for c in s['chars'])]
            self.assertGreaterEqual(min(cjk_sizes),9.95)
            with fitz.open(self.mono) as src:
                region=fitz.Rect(300,180,470,270)
                self.assertEqual(src[0].get_pixmap(clip=region).samples,doc[0].get_pixmap(clip=region).samples)

    def test_model_cannot_change_numeric_values_or_invent_source_quote(self):
        response=copy.deepcopy(self.responses[1]);response['updates']=[self.change('速度为 21 m/s。')]
        with self.assertRaisesRegex(ValueError,'numerical'):validate_response(self.plan,1,response)
        response['updates'][0]=self.change();response['updates'][0]['sourceQuote']='Fabricated quote'
        with self.assertRaisesRegex(ValueError,'grounded'):validate_response(self.plan,1,response)

    def test_wider_quote_does_not_require_copying_numbers_from_untouched_neighbor(self):
        plan=copy.deepcopy(self.plan);plan['sourceTexts']['1']+=' Published in 2025.'
        update=self.change();update['sourceQuote']=plan['sourceTexts']['1']
        response=copy.deepcopy(self.responses[1]);response['updates']=[update]
        validate_response(plan,1,response)
        update['replacement']='速度为 21 m/s。'
        with self.assertRaisesRegex(ValueError,'numerical'):validate_response(plan,1,response)

    def test_chinese_prefix_does_not_hide_a_numeric_change(self):
        from repair_ops import numbers
        self.assertEqual(numbers('表3. 图11'),numbers('Table 3. Figure 11'))
        self.assertNotEqual(numbers('表3'),numbers('表4'))

    def test_greek_symbol_indices_are_not_confused_with_scientific_values(self):
        from repair_ops import numbers
        self.assertEqual(numbers('k1 and k2 use 0.01'),numbers('λ1 和 λ2 使用 0.01'))
        self.assertNotEqual(numbers('k1 = 0.01'),numbers('λ1 = 0.02'))

    def test_repair_images_are_bound_to_real_source_bytes(self):
        from repair_ops import request_body
        path=self.root/'source.png';pix=fitz.Pixmap(fitz.csRGB,fitz.IRect(0,0,8,8),False);pix.clear_with(255);pix.save(path)
        plan={**self.plan,'issues':[],'sourceImages':{'1':[{'name':path.name,'sha256':digest(path)}]}}
        body=request_body(plan,1,self.root);self.assertEqual(body['messages'][1]['content'][2]['type'],'image_url')
        self.assertEqual(__import__('json').loads(body['messages'][1]['content'][0]['text'])['sourceImages'],plan['sourceImages']['1'])
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'image changed'):request_body(plan,1,self.root)

    def test_visual_transcription_requires_exact_source_image_and_remains_candidate(self):
        from repair_ops import quote_candidates
        plan=copy.deepcopy(self.plan);plan['sourceSha256']='b'*64
        plan['sourceImages']={'1':[{'name':'source-0001.png','sha256':'a'*64}]}
        change=self.change('速度为 −20 m/s。');change['sourceQuote']='Speed is −20 m/s.'
        change['sourceEvidence']={'kind':'source_page_image','imageSha256':'a'*64}
        response=copy.deepcopy(self.responses[1]);response['updates']=[change]
        validate_response(plan,1,response)
        records=quote_candidates(plan,{1:response});self.assertEqual(records[0]['quote'],change['sourceQuote'])
        change['replacement']='速度为 20 m/s。'
        with self.assertRaisesRegex(ValueError,'numerical'):validate_response(plan,1,response)
        change['replacement']='速度为 −20 m/s。';change['sourceEvidence']['imageSha256']='c'*64
        with self.assertRaisesRegex(ValueError,'image'):validate_response(plan,1,response)

    def test_reference_superscript_in_prose_does_not_protect_an_entire_paragraph(self):
        path=self.root/'reference.pdf';doc=fitz.open();page=doc.new_page()
        page.insert_text((50,80),'中文论述',fontname='china-s',fontsize=11)
        page.insert_text((94,76),'12',fontsize=7)
        page.insert_text((120,80),'后续中文论述。',fontname='china-s',fontsize=11)
        doc.save(path);doc.close()
        with fitz.open(path) as doc:
            self.assertTrue(any(s['flags']&1 for b in doc[0].get_text('dict')['blocks'] if b['type']==0 for l in b['lines'] for s in l['spans']))
        blocks=inventory(path,[1]);paragraph=next(b for b in blocks if '中文论述' in b['text'])
        self.assertFalse(paragraph['protected'])
        self.assertGreaterEqual(paragraph['minimumCjkSize'],10)
        response={1:{'candidateId':'reference-fixture','sourcePage':1,'updates':[],'supplements':[],'unresolved':[]}}
        plan={'candidateId':'reference-fixture','monoSha256':digest(path),'sourcePages':[1],
              'sourceTexts':{'1':'Reference fixture'},'repairRound':1,'blocks':blocks}
        apply_repairs(path,[1],plan,response,self.root/'reference-out',[])
        self.assertEqual(read_json(self.root/'reference-out/repair-applied.json')['changes'],[],
                         'A small Latin superscript must not reflow readable Chinese prose')

    def test_unsafe_proposal_is_not_applied_and_rejection_is_retained(self):
        from repair_ops import sanitize_response
        response=copy.deepcopy(self.responses[1]);response['updates']=[self.change('速度为 21 m/s。')]
        clean=sanitize_response(self.plan,1,response)
        self.assertEqual(clean['updates'],[]);self.assertIn('numerical',clean['unresolved'][0])
        self.assertEqual(response['updates'][0]['replacement'],'速度为 21 m/s。')
        response['candidateId']='forged'
        with self.assertRaisesRegex(ValueError,'mismatch'):sanitize_response(self.plan,1,response)

    def test_repair_prompt_groups_repeated_program_findings_without_removing_evidence(self):
        from repair_ops import request_body
        import json
        plan={**self.plan,'issues':[{'id':str(n),'sourcePage':1,'detail':'small glyph','programDetails':{'kind':'font','page':1,'document':'cn'}} for n in range(100)]}
        context=json.loads(request_body(plan,1)['messages'][1]['content'])
        self.assertEqual(len(context['issues']),1);self.assertEqual(context['issues'][0]['occurrences'],100)
        self.assertEqual(len(plan['issues']),100)

    def test_protected_formula_and_arbitrary_operations_are_rejected(self):
        formula=next(b for b in self.blocks if '=' in b['text']);self.assertTrue(formula['protected'])
        response=copy.deepcopy(self.responses[1]);change=self.change();change.update(blockId=formula['id'],originalSha256=formula['sha256'])
        response['updates']=[change]
        with self.assertRaisesRegex(ValueError,'protected'):validate_response(self.plan,1,response)
        response['updates']=[self.change()];response['updates'][0]['command']='arbitrary shell'
        with self.assertRaisesRegex(ValueError,'Unsupported'):validate_response(self.plan,1,response)

    def test_changed_inventory_and_missing_page_are_rejected(self):
        plan=copy.deepcopy(self.plan);plan['blocks'][0]['bbox'][0]+=1
        with self.assertRaisesRegex(ValueError,'input changed'):apply_repairs(self.mono,[1,2],plan,self.responses,self.root/'bad',[])
        with self.assertRaisesRegex(ValueError,'coverage'):apply_repairs(self.mono,[1,2],self.plan,{1:self.responses[1]},self.root/'bad2',[])

    def test_readable_short_paragraph_uses_blank_space_without_a_new_page(self):
        path=self.root/'inline.pdf';doc=fitz.open();p=doc.new_page()
        p.insert_text((50,70),'气动力是标量。',fontname='china-s',fontsize=11)
        p.draw_rect(fitz.Rect(45,130,250,170),fill=(.8,.2,.1));doc.save(path);doc.close()
        blocks=inventory(path,[1]);block=blocks[0]
        plan={'candidateId':'inline-fixture','monoSha256':digest(path),'sourcePages':[1],
              'sourceTexts':{'1':'Aerodynamic force is a vector.'},'repairRound':1,'blocks':blocks}
        response={1:{'candidateId':'inline-fixture','sourcePage':1,'updates':[{'blockId':block['id'],
            'originalSha256':block['sha256'],'sourceQuote':plan['sourceTexts']['1'],
            'replacement':'气动力是矢量。','reason':'Synthetic semantic correction'}],'supplements':[],'unresolved':[]}}
        out=self.root/'inline-out';apply_repairs(path,[1],plan,response,out,[])
        self.assertEqual(read_json(out/'mapping.json')['sourcePages'],[1])
        with fitz.open(path) as before,fitz.open(out/'mono.pdf') as after:
            clip=fitz.Rect(40,125,255,175)
            self.assertEqual(before[0].get_pixmap(clip=clip).samples,after[0].get_pixmap(clip=clip).samples)
            self.assertIn('气动力是矢量',after[0].get_text())

    def test_blank_space_expansion_stops_at_an_existing_vector_rule(self):
        path=self.root/'rule.pdf';doc=fitz.open();p=doc.new_page()
        p.insert_text((50,70),'可读中文段落。',fontname='china-s',fontsize=11)
        rect=__import__('pymupdf').Rect(p.get_text('blocks')[0][:4]);p.draw_line((45,rect.y1+2),(250,rect.y1+2))
        doc.save(path);doc.close();block=inventory(path,[1])[0]
        self.assertLessEqual(block['replacementTextbox'][3],rect.y1+1)


if __name__=='__main__':unittest.main()
