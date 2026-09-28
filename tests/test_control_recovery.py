"""Recovery uses a fresh transport fence and the original durable command."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from tests.test_background_http import BackgroundHttpFixture
from tiku_agent.execution_runtime import OPERATION_HEADER


class ControlRecoveryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_missing_background_job_does_not_trap_startup(self):
        root = Path(__file__).resolve().parents[1]
        script = r"""
const assert = require('node:assert/strict');
const text = require('node:fs').readFileSync('tiku_agent/demo_web/demo.js','utf8');
const source = text.slice(text.indexOf('async function retryConnection()'),text.indexOf('form.addEventListener',text.indexOf('async function retryConnection()')));
(async()=>{
 for (const code of ['EXECUTION_NOT_FOUND','EXECUTION_STALE','EXECUTION_AUTH_REQUIRED']) {
  let bootstraps=0, failures=0, observes=0;
  const receipt={key:'original-command',epoch:'a'.repeat(32),done:false};
  const isBusy=false,backgroundEnabled=true,executionContext=null;
  const backgroundClient={records:()=>[receipt],query:async()=>{throw Object.assign(new Error('missing'),{code});}};
  const setStatus=()=>{};
  const backgroundNotice=()=>failures++;
  const runSessionBootstrap=async()=>{bootstraps++;};
  const resumeBackgroundJobs=async()=>{observes++;};
  eval(source+';globalThis.runRecovery=retryConnection;');
  await globalThis.runRecovery();
  assert.equal(bootstraps,code==='EXECUTION_AUTH_REQUIRED'?0:1);
  assert.equal(observes,0);
  assert.equal(receipt.done,false); // not forgotten and not resubmitted
  assert.equal(failures,code==='EXECUTION_AUTH_REQUIRED'?1:0);
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which('node'), '-'], input=script,
            cwd=root, encoding='utf8', capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reconciled_control_fence_can_retry_with_original_operation(self):
        with tempfile.TemporaryDirectory() as root:
            h = BackgroundHttpFixture(root)
            with TestClient(h.app) as client:
                context = h.login(client)
                headers = h.headers(context)
                original = headers[OPERATION_HEADER]
                fence = headers['X-Session-Request-Fence']
                reconciled = client.get('/api/session', headers={
                    'X-Session-Coordination-Version': '6',
                    'X-Session-Reconcile-Fences': json.dumps([fence]),
                })
                self.assertEqual(reconciled.status_code, 200)
                rejected = client.post('/api/reset', headers=headers)
                self.assertEqual(rejected.status_code, 409)
                self.assertEqual(rejected.json()['code'], 'STALE_ACTION')
                headers['X-Session-Request-Fence'] = str(int(time.time()*1000))+':'+uuid4().hex
                reset = client.post('/api/reset', headers=headers)
                self.assertEqual(reset.status_code, 200, reset.text)
                epoch = reset.json()['execution']['epoch']
                self.assertNotEqual(epoch, context['epoch'])
                # Even after an old transport fence expires, an explicit retry
                # must retrieve the same command rather than reset again.
                headers['X-Session-Request-Fence'] = '1000000000000:'+uuid4().hex
                expired = client.post('/api/reset', headers=headers)
                self.assertEqual(expired.status_code, 409)
                headers['X-Session-Request-Fence'] = str(int(time.time()*1000))+':'+uuid4().hex
                self.assertEqual(headers[OPERATION_HEADER], original)
                replay = client.post('/api/reset', headers=headers)
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertEqual(replay.json()['execution']['epoch'], epoch)
                self.assertEqual(client.get('/api/execution').json()['execution']['epoch'], epoch)
                self.assertEqual(h.f.calls, [])
