import copy
import json
import unittest

from test_review_protocol import ReviewFixture
from test_delivery_runner import NoWorkers
from core import atomic,canonical,digest,read_json
from corpus import CorpusIndex
from delivery_runner import DeliveryRunner
from review_protocol import validate_unit


class CorpusReviewTests(ReviewFixture,unittest.TestCase):
    def configure_corpus(self):
        self.corpus=CorpusIndex(self.store.db)
        with self.store.transaction():
            source=self.corpus.add_source({'kind':'paper','job':self.job,'sha256':self.store.get(self.job)['sourceSha256'],
                                          'title':'fixture','retrievedAt':1},[{'page':1,'anchor':'p1','text':'fixture source'}])
            pid=source['passages'][0]
            self.term=self.corpus.add_translation('term','fixture','夹具','fixture-domain',pid,producer_job=self.job)
            self.memory=self.corpus.add_translation('memory','fixture source','夹具译文','fixture-domain',pid,producer_job=self.job)

    def rewrite_report(self,change):
        report=json.loads(self.response['choices'][0]['message']['content']);change(report)
        self.response['choices'][0]['message']['content']=canonical(report)
        atomic(self.private/'response.json',self.response)
        self.call['response']['sha256']=digest(self.private/'response.json');self.save_call()

    def accept_through_delivery(self):
        runner=DeliveryRunner(NoWorkers());runner.bind(self.store)
        phase={'job':self.job,'stage':'accept','generation':5};phase['worker']=runner.identity(phase)
        runner.start(phase,self.store.get(self.job));result=runner.inspect(phase)
        self.assertEqual(result['state'],'completed')

    def test_exact_reviewed_ids_promote_only_after_pdf_acceptance(self):
        self.submit();self.assertEqual(self.corpus.trusted_translations(self.store,'fixture-domain'),[])
        self.accept_through_delivery()
        trusted=self.corpus.trusted_translations(self.store,'fixture-domain')
        self.assertEqual({t['id'] for t in trusted},{self.term,self.memory})
        self.assertEqual({t['kind'] for t in trusted},{'term','memory'})

    def test_rejected_candidate_cannot_piggyback_on_passing_pdf(self):
        def reject(report):
            next(r for r in report['corpusReview'] if r['id']==self.term)['verdict']='rejected'
        self.rewrite_report(reject);self.submit();self.accept_through_delivery()
        self.assertEqual([t['id'] for t in self.corpus.trusted_translations(self.store,'fixture-domain')],[self.memory])

    def test_missing_corpus_ids_cannot_count_as_verified(self):
        self.rewrite_report(lambda r:r.update(corpusReview=[]))
        with self.assertRaisesRegex(ValueError,'corpus review identities'):self.submit()

    def test_verified_target_must_appear_in_actual_translation(self):
        unit=copy.deepcopy(self.unit);unit['corpusCandidates'][0]['target']='not present in candidate'
        report=json.loads(self.response['choices'][0]['message']['content'])
        with self.assertRaisesRegex(ValueError,'actual source or translated occurrence'):validate_unit(report,unit,self.candidate)

    def test_canonical_chinese_pdf_glyphs_do_not_fabricate_a_missing_term(self):
        unit=copy.deepcopy(self.unit);unit['sourceTranslations'][0]['text']='夹具译文；域增量学习'
        unit['corpusCandidates'][0]['target']='域增量学习'
        report=json.loads(self.response['choices'][0]['message']['content'])
        validate_unit(report,unit,self.candidate)
        unit['sourceTranslations'][0]['text']='夹具译文；域增量训练'
        with self.assertRaisesRegex(ValueError,'actual source or translated occurrence'):validate_unit(report,unit,self.candidate)

    def test_negative_paper_can_continue_to_repair_without_promoting_unsupported_term(self):
        unit=copy.deepcopy(self.unit);unit['corpusCandidates'][0]['target']='not present in candidate'
        report=json.loads(self.response['choices'][0]['message']['content'])
        report['verdict']='needs_attention';report['checks']['faithfulness']['passed']=False
        report['issues']=[{'id':'translation-error','severity':'major','status':'open','detail':'Actual negative fixture finding'}]
        normalized=validate_unit(report,unit,self.candidate)
        self.assertEqual(normalized['corpusReview'][0]['verdict'],'uncertain')
        self.assertEqual(normalized['verdict'],'needs_attention')
        self.assertEqual(report['corpusReview'][0]['verdict'],'verified','Raw provider decision must remain unchanged')

    def test_corrupted_private_review_revokes_reuse(self):
        self.submit();self.accept_through_delivery();atomic(self.private/'response.json',{})
        self.assertEqual(self.corpus.trusted_translations(self.store,'fixture-domain'),[])


if __name__=='__main__':unittest.main()
