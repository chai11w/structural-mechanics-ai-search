import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT=Path(__file__).resolve().parents[1]


class ExecutionFrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"),"Node.js required")
    def test_headers_keep_operation_identity_and_reject_invalid_context(self):
        source=(ROOT/"tiku_agent/demo_web/demo.js").read_text(encoding="utf-8")
        helper=source.split("// Phase 5 metadata",1)[1].split("function consumeTaskStateResponse",1)[0]
        # Run the actual transport helpers, with only existing UI predicates stubbed.
        script="// Phase 5 metadata"+helper+"""
const assert = require('node:assert/strict');
function isTaskStateRequestPath(url) { return url.startsWith('/api/'); }
function staleSessionActionError() { return new Error('stale'); }
let headers = new Headers();
applyExecutionHeaders(headers, {id:'legacy'}, '/api/message');
assert.equal(headers.get('X-Tiku-Operation'), null);
acceptExecutionContext({execution:{schema:1,epoch:'a'.repeat(32),state_version:4}});
const fence = {id:'same-operation-123'};
applyExecutionHeaders(headers, fence, '/api/message');
const first = headers.get('X-Tiku-Operation');
assert.deepEqual(JSON.parse(first), {key:fence.id,epoch:'a'.repeat(32),state_version:4});
acceptExecutionContext({execution:{schema:1,epoch:'a'.repeat(32),state_version:9}});
applyExecutionHeaders(headers, fence, '/api/message/stream');
assert.equal(headers.get('X-Tiku-Operation'), first);
applyExecutionHeaders(headers, {id:'new-operation-456'}, '/api/image/stream');
assert.equal(JSON.parse(headers.get('X-Tiku-Operation')).state_version,9);
headers = new Headers();
applyExecutionHeaders(headers, fence, '/api/session');
assert.equal(headers.get('X-Tiku-Operation'), null);
acceptExecutionContext({execution:{schema:1,epoch:'bad',state_version:-1}});
assert.throws(()=>applyExecutionHeaders(headers,{id:'x'},'/api/reset'),/stale/);
acceptExecutionContext({execution:{schema:1,epoch:'b'.repeat(32),state_version:0}});
applyExecutionHeaders(headers,{id:'reset-operation'},'/api/reset');
assert.equal(JSON.parse(headers.get('X-Tiku-Operation')).epoch,'b'.repeat(32));
"""
        result=subprocess.run([shutil.which("node"),"-"],input=script,text=True,capture_output=True,encoding="utf-8",timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_isolated_launcher_rejects_production_runtime(self):
        from scripts.run_tiku_agent_phase5 import build_app
        with self.assertRaises(ValueError):
            build_app(ROOT/".tmp_tiku_agent_v2_prod_8790")


if __name__=="__main__":
    unittest.main()
