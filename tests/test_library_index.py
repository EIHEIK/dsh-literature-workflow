from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from batches import BatchStore
from core import atomic,digest,read_json
from corpus import CorpusIndex
from library_index import import_attachment,PDFScanner
from pdf_scan_worker import scan
from stage_handlers import BackgroundHandler
from test_workflow import pdf


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.test-output');self.root=Path(self.tmp.name)
        self.source=self.root/'source.pdf';pdf(self.source,label='aerodynamics fixture')
        self.store=BatchStore(self.root/'data');source=self.source
        class Client:
            def get(self,path):
                return {'data':{'itemType':'attachment','contentType':'application/pdf','parentItem':'ABCDEFGH'}} if path.endswith('IJKLMNOP') else {'data':{'itemType':'journalArticle','title':'aerodynamics fixture'}}
            def file(self,key):return str(source)
        class Scanner:
            def scan(self,path):return scan(path),{'firstIndexedAt':1}
        self.client=Client();self.scanner=Scanner()

    def tearDown(self):self.store.db.close();self.tmp.cleanup()

    def test_real_pdf_scan_and_idempotent_grounded_registration(self):
        first=import_attachment(self.store,self.client,self.scanner,'ABCDEFGH','IJKLMNOP',expected_sha=digest(self.source))
        again=import_attachment(self.store,self.client,self.scanner,'ABCDEFGH','IJKLMNOP',expected_sha=digest(self.source))
        self.assertEqual(first,again);index=CorpusIndex(self.store.db);matches=index.search('aerodynamics')
        self.assertEqual(len(matches),2);self.assertEqual(matches[0]['reference']['attachmentKey'],'IJKLMNOP')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0],0)

    def test_own_pdf_is_not_counted_as_related_source(self):
        import_attachment(self.store,self.client,self.scanner,'ABCDEFGH','IJKLMNOP')
        summary=self.store.prepare([{'source':str(self.source),'title':'aerodynamics fixture'}],{'revision':'fixture'})
        job=self.store.get(summary['items'][0]['job']);phase=self.store.phase(job['id'])
        prepared=BackgroundHandler().prepare(self.store,phase,job)
        references=read_json(prepared['inputs']['/input/references.json'])
        self.assertEqual(references['sourceCount'],0);self.assertEqual(references['references'],[])

    def test_source_change_during_scan_is_not_registered(self):
        original=self.scanner.scan
        def changed(path):
            result=original(path);self.source.write_bytes(b'changed after scan');return result
        with patch.object(self.scanner,'scan',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'changed during extraction'):
                import_attachment(self.store,self.client,self.scanner,'ABCDEFGH','IJKLMNOP')
        corpus=CorpusIndex(self.store.db)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM corpus_sources').fetchone()[0],0)

    def test_changed_intake_and_missing_extraction_pages_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'source changed'):
            import_attachment(self.store,self.client,self.scanner,'ABCDEFGH','IJKLMNOP',expected_sha='0'*64)
        data=scan(self.source);data['pages'].pop();out=self.root/'output';out.mkdir();atomic(out/'scan.json',data)
        with self.assertRaisesRegex(ValueError,'Missing PDF extraction pages'):PDFScanner.validate(out,digest(self.source))

    def test_batch_intake_uses_verified_scan_without_host_pdf_parser(self):
        info=scan(self.source)
        with patch('core.pdf_info',side_effect=AssertionError('Host parser must not run')):
            summary=self.store.prepare([{'source':str(self.source),'_sourceInfo':info}],{'revision':'fixture'})
        self.assertEqual(len(summary['items']),1)
        info['sourceSha256']='0'*64
        with self.assertRaisesRegex(ValueError,'scan no longer matches'):
            self.store.prepare([{'source':str(self.source),'_sourceInfo':info}],{'revision':'changed-fixture'})

    def test_resolver_requires_versioned_extraction_receipt(self):
        from zotero_resolver import ZoteroResolver
        from source_text import EXTRACTOR_VERSION
        root=self.root/'scans';sha=digest(self.source)
        scan_data=scan(self.source)
        for folder in (root/sha,root/EXTRACTOR_VERSION/sha):
            atomic(folder/'output/scan.json',scan_data)
            atomic(folder/'receipt.json',{'sourceSha256':sha,'outputSha256':digest(folder/'output/scan.json')})
        resolver=ZoteroResolver(root,client=self.client)
        inventory=[{'parentItemKey':'ABCDEFGH','sourceAttachmentKey':'IJKLMNOP','title':'aerodynamics fixture'}]
        with patch.object(resolver,'inventory',side_effect=lambda scope:(inventory,[])):
            self.assertEqual(len(resolver({'kind':'items','keys':['ABCDEFGH']},'all')[1]),1)
            folder=root/EXTRACTOR_VERSION/sha
            receipt=__import__('core').read_json(folder/'receipt.json');receipt['extractorVersion']=EXTRACTOR_VERSION
            atomic(folder/'receipt.json',receipt)
            inputs,omissions=resolver({'kind':'items','keys':['ABCDEFGH']},'all')
            self.assertFalse(omissions);self.assertEqual(inputs[0]['_sourceInfo']['extractorVersion'],EXTRACTOR_VERSION)


if __name__=='__main__':unittest.main()
