import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT=Path(__file__).resolve().parents[1]


class ExecutionFrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_finished_page_and_replay_have_browser_compatible_public_projection(self):
        from tests import test_execution_handoffs as fixtures
        fixture = fixtures.ExecutionHandoffTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepare()
        parent = fixture.a3.store.load("s")
        with fixtures.TestClient(fixtures.create_app(runtime=fixture.a3, incoming_dir=fixture.root/"incoming")) as client:
            client.cookies.set(fixtures.SESSION_COOKIE, "s")
            headers = fixture.http_headers()
            payload = {"scope":"workflow", "target":{"workflow_id":parent.workflow_search_id or parent.current_search_id,
                "task_revision":parent.task_revision}}
            responses = [client.post("/api/execution/control", json=payload, headers=headers).json(),
                client.post("/api/execution/control", json=payload, headers=headers).json(),
                client.get("/api/execution").json()]
        script = r"""
const assert = require('node:assert/strict');
const source = require('node:fs').readFileSync('./tiku_agent/demo_web/demo.js','utf8');
const taskStateV1 = require('./tiku_agent/demo_web/task_state.js');
function block(start,end){return source.split(start)[1].split(end)[0];}
eval('function currentWorkflowActionTarget'+block('function currentWorkflowActionTarget','function taskStateAllowsWorkflowAction'));
eval('function a3SnapshotMatchesTaskState'+block('function a3SnapshotMatchesTaskState','function a3SnapshotIsIdleCapability'));
eval('function normalizeA3Snapshot'+block('function normalizeA3Snapshot','function openLightbox'));
let taskStateContext;
for (const envelope of RESPONSES) {
 taskStateContext = taskStateV1.createTaskStateModelFromEnvelope(envelope);
 assert.equal(taskStateContext.consistent,true);
 assert.equal(taskStateContext.snapshot.workflow.phase,'COMPLETE');
 const a3=normalizeA3Snapshot(envelope.session.a3);
 assert.equal(a3SnapshotMatchesTaskState(a3,currentWorkflowActionTarget()),true);
 a3.page_finished=false;
 assert.equal(a3SnapshotMatchesTaskState(a3,currentWorkflowActionTarget()),false);
}
"""
        result = subprocess.run([shutil.which("node"), "-"], cwd=ROOT,
            input="const RESPONSES="+json.dumps(responses)+";\n"+script,
            text=True, capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js required")
    def test_control_transport_persists_retries_and_never_replays_business(self):
        from tiku_agent.task_state_public import empty_public_task_state_snapshot
        script = r"""
const assert = require('node:assert/strict');
const api = require('./tiku_agent/demo_web/execution_control.js');
const taskStateV1 = require('./tiku_agent/demo_web/task_state.js');
const base = {task_state: EMPTY, execution:{schema:1,epoch:'a'.repeat(32),state_version:4},
  execution_control:{schema:1,epoch:'a'.repeat(32),state_version:4,controls:[{action:'reset_session',target:{}}],pending:[],has_more:false}};
const good = structuredClone(base);
assert.equal(api.parseView(good, taskStateV1).controls.length, 1);
const betweenParentAndChild = structuredClone(base);
betweenParentAndChild.task_state = {schema_version:1,
 workflow:{exists:true,workflow_id:'search_inflight_parent_12345678',kind:'IMAGE_SEARCH',route:'A3',task_revision:1,
   phase:'A2_ACTIVE',status:'INCONSISTENT',completed_steps:[],allowed_actions:[],next_stage:'RETRY'},
 active_child_task:null,current_unit:null,units:[],consistency:{status:'INCONSISTENT',codes:['ACTIVE_CHILD_TASK_MISSING']}};
assert.equal(taskStateV1.createTaskStateModelFromEnvelope(betweenParentAndChild).consistent,false);
assert.equal(api.parseView(betweenParentAndChild,taskStateV1).controls[0].action,'reset_session');
assert.equal(taskStateV1.allowsWorkflowAction(taskStateV1.createTaskStateModelFromEnvelope(betweenParentAndChild),'select_unit'),false);
for (const mutate of [v=>v.execution.state_version++, v=>v.execution_control.controls.push({action:'handle_image',target:{}}),
    v=>v.execution_control.state_version=true, v=>v.task_state=null,
    v=>v.execution_control.controls.push({action:'recover_operation',target:{source_operation_id:'b'.repeat(32)}})]) {
  const bad = structuredClone(base); mutate(bad); assert.throws(()=>api.parseView(bad,taskStateV1));
}
let stored = new Map(), calls = [], mode = 'lost', sequence = 0, commits = [], cleared = [], retires = 0;
const storage = {getItem:key=>stored.get(key)??null, setItem:(key,value)=>stored.set(key,value), removeItem:key=>stored.delete(key)};
const host = {taskStateV1,storage, locks:{request:async (name,options,fn)=>{assert.equal(name,'tiku-agent-execution-command-v1');return fn({});}},
 createFence:()=>({id:'command-'+(++sequence),records:[],ownRecord:{}}), validFence:f=>f?.id?.startsWith('command-'),
 headers:()=>new Headers(), acknowledged:()=>true, retire:()=>retires++, publish:()=>{},
 clearFence:(f,own)=>cleared.push({id:f.id,own}), committed:(result,current,action)=>commits.push({result,current,action}),
 fetch:async(path,options)=>{
   calls.push({path,body:options.body,operation:options.headers?.get('X-Tiku-Operation')});
   assert.equal(options.cache,'no-store');
   if (path === '/api/execution') return new Response(JSON.stringify(base),{headers:{'content-type':'application/json'}});
   assert.equal(path,'/api/reset');
   if(mode==='lost') throw new TypeError('response lost');
   if(mode==='stale') return new Response(JSON.stringify({code:'EXECUTION_STALE'}),{status:409,headers:{'content-type':'application/json'}});
   return new Response(JSON.stringify(good),{headers:{'content-type':'application/json'}});
 }};
(async()=>{
 let client=api.createClient(host);
 const first=await client.inspect();
 assert.equal(sequence,0); assert.equal(retires,0);
 await assert.rejects(client.execute(structuredClone(first.view),0)); // unbranded view
 await assert.rejects(client.execute(first.view,0),/连接中断/);
 assert.equal(client.hasPending(),true); assert.equal(cleared.length,0);
 const original=calls.find(c=>c.path==='/api/reset');
 client=api.createClient(host); // refresh loses all in-memory state
 const reloaded=await client.inspect();
 await assert.rejects(client.execute(reloaded.view,0),/上次控制/);
 assert.equal(sequence,1);
 mode='ok'; base.execution.state_version=8; base.execution_control.state_version=8;
 await client.retry();
 const replay=calls.filter(c=>c.path==='/api/reset')[1];
 assert.equal(replay.operation,original.operation); assert.equal(replay.body,original.body);
 assert.equal(client.hasPending(),false);
 assert.equal(commits[0].current.execution.state_version,8); // historical receipt never authorizes live actions
 assert.equal(commits[0].result.execution.state_version,4);
 assert.equal(cleared.length,1); assert.equal(cleared[0].own,false);
 const next=await client.inspect(); mode='stale';
 await assert.rejects(client.execute(next.view,0),/任务已更新/);
 assert.equal(client.hasPending(),false); assert.equal(cleared[1].own,true);
 assert.equal(commits.length,1);
 host.locks.request=async(name,options,fn)=>fn(null);
 const before=calls.length; await assert.rejects(client.retry(),/另一页面/); assert.equal(calls.length,before);
 stored.set(api.JOURNAL_KEY,'{broken'); assert.throws(()=>client.hasPending(),/记录损坏/);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run([shutil.which("node"), "-"], cwd=ROOT,
            input="const EMPTY="+json.dumps(empty_public_task_state_snapshot())+";\n"+script,
            text=True, capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

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
