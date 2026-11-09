import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import urllib.error

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'));sys.path.insert(0,str(ROOT/'runtime'))
from core import atomic,digest
from zotero_native_bridge import Gateway


class NativeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'.test-output');self.root=Path(self.tmp.name);data=self.root/'data';data.mkdir()
        db=sqlite3.connect(data/'state.sqlite3');db.execute('CREATE TABLE jobs(id TEXT,body TEXT)');db.commit();db.close()
        service=self.root/'service.json';atomic(service,{'data':str(data),'runtime':str(ROOT/'runtime')})
        config=self.root/'bridge.json';atomic(config,{'serviceConfig':str(service),'serverID':'fixture','key':'fixture-not-a-real-key'})
        self.gateway=Gateway(config);self.pdf=self.root/'publication.pdf';self.pdf.write_bytes(b'FIXTURE PDF BYTES')
        self.allowed={'job':'fixture','parent':'ABCDEFGH','path':str(self.pdf),'sha256':digest(self.pdf),'title':'验收译本 fixture'}
        self.args={'action':'import','filePath':str(self.pdf),'parentItemKey':'ABCDEFGH','title':self.allowed['title'],'linkMode':'imported_file'}
    def tearDown(self):self.tmp.cleanup()

    def test_arbitrary_file_never_reaches_native_write(self):
        with self.assertRaises(PermissionError):self.gateway.import_file(self.args)
        self.assertFalse(list(self.gateway.intents.glob('*.json')))

    def test_only_fixed_import_shape_is_exposed(self):
        with self.assertRaises(ValueError):self.gateway.import_file({**self.args,'command':'anything'})
        with self.assertRaises(ValueError):self.gateway.import_file({**self.args,'action':'delete'})

    def test_lost_metadata_response_is_reconciled_without_duplicate_attachment(self):
        allowed=self.allowed;gateway=self.gateway
        gateway.authorize=lambda *args:copy.deepcopy(allowed) # Explicit synthetic acceptance fixture only.
        class Fake:
            def __init__(self):self.items={};self.creates=0;self.uploads=0;self.fail=True;self.uploaded=False
            def children(self,parent):return list(self.items.values())
            def request(self,path,method='GET',body=None,headers=None):
                if path.startswith('/items/new'):return {'itemType':'attachment','linkMode':'imported_file'}
                if path=='/users/0/items' and method=='POST':
                    self.creates+=1;item={'key':'IJKLMNOP','version':1,'data':copy.deepcopy(body[0])};self.items['IJKLMNOP']=item
                    if self.fail:self.fail=False;raise urllib.error.URLError('synthetic lost ACK')
                    return {'successful':{'0':item}}
                if method=='PATCH':self.items['IJKLMNOP']['data'].update(body);return None
                return self.items['IJKLMNOP']
            def matches(self,item,sha):return self.uploaded
            def upload(self,item,path):self.uploads+=1;self.uploaded=True
        fake=Fake();gateway.native=fake
        with self.assertRaises(urllib.error.URLError):gateway.import_file(self.args)
        result=gateway.import_file(self.args);again=gateway.import_file(self.args)
        self.assertTrue(result['verified']);self.assertEqual(result,again)
        self.assertEqual(fake.creates,1);self.assertEqual(fake.uploads,1)
        self.assertEqual(fake.items['IJKLMNOP']['data']['title'],allowed['title'])
        self.assertEqual(fake.items['IJKLMNOP']['data']['parentItem'],'ABCDEFGH')

    def test_revoked_key_blocks_recovery_without_reauthorizing(self):
        for status,expected in [(401,'authorization_required'),(412,'server_identity_changed')]:
            path=self.gateway.intents/(str(status)+'.json');atomic(path,{**self.allowed,'state':'pending'})
            calls=[]
            def reject(args):
                calls.append(args);raise urllib.error.HTTPError('http://127.0.0.1/',status,'synthetic',{},None)
            self.gateway.import_file=reject;self.gateway.recover_pending_once(now=100)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['state'],expected)
            self.gateway.recover_pending_once(now=100000)
            self.assertEqual(len(calls),1)

    def test_transport_recovery_preserves_newer_intent_and_has_a_limit(self):
        path=self.gateway.intents/'pending.json';atomic(path,{**self.allowed,'state':'pending'})
        calls=[]
        def lose_ack(args):
            calls.append(args);record=json.loads(path.read_text(encoding='utf-8'));record['attachmentKey']='IJKLMNOP'
            atomic(path,record);raise urllib.error.URLError('synthetic')
        self.gateway.import_file=lose_ack
        self.gateway.recover_pending_once(now=100);self.gateway.recover_pending_once(now=101)
        self.assertEqual(len(calls),1)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['attachmentKey'],'IJKLMNOP')
        for n in range(1,8):self.gateway.recover_pending_once(now=100+n*2000)
        self.assertEqual(len(calls),6)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['state'],'needs_attention')
