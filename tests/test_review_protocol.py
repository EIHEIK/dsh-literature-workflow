"""Synthetic receipts only; these tests do not claim paper quality or call APIs."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from core import Store,atomic,canonical,digest,read_json,CHECKS,object_hash
from model_broker import ModelBroker,PriceBook,provider_payload
from review_protocol import POLICY,begin_review,request_body,reconstruct,submit_review
from test_workflow import pdf

ROOT=Path(__file__).resolve().parents[1]


class ReviewFixture:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.test-output');self.root=Path(self.tmp.name)
        count=getattr(self,'fixture_pages',1)
        source=self.root/'source.pdf';pdf(source,pages=count)
        self.store=Store(self.root/'data')
        with self.store.transaction():
            job=self.store.create({'source':str(source)},'fixture-producer')
            job=self.store.stage(job['id'],{'mono':str(source)},'fixture-producer')
            self.job=job['id'];c=job['candidate'];self.folder=Path(c['files']['cn']['path']).parent
            for label in ('source','cn'):
                for n in range(1,count+1):
                    original=self.folder/f'preview/{label}-{n:04}.png'
                    for half in ('top','bottom'):
                        name=f'preview/{label}-{n:04}-{half}.png';p=self.folder/name;shutil.copyfile(original,p)
                        c['files'][name]={'path':str(p),'sha256':digest(p)}
            p=self.folder/'review-text.json';atomic(p,{'source':{str(n):'fixture source' for n in range(1,count+1)},
                                                     'cn':{str(n):'夹具译文' for n in range(1,count+1)}})
            c['files']['reviewText']={'path':str(p),'sha256':digest(p)};self.store.save(job)
        self.candidate=self.store.get(self.job)['candidate']
        ModelBroker(self.store,PriceBook(read_json(ROOT/'deploy/deepseek-pricing.json')))
        self.configure_corpus()
        self.phase={'job':self.job,'stage':'review','generation':4}
        self.plan,_=begin_review(self.store,self.phase)
        self.ticket=json.loads(self.store.db.execute('SELECT body FROM tickets WHERE id=?',(self.plan['ticket'],)).fetchone()[0])
        self.unit=self.plan['units'][0];self.call_id='SYNTHETIC-NOT-A-REAL-API-CALL'
        report={'unitId':self.unit['id'],'candidateId':self.candidate['id'],
                'checks':{k:{'passed':True,'evidence':'SYNTHETIC TEST EVIDENCE'} for k in CHECKS},
                'issues':[],'verdict':'pass','summary':'SYNTHETIC TEST ONLY',
                'corpusReview':[{'id':t['id'],'verdict':'verified','evidence':'SYNTHETIC CORPUS REVIEW'} for t in self.unit['corpusCandidates']]}
        self.response={'id':'fixture-response','choices':[{'message':{'content':canonical(report)}}]}
        self.request=provider_payload(request_body(self.plan,self.unit,self.folder),POLICY)
        self.private=self.store.root/'broker-private'
        atomic(self.private/'request.json',self.request);atomic(self.private/'response.json',self.response)
        self.call={'id':self.call_id,'state':'completed','job':self.job,'policy':POLICY,
                   'request':{'path':str(self.private/'request.json'),'sha256':digest(self.private/'request.json')},
                   'response':{'path':str(self.private/'response.json'),'sha256':digest(self.private/'response.json')}}
        with self.store.transaction():
            self.store.db.execute('INSERT INTO model_calls VALUES(?,?,?,?,?,?)',(self.call_id,self.job,'fixture',1,'completed',canonical(self.call)))
            self.store.db.execute('INSERT INTO model_deliveries VALUES(?,?,?,?,?,?)',(self.job,'review',4,'fixture',self.call_id,object_hash(self.request)))
        self.bindings={self.unit['id']:self.call_id}

    def configure_corpus(self):pass

    def tearDown(self):self.store.db.close();self.tmp.cleanup()

    def reconstruct(self,bindings=None):
        return reconstruct(self.store,self.ticket,self.candidate,self.bindings if bindings is None else bindings)

    def save_call(self):
        with self.store.transaction():self.store.db.execute('UPDATE model_calls SET body=? WHERE id=?',(canonical(self.call),self.call_id))

    def submit(self):
        work=self.root/'work';work.mkdir(exist_ok=True)
        p=work/'review-receipts.json';atomic(p,{'ticket':self.ticket['id'],'receipts':[
            {'unit':self.unit['id'],'providerId':self.response['id'],'responseHash':object_hash(self.response)}]})
        return submit_review(self.store,self.phase,{'work':str(work)},
                             {'ticket':self.ticket['id'],'receiptHash':digest(p)})


class ReviewProtocolTests(ReviewFixture,unittest.TestCase):
    def test_visual_quote_manifest_is_bound_to_source_page_image(self):
        from review_protocol import build_plan
        candidate=copy.deepcopy(self.candidate);image=candidate['files']['preview/source-0001.png']['sha256']
        quote={'sourceSha256':candidate['sourceSha256'],'sourcePage':1,'imageSha256':image,'quote':'Speed is −20 m/s.'}
        quote={'id':object_hash(quote),**quote};path=self.folder/'source-quotes.json';atomic(path,{'candidates':[quote]})
        candidate['files']['sourceQuotes']={'path':str(path),'sha256':digest(path)}
        job=self.store.get(self.job)
        plan=build_plan(job,candidate,'fixture');self.assertEqual(plan['units'][0]['sourceQuoteCandidates'],[quote])
        quote['imageSha256']='b'*64;quote['id']=object_hash({k:v for k,v in quote.items() if k!='id'})
        atomic(path,{'candidates':[quote]});candidate['files']['sourceQuotes']['sha256']=digest(path)
        with self.assertRaisesRegex(ValueError,'source image'):build_plan(job,candidate,'fixture')

    def test_visual_transcription_needs_explicit_independent_verification(self):
        from review_protocol import validate_unit
        report=json.loads(self.response['choices'][0]['message']['content'])
        unit={**self.unit,'sourceQuoteCandidates':[{'id':'visual-quote'}]}
        with self.assertRaisesRegex(ValueError,'visual source'):validate_unit(report,unit,self.candidate)
        report['sourceEvidenceReview']=[{'id':'visual-quote','verdict':'uncertain','evidence':'Sign unclear in original.'}]
        with self.assertRaisesRegex(ValueError,'cannot pass'):validate_unit(report,unit,self.candidate)
        report['sourceEvidenceReview'][0]['verdict']='verified'
        validate_unit(report,unit,self.candidate)
        report['sourceEvidenceReview'][0]['id']='wrong-image-quote'
        with self.assertRaisesRegex(ValueError,'visual source'):validate_unit(report,unit,self.candidate)

    def test_process_ticket_does_not_change_actual_candidate_review_request(self):
        other=copy.deepcopy(self.plan);other['ticket']='different-recovery-ticket'
        self.assertEqual(request_body(self.plan,self.unit,self.folder),request_body(other,self.unit,self.folder))
        other['candidateId']='different-candidate'
        self.assertNotEqual(request_body(self.plan,self.unit,self.folder),request_body(other,self.unit,self.folder))

    def test_misnested_report_preserves_negative_decision_and_all_evidence(self):
        from review_json import load_review
        report=json.loads(self.response['choices'][0]['message']['content']);report['verdict']='needs_attention'
        nested={k:report[k] for k in ('unitId','candidateId','checks')}
        nested['checks']={**report['checks'],**{k:report[k] for k in ('issues','verdict','summary','corpusReview')}}
        raw=json.dumps(nested)[:-1];fixed=load_review(raw)
        self.assertEqual(fixed['verdict'],'needs_attention');self.assertEqual(fixed['checks'],report['checks'])
        validate=__import__('review_protocol').validate_unit
        validate(fixed,self.unit,self.candidate)
        with self.assertRaises(ValueError):load_review('{"candidateId":"fake","checks":{')
    def test_auxiliary_note_is_kept_without_replacing_required_checks(self):
        from review_protocol import validate_unit
        report=json.loads(self.response['choices'][0]['message']['content'])
        report['checks']['complete_scope_note']='Supplementary explanation only'
        normalized=validate_unit(report,self.unit,self.candidate)
        self.assertEqual(set(normalized['checks']),set(CHECKS))
        self.assertIn('complete_scope_note',normalized['auxiliaryNotes'])
        del report['checks']['faithfulness']
        with self.assertRaisesRegex(ValueError,'Missing independent checks'):validate_unit(report,self.unit,self.candidate)

    def test_complete_receipts_allow_acceptance_and_are_rechecked(self):
        self.assertEqual(self.submit()['verdict'],'pass')
        with self.store.transaction():self.assertEqual(self.store.accept(self.job)['state'],'accepted')
        self.store.db.execute('DELETE FROM model_deliveries');self.store.db.commit()
        with self.assertRaisesRegex(ValueError,'controller receipt'):self.store.acceptance(self.store.get(self.job))

    def test_restart_reuses_plan_and_submission_without_new_model_calls(self):
        plan,_=begin_review(self.store,self.phase);self.assertEqual(plan,self.plan)
        first=self.submit();self.assertEqual(self.submit(),first)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0],1)

    def test_missing_page_or_wrong_generation_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'coverage'):self.reconstruct({})
        self.ticket['generation']=5
        with self.assertRaisesRegex(ValueError,'controller receipt'):self.reconstruct()

    def test_validly_hashed_wrong_image_request_is_rejected(self):
        changed=copy.deepcopy(self.request);changed['messages'][1]['content']=changed['messages'][1]['content'][:-1]
        atomic(self.private/'request.json',changed);self.call['request']['sha256']=digest(self.private/'request.json');self.save_call()
        with self.assertRaisesRegex(ValueError,'exact review images'):self.reconstruct()

    def test_model_cannot_override_program_failure(self):
        path=Path(self.candidate['files']['qa']['path']);qa=read_json(path);qa['hardFailures']=['tiny Chinese text'];atomic(path,qa)
        self.candidate['files']['qa']['sha256']=digest(path)
        with self.store.transaction():
            job=self.store.get(self.job);job['candidate']=self.candidate;self.store.save(job)
        self.assertEqual(self.submit()['verdict'],'needs_attention')
        with self.assertRaisesRegex(ValueError,'Deterministic QA'):self.store.acceptance(self.store.get(self.job))

    def test_changed_private_response_is_rejected(self):
        atomic(self.private/'response.json',{})
        with self.assertRaisesRegex(ValueError,'Changed review evidence'):self.reconstruct()

    def test_worker_cannot_pass_empty_or_duplicate_unit_receipts(self):
        self.response['id']='not-delivered'
        with self.assertRaisesRegex(ValueError,'private review response'):self.submit()


class EarlyNegativeReviewTests(ReviewFixture,unittest.TestCase):
    fixture_pages=2

    def negative(self):
        report=json.loads(self.response['choices'][0]['message']['content'])
        report['verdict']='needs_attention';report['checks']['faithfulness']['passed']=False
        report['issues']=[{'id':'confirmed','severity':'major','status':'open','detail':'SYNTHETIC numerical discrepancy'}]
        self.response['choices'][0]['message']['content']=canonical(report)
        atomic(self.private/'response.json',self.response)
        self.call['response']['sha256']=digest(self.private/'response.json');self.save_call()

    def test_partial_material_failure_can_repair_but_never_accept_or_promote(self):
        self.negative();report=self.reconstruct()
        self.assertEqual(report['verdict'],'needs_attention');self.assertFalse(report['checks']['complete_scope'])
        self.assertEqual(report['reviewCoverage'],{'complete':False,'reviewedUnits':1,'requiredUnits':2})
        self.assertEqual(report['visualCnPages'],[1]);self.assertEqual(report['semanticSourcePages'],[1])
        self.assertEqual(self.submit()['verdict'],'needs_attention')
        with self.assertRaisesRegex(ValueError,'has not passed'):self.store.acceptance(self.store.get(self.job))
        self.assertFalse(report['corpusReview']['verifiedTermIds'])

    def test_missing_positive_units_and_unverified_negative_receipts_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'coverage'):self.reconstruct()
        self.negative();self.ticket['generation']=5
        with self.assertRaisesRegex(ValueError,'controller receipt'):self.reconstruct()


if __name__=='__main__':unittest.main()
