"""Synthetic promotion evidence only; no service/API/Zotero writes."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from activate_verified_batch import verify_scope,verify_archive,verify_terminal


class BatchActivationTests(unittest.TestCase):
    def setUp(self):
        self.intake={'ready':[{'sourceSha256':str(i),'zotero_key':'PARENT'+str(i),'pages':2} for i in range(3)]}
        def item(i):return {'sourceSha256':str(i),'zotero':{'parentItemKey':'PARENT'+str(i)},'pages':[1,2]}
        budget={'perPaperCap':10_000_000,'perPaperTarget':5_000_000,'batchCap':None}
        self.pilot={**budget,'pilot':True,'pilotCap':15_000_000,'items':[item(0)]}
        self.bulk={**budget,'pilot':False,'items':[item(1),item(2)]}

    def test_scope_covers_original_list_once_and_retains_budgets(self):
        verify_scope(self.intake,self.pilot,self.bulk)
        for change in ('duplicate','omit','range','parent','budget'):
            bulk=copy.deepcopy(self.bulk)
            if change=='duplicate':bulk['items'].append(self.pilot['items'][0])
            if change=='omit':bulk['items'].pop()
            if change=='range':bulk['items'][0]['pages']=[1]
            if change=='parent':bulk['items'][0]['zotero']['parentItemKey']='CHANGED'
            if change=='budget':bulk['perPaperCap']+=1
            with self.subTest(change=change),self.assertRaises(ValueError):verify_scope(self.intake,self.pilot,bulk)

    def test_only_real_terminal_archive_stage_can_promote(self):
        verify_terminal({'stage':'archive','state':'done'},{'state':'archived'})
        for phase,job in [({'stage':'archive','state':'blocked'},{'state':'archived'}),
                          ({'stage':'review','state':'done'},{'state':'archived'}),
                          ({'stage':'archive','state':'done'},{'state':'published'})]:
            with self.assertRaises(ValueError):verify_terminal(phase,job)

    def test_archive_must_match_current_zotero_parent_and_file_bytes(self):
        job={'zotero':{'parentItemKey':'PARENT'},'publication':{'id':'pub','outputs':{}},'archive':{}}
        children=[]
        for mode in ('cn','bi'):
            job['publication']['outputs'][mode]={'path':mode+'.pdf','sha256':mode}
            job['archive'][mode]={'state':'verified','attachmentKey':mode,'parentItemKey':'PARENT','publication':'pub','sha256':mode,'title':mode}
            children.append({'key':mode,'data':{'parentItem':'PARENT','itemType':'attachment','title':mode}})
        class Client:
            def children(self,parent):return copy.deepcopy(children)
            def file(self,key):return key+'.pdf'
        digest=lambda path:Path(path).stem
        verify_archive(job,Client(),digest)
        with self.assertRaises(ValueError):verify_archive(job,Client(),lambda p:'wrong')
        children[0]['data']['parentItem']='CHANGED'
        with self.assertRaises(ValueError):verify_archive(job,Client(),digest)
        children[0]['data']['parentItem']='PARENT'
        job['archive'].pop('bi')
        with self.assertRaises(ValueError):verify_archive(job,Client(),digest)
