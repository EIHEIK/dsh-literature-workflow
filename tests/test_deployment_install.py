from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'runtime'))
from core import atomic,read_json
from deployment_install import ENTRIES,snapshot,verify_release,activate,rollback,verify_installation
from instance_lock import InstanceLock


class DeploymentInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.test-output');self.root=Path(self.tmp.name)
        self.source=self.root/'source';self.source.mkdir();self.releases=self.root/'releases'
        for entry in ENTRIES:
            path=self.source/entry
            if '.' in entry:path.write_text('fixture',encoding='utf-8')
            else:path.mkdir();(path/'fixture.txt').write_text(entry,encoding='utf-8')
        self.config=self.root/'private/service.json';self.data=self.root/'data';self.data.mkdir()
        atomic(self.config,{'runtime':str(self.source/'runtime'),'data':str(self.data),'paidWorkEnabled':True,'port':12345,
                            'credentials':{'fixture':'not-a-real-key'}})
        db=sqlite3.connect(self.data/'state.sqlite3');db.execute('CREATE TABLE retained_cost(value INTEGER)');db.execute('INSERT INTO retained_cost VALUES(123)');db.commit();db.close()
        self.fp={'fixture':'dependency-fingerprint'}

    def tearDown(self):self.tmp.cleanup()

    def test_snapshot_stable_and_changed_or_added_files_fail_verification(self):
        release,sha=snapshot(self.source,self.releases)
        self.assertEqual(snapshot(self.source,self.releases),(release,sha))
        (release/'runtime/extra.py').write_text('unexpected',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'Unexpected files'):verify_release(release,sha)
        (release/'runtime/extra.py').unlink()
        (release/'runtime/fixture.txt').write_text('changed',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'content changed'):verify_release(release,sha)

    def test_install_is_offline_and_does_not_replace_database(self):
        release,sha=snapshot(self.source,self.releases)
        with patch('deployment_install.python_fingerprint',return_value=self.fp):
            record=activate(self.config,release,sha);self.assertEqual(verify_installation(self.config)['id'],record['id'])
        config=read_json(self.config);self.assertFalse(config['paidWorkEnabled']);self.assertEqual(config['port'],12345)
        self.assertEqual(config['credentials']['fixture'],'not-a-real-key')
        self.assertGreaterEqual(len(config['credentials']['codex']),40)
        self.assertEqual(config['supervisionMode'],'codex')
        db=sqlite3.connect(self.data/'state.sqlite3');self.assertEqual(db.execute('SELECT value FROM retained_cost').fetchone()[0],123);db.close()
        backup=Path(record['backup']);self.assertTrue((backup/'state.sqlite3').is_file());self.assertTrue((backup/'service.json').is_file())

    def test_running_controller_blocks_activation(self):
        release,sha=snapshot(self.source,self.releases)
        with patch('deployment_install.python_fingerprint',return_value=self.fp),InstanceLock(self.data):
            with self.assertRaisesRegex(RuntimeError,'Another controller'):activate(self.config,release,sha)
        self.assertEqual(read_json(self.config)['runtime'],str(self.source/'runtime'))

    def test_code_rollback_preserves_new_costs_and_disables_paid_work(self):
        first,h1=snapshot(self.source,self.releases)
        with patch('deployment_install.python_fingerprint',return_value=self.fp):
            original=activate(self.config,first,h1)
            (self.source/'runtime/fixture.txt').write_text('next version',encoding='utf-8')
            second,h2=snapshot(self.source,self.releases);activate(self.config,second,h2)
            db=sqlite3.connect(self.data/'state.sqlite3');db.execute('UPDATE retained_cost SET value=987');db.commit();db.close()
            restored=rollback(self.config,original['id']);self.assertEqual(restored['kind'],'rollback-code-only')
        self.assertEqual(read_json(self.config)['runtime'],str(first/'runtime'))
        db=sqlite3.connect(self.data/'state.sqlite3');self.assertEqual(db.execute('SELECT value FROM retained_cost').fetchone()[0],987);db.close()

    def test_manifest_cannot_escape_release(self):
        release,sha=snapshot(self.source,self.releases);manifest=read_json(release/'release-manifest.json')
        manifest['files']['../outside']='0'*64;atomic(release/'release-manifest.json',manifest)
        from core import object_hash
        with self.assertRaises(ValueError):verify_release(release,object_hash(manifest))


if __name__=='__main__':unittest.main()
