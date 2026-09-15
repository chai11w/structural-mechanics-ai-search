import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tiku_shared.bank_publication import PublicationStore, PublicationError, canonical, verify_bundle


class ObjectStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='bank-objects-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.engine = self.reopen()

    def reopen(self):
        return PublicationStore(self.root/'published', self.root/'private', self.root/'receipts',
            validator=lambda p: json.loads((p/'registry.json').read_bytes()), incremental=True)

    def stage(self, label):
        op = 'op_' + uuid4().hex
        d = self.engine.candidate_directory(op)
        (d/'main/题目').mkdir(parents=True); (d/'symbolic').mkdir()
        (d/'main/题目/1.jpg').write_bytes(b'unchanged media' * 100000)
        (d/'main/index.xlsx').write_bytes(label.encode())
        (d/'registry.json').write_bytes(canonical({'records': {}, 'label': label}))
        p = self.engine.prepare(op, expected=self.engine.current(), summary={'label':label}, principal='owner', channel='lida')
        r = self.engine.review(op, principal='owner', channel='lida')
        self.engine.approve(op, plan_digest=p['plan_digest'], challenge=r['approval_challenge'], principal='owner', channel='lida')
        return p

    def publish(self, label):
        p = self.stage(label)
        r = self.engine.execute(p['operation_id'], plan_digest=p['plan_digest'])
        return self.engine.root/'versions'/r['result']['version'], r

    def objects(self):
        return {p.name:p.stat().st_size for p in (self.engine.root/'objects').glob('*/*') if p.is_file()}

    def test_unchanged_bytes_are_shared_and_only_changed_objects_are_added(self):
        first, a = self.publish('first'); before = self.objects()
        second, b = self.publish('second'); after = self.objects()
        self.assertTrue(os.path.samefile(first/'main/题目/1.jpg', second/'main/题目/1.jpg'))
        self.assertFalse(os.path.samefile(first/'main/index.xlsx', second/'main/index.xlsx'))
        self.assertLess(sum(after[k] for k in after.keys()-before.keys()), 1024)
        self.assertEqual((first/'main/index.xlsx').read_bytes(), b'first')
        self.assertEqual((self.engine.root/'current/main/index.xlsx').read_bytes(), b'second')
        verify_bundle(first, a['result']['version']); verify_bundle(second, b['result']['version'])
        self.engine = self.reopen()
        self.assertEqual(self.engine.execute(b['operation_id'], plan_digest=b['plan_digest'])['result'], b['result'])
        self.assertEqual(self.objects(), after)

    def test_an_arbitrary_external_hardlink_is_not_an_object(self):
        op='op_'+uuid4().hex; d=self.engine.candidate_directory(op)
        (d/'main').mkdir(parents=True); (d/'symbolic').mkdir()
        (d/'registry.json').write_text('{}')
        external=self.root/'external'; external.write_bytes(b'not a stored object')
        os.link(external,d/'main/unsafe.jpg')
        with self.assertRaisesRegex(PublicationError,'ambiguous-bundle-file'):
            self.engine.prepare(op,expected=None,summary={},principal='owner',channel='lida')
        self.assertIsNone(self.engine.current())

    def test_link_limit_rotates_an_object_without_modifying_prior_versions(self):
        from tiku_shared.bank_objects import shared_file
        with patch('tiku_shared.bank_objects.MAX_OBJECT_LINKS', 4):
            versions = [self.publish(str(i)) for i in range(4)]
        first = versions[0][0] / 'main/题目/1.jpg'
        last = versions[-1][0] / 'main/题目/1.jpg'
        self.assertFalse(os.path.samefile(first, last))
        self.assertEqual(first.read_bytes(), last.read_bytes())
        for directory, result in versions:
            manifest = verify_bundle(directory, result['result']['version'])
            item = next(i for i in manifest['files'] if i['path'] == 'main/题目/1.jpg')
            self.assertTrue(shared_file(directory / item['path'], item['sha256']))

    def test_corrupted_object_is_detected_and_never_silently_replaced(self):
        first, a=self.publish('first'); p=self.stage('second')
        image=first/'main/题目/1.jpg'; original=image.read_bytes()
        image.write_bytes(b'corrupt')
        with self.assertRaises(PublicationError):
            self.engine.execute(p['operation_id'],plan_digest=p['plan_digest'])
        self.assertEqual(self.engine.current(),a['result'])
        self.assertEqual(image.read_bytes(),b'corrupt')
        image.write_bytes(original)
        self.assertEqual(self.engine.execute(p['operation_id'],plan_digest=p['plan_digest'])['state'],'published')

    def test_process_crashes_recover_pointer_alias_and_publish_only_once(self):
        self.publish('initial')
        code = '''
import json,os,sys
from pathlib import Path
from tiku_shared.bank_publication import PublicationStore
root=Path(sys.argv[1])
def checkpoint(name):
    if name==sys.argv[4]: os._exit(73)
engine=PublicationStore(root/'published',root/'private',root/'receipts',
    validator=lambda p:json.loads((p/'registry.json').read_bytes()),
    incremental=True,checkpoint=checkpoint)
engine.execute(sys.argv[2],plan_digest=sys.argv[3])
'''
        for point in ('after-version','before-pointer','after-pointer','after-journal'):
            with self.subTest(point=point):
                before=self.engine.current(); prepared=self.stage(point)
                child=subprocess.run([sys.executable,'-B','-c',code,str(self.root),
                    prepared['operation_id'],prepared['plan_digest'],point],capture_output=True,text=True)
                self.assertEqual(child.returncode,73,child.stderr)
                self.engine=self.reopen(); self.engine.recover()
                result=self.engine.execute(prepared['operation_id'],plan_digest=prepared['plan_digest'])
                self.assertEqual(result['state'],'published')
                self.assertEqual(result['result']['revision'],before['revision']+1)
                self.assertEqual((self.engine.root/'current').resolve(),self.engine.root/'versions'/result['result']['version'])
                self.assertEqual(sum(e['event']=='published' for e in self.engine.audit(prepared['operation_id'])),1)


if __name__=='__main__':
    unittest.main()
