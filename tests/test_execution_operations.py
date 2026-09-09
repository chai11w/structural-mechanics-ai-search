from contextlib import closing
import json
import multiprocessing
import io
import time
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from tiku_agent.agent import AgentResponse, TikuSearchAgent
from tiku_agent.execution_operations import OperationRequest, OperationStore
from tiku_agent.execution_runtime import OPERATION_HEADER, attach_execution
from tiku_agent.execution_store import ExecutionError, ExecutionPolicy, ExecutionSessionStore, ExecutionStore, _WRITER
from tiku_agent.fastapi_demo import create_app
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentProtocolError, AgentSessionRuntime
from tiku_agent.state import AgentState
from tests.test_tiku_agent_session_runtime import FakeTools


def _claim_process(path, operation_id, ready, start, result):
    store = OperationStore(ExecutionStore(path))
    ready.put(True)
    start.wait(10)
    try:
        writer = store.claim(operation_id)
        result.put(("winner",writer.attempt_id))
    except ExecutionError as exc:
        result.put((exc.code,""))


class ExecutionOperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.authority = ExecutionStore(self.root/"execution.db")
        self.ops = OperationStore(self.authority)
        self.calls = []
        calls = self.calls
        class FakeAgent:
            def __init__(self,state): self.state=state; self.config=None
            def handle_text(self,text):
                calls.append(text)
                self.state.last_intent={"raw":"must not enter receipt"}
                return AgentResponse(text="reply:"+text,state=self.state.to_dict(),intent="greeting")
        self.runtime = AgentSessionRuntime(ExecutionSessionStore(self.authority),
            artifacts=SessionArtifacts(self.root/"media"),agent_factory=FakeAgent)
        attach_execution(self.runtime,self.authority,configuration_version="operations-fixture-v1")

    def req(self,sid="s",key=None):
        context=self.authority.context(sid)
        return OperationRequest(key or uuid4().hex,context["epoch"],context["state_version"])

    def assertCode(self,code,fn):
        with self.assertRaises((ExecutionError,AgentProtocolError)) as caught: fn()
        self.assertEqual(caught.exception.code,code)

    def test_runtime_replays_result_without_agent_or_new_attempt(self):
        req=self.req()
        first=self.runtime.handle_text("s","hello",operation_request=req)
        second=self.runtime.handle_text("s","hello",operation_request=req)
        self.assertEqual(first.text,second.text)
        self.assertEqual(self.calls,["hello"])
        self.assertTrue(second.execution_receipt["replayed"])
        with self.authority.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM execution_attempts").fetchone()[0],1)
            self.assertNotIn("must not enter receipt",conn.execute("SELECT result FROM execution_operations").fetchone()[0])

    def test_same_key_different_text_target_or_state_version_conflicts(self):
        req=self.req()
        self.runtime.handle_text("s","hello",operation_request=req)
        self.assertCode("EXECUTION_INPUT_CONFLICT",lambda:self.runtime.handle_text("s","changed",operation_request=req))
        changed=OperationRequest(req.key,req.epoch,req.state_version+1)
        self.assertCode("EXECUTION_INPUT_CONFLICT",lambda:self.runtime.handle_text("s","hello",operation_request=changed))
        self.assertEqual(len(self.calls),1)

    def test_typed_request_is_validated_and_execution_deadline_fences_finish(self):
        self.assertCode("EXECUTION_CONTEXT_REQUIRED", lambda: OperationRequest.parse(OperationRequest("bad", "bad", True)))
        clock = [1000.0]
        authority = ExecutionStore(self.root / "deadline.db", now=lambda: clock[0],
                                   policy=ExecutionPolicy(lease_seconds=300, max_execution_seconds=10))
        ops = OperationStore(authority)
        ctx = authority.context("s")
        req = OperationRequest(uuid4().hex, ctx["epoch"], ctx["state_version"])
        writer = ops.claim(ops.register("s", "local", req, "test", {})["id"])
        clock[0] += 11
        self.assertCode("EXECUTION_LEASE_LOST", lambda: ops.finish(writer, {"schema": 1, "response": None}))
        self.assertCode("EXECUTION_LEASE_LOST", lambda: ops.renew(writer))

    def test_operation_status_is_scoped_read_only_and_contains_no_secrets(self):
        with TestClient(create_app(runtime=self.runtime,incoming_dir=self.root/"incoming")) as client:
            ctx = client.get("/api/session").json()["execution"]
            req = {"key": uuid4().hex, "epoch": ctx["epoch"], "state_version": ctx["state_version"]}
            response = client.post("/api/message", json={"text": "hello"}, headers={OPERATION_HEADER: json.dumps(req)})
            self.assertEqual(response.status_code, 200)
            for _ in range(2):
                observed = client.get("/api/operation", params={"key": req["key"], "epoch": req["epoch"]})
                self.assertEqual(observed.status_code, 200, observed.text)
                op = observed.json()["operation"]
                self.assertEqual(set(op), {"operation_id", "kind", "status", "attempts", "effects", "accounting"})
                self.assertEqual(op["status"], "SUCCEEDED")
                self.assertEqual(set(op["attempts"][0]), {"attempt_id", "status"})
            self.assertEqual(self.calls, ["hello"])
            with TestClient(create_app(runtime=self.runtime,incoming_dir=self.root/"other")) as other:
                other.get("/api/session")
                self.assertEqual(other.get("/api/operation", params={"key": req["key"], "epoch": req["epoch"]}).status_code, 404)

    def test_missing_context_and_old_new_operation_fail_before_agent(self):
        req=self.req()
        self.assertCode("EXECUTION_CONTEXT_REQUIRED",lambda:self.runtime.handle_text("s","hello"))
        self.runtime.handle_text("s","hello",operation_request=req)
        old=OperationRequest(uuid4().hex,req.epoch,req.state_version)
        self.assertCode("EXECUTION_STALE",lambda:self.runtime.handle_text("s","new",operation_request=old))
        self.assertEqual(len(self.calls),1)

    def test_explicit_new_operation_is_not_content_deduplicated(self):
        self.runtime.handle_text("s","hello",operation_request=self.req())
        self.runtime.handle_text("s","hello",operation_request=self.req())
        self.assertEqual(self.calls,["hello","hello"])
        with self.authority.transaction() as conn:
            rows=conn.execute("SELECT id,previous_operation_id FROM execution_operations ORDER BY rowid").fetchall()
            self.assertEqual(rows[1]["previous_operation_id"], rows[0]["id"])

    def test_registered_before_crash_can_be_claimed_after_restart(self):
        req=self.req()
        row=self.ops.register("s","local",req,"handle_text",{"text":"hello"})
        restarted=OperationStore(ExecutionStore(self.authority.path))
        same=restarted.register("s","local",req,"handle_text",{"text":"hello"})
        self.assertEqual(row["id"],same["id"])
        writer=restarted.claim(row["id"])
        self.assertTrue(writer.attempt_id)

    def test_two_actual_processes_can_only_claim_one_attempt(self):
        row=self.ops.register("s","local",self.req(),"handle_text",{"text":"hello"})
        context=multiprocessing.get_context("spawn")
        ready,start,result=context.Queue(),context.Event(),context.Queue()
        workers=[context.Process(target=_claim_process,args=(str(self.authority.path),row["id"],ready,start,result)) for _ in range(2)]
        try:
            for worker in workers: worker.start()
            for _ in workers: self.assertTrue(ready.get(timeout=20))
            start.set()
            statuses=[result.get(timeout=20)[0] for _ in workers]
            self.assertCountEqual(statuses,["winner","EXECUTION_BUSY"])
        finally:
            start.set()
            for worker in workers:
                worker.join(timeout=20)
                if worker.is_alive(): worker.terminate(); worker.join()
            for q in (ready,result): q.close(); q.join_thread()

    def test_lease_expiry_becomes_unknown_and_old_writer_cannot_save(self):
        now=[1800000000.0]
        store=ExecutionStore(self.root/"clock.db",now=lambda:now[0],policy=ExecutionPolicy(lease_seconds=1))
        ops=OperationStore(store)
        ctx=store.context("x")
        req=OperationRequest(uuid4().hex,ctx["epoch"],ctx["state_version"])
        row=ops.register("x","local",req,"handle_text",{})
        writer=ops.claim(row["id"])
        now[0]+=2
        self.assertCode("EXECUTION_UNKNOWN",lambda:ops.claim(row["id"]))
        token=_WRITER.set(writer)
        try:
            self.assertCode("EXECUTION_LEASE_LOST",lambda:ExecutionSessionStore(store).save(AgentState(session_id="x")))
        finally: _WRITER.reset(token)
        self.assertEqual(ops.lookup("x","local",req)["status"],"UNKNOWN")

    def test_unknown_exception_is_not_reexecuted(self):
        req=self.req()
        with patch.object(self.runtime,"_make_agent",side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError): self.runtime.handle_text("s","hello",operation_request=req)
        self.assertCode("EXECUTION_UNKNOWN",lambda:self.runtime.handle_text("s","hello",operation_request=req))
        self.assertEqual(self.calls,[])

    def test_identity_and_session_cannot_reuse_receipt(self):
        req=self.req()
        self.runtime.handle_text("s","hello",identity_key="one",operation_request=req)
        self.assertCode("EXECUTION_STALE",lambda:self.runtime.handle_text("s","hello",identity_key="two",operation_request=req))
        self.assertCode("EXECUTION_STALE",lambda:self.runtime.handle_text("other","hello",identity_key="one",operation_request=req))
        self.assertEqual(len(self.calls),1)

    def test_reset_replay_cannot_clear_a_new_task(self):
        self.runtime.handle_text("s","hello",operation_request=self.req())
        req=self.req()
        self.runtime.clear("s",operation_request=req)
        self.runtime.handle_text("s","new",operation_request=self.req())
        self.runtime.clear("s",operation_request=req)
        self.assertIsNotNone(self.runtime.store.load("s"))
        self.assertEqual(self.calls,["hello","new"])

    def test_capacity_rejects_before_business(self):
        self.authority.policy=ExecutionPolicy(max_operations=1)
        self.runtime.handle_text("s","hello",operation_request=self.req())
        self.assertCode("EXECUTION_CAPACITY",lambda:self.runtime.handle_text("s","new",operation_request=self.req()))
        self.assertEqual(len(self.calls),1)

    def test_http_json_and_stream_share_durable_operation(self):
        app=create_app(runtime=self.runtime,incoming_dir=self.root/"incoming")
        with TestClient(app) as client:
            initial=client.get("/api/session")
            self.assertEqual(initial.status_code,200)
            context=initial.json()["execution"]
            request={"key":uuid4().hex,"epoch":context["epoch"],"state_version":context["state_version"]}
            headers={OPERATION_HEADER:json.dumps(request)}
            first=client.post("/api/message",json={"text":"hello"},headers=headers)
            self.assertEqual(first.status_code,200,first.text)
            second=client.post("/api/message/stream",json={"text":"hello"},headers=headers)
            events=[json.loads(line) for line in second.text.splitlines()]
            result=next(e["data"] for e in events if e["type"]=="result")
            self.assertEqual(self.calls,["hello"])
            self.assertTrue(result["operation"]["replayed"])
            self.assertEqual(result["operation"]["operation_id"],first.json()["operation"]["operation_id"])

    def test_actual_http_v6_replay_reset_and_missing_key(self):
        app=create_app(runtime=self.runtime,incoming_dir=self.root/"incoming")
        with TestClient(app) as client:
            context=client.get("/api/session").json()["execution"]
            req={"key":uuid4().hex,"epoch":context["epoch"],"state_version":context["state_version"]}
            headers={OPERATION_HEADER:json.dumps(req),"Sec-Fetch-Site":"same-origin",
                     "X-Session-Coordination-Version":"6","X-Session-Request-Fence":f"{int(time.time()*1000)}:{uuid4().hex}"}
            first=client.post("/api/message",json={"text":"hello"},headers=headers)
            replay=client.post("/api/message",json={"text":"hello"},headers=headers)
            self.assertEqual(first.status_code,200,first.text)
            self.assertTrue(replay.json()["operation"]["replayed"],replay.text)
            self.assertEqual(len(self.calls),1)
            missing=client.post("/api/message",json={"text":"bad"})
            self.assertEqual(missing.status_code,409)
            context=client.get("/api/session").json()["execution"]
            req={"key":uuid4().hex,"epoch":context["epoch"],"state_version":context["state_version"]}
            headers[OPERATION_HEADER]=json.dumps(req)
            headers["X-Session-Request-Fence"]=f"{int(time.time()*1000)}:{uuid4().hex}"
            reset=client.post("/api/reset",headers=headers)
            self.assertEqual(reset.status_code,200,reset.text)
            self.assertNotEqual(reset.json()["execution"]["epoch"],context["epoch"])
            repeat=client.post("/api/reset",headers=headers)
            self.assertEqual(repeat.status_code,200,repeat.text)
            self.assertEqual(repeat.json()["execution"],reset.json()["execution"])

    def test_revoked_authentication_blocks_cached_response(self):
        from tiku_agent.invite_access import InviteIdentity
        class Access:
            cookie_name="test_invite"
            enabled=True
            def verify_cookie(self,value): return InviteIdentity("test-identity") if self.enabled else None
        access=Access()
        app=create_app(runtime=self.runtime,incoming_dir=self.root/"incoming",invite_access=access)
        with TestClient(app) as client:
            context=client.get("/api/session").json()["execution"]
            request={"key":uuid4().hex,"epoch":context["epoch"],"state_version":context["state_version"]}
            headers={OPERATION_HEADER:json.dumps(request)}
            self.assertEqual(client.post("/api/message",json={"text":"hello"},headers=headers).status_code,200)
            access.enabled=False
            self.assertEqual(client.post("/api/message",json={"text":"hello"},headers=headers).status_code,401)
            self.assertEqual(len(self.calls),1)

    def test_new_authority_instance_cannot_write_without_execution_token(self):
        other=ExecutionStore(self.authority.path)
        self.assertCode("EXECUTION_CONTEXT_REQUIRED",lambda:ExecutionSessionStore(other).save(AgentState(session_id="s")))
        bypass=AgentSessionRuntime(ExecutionSessionStore(other),artifacts=SessionArtifacts(self.root/"other"))
        self.assertCode("EXECUTION_CONTEXT_REQUIRED",lambda:bypass.handle_text("s","hello"))

    def test_store_opened_before_enable_cannot_bypass_persisted_writer_gate(self):
        path = self.root / "preopened.db"
        older = ExecutionStore(path)
        enabled = ExecutionStore(path)
        runtime = AgentSessionRuntime(ExecutionSessionStore(enabled), artifacts=SessionArtifacts(self.root / "preopened-media"))
        attach_execution(runtime, enabled)
        self.assertCode("EXECUTION_CONTEXT_REQUIRED", lambda: ExecutionSessionStore(older).save(AgentState(session_id="new")))

    def test_result_does_not_cross_changed_producer_and_cost_run_is_bound(self):
        req=self.req()
        self.runtime.handle_text("s","hello",operation_request=req)
        with self.authority.transaction() as conn:
            link=conn.execute("SELECT * FROM execution_cost_runs").fetchone()
            self.assertIsNotNone(link)
            self.assertEqual(conn.execute("SELECT operation_id FROM execution_attempts WHERE id=?",(link["attempt_id"],)).fetchone()[0],link["operation_id"])
        self.runtime.execution_operations.producer="changed-release"
        self.assertCode("EXECUTION_RESULT_UNAVAILABLE",lambda:self.runtime.handle_text("s","hello",operation_request=req))
        self.assertEqual(len(self.calls),1)

    def test_cleanup_cannot_resurrect_old_key_or_remove_unknown(self):
        now=[1800000000.0]
        store=ExecutionStore(self.root/"retention.db",now=lambda:now[0],policy=ExecutionPolicy(history_ttl=2,session_ttl=2,lease_seconds=1))
        ops=OperationStore(store)
        ctx=store.context("x")
        req=OperationRequest(uuid4().hex,ctx["epoch"],ctx["state_version"])
        row=ops.register("x","local",req,"handle_text",{})
        writer=ops.claim(row["id"])
        ops.finish(writer,{"schema":1,"response":None})
        now[0]+=5
        self.assertEqual(ops.maintain()["operations_removed"],1)
        store.context("x")
        self.assertCode("EXECUTION_STALE",lambda:ops.register("x","local",req,"handle_text",{}))
        ctx=store.context("y")
        req=OperationRequest(uuid4().hex,ctx["epoch"],ctx["state_version"])
        row=ops.register("y","local",req,"handle_text",{})
        ops.claim(row["id"])
        now[0]+=5
        ops.maintain()
        self.assertEqual(ops.lookup("y","local",req)["status"],"UNKNOWN")

    def test_a3_http_upload_select_crop_answer_and_prepare_replays(self):
        from PIL import Image
        from tiku_agent.a3_runtime import A3MvpRuntime
        from tiku_agent.tools import ToolResult
        from tests.test_a3_runtime import FakeObserver,FakeVerifier,FakeAutoCropper
        image=self.root/"page.jpg"
        Image.new("RGB",(1000,800),"white").save(image)
        candidate=self.root/"candidate.jpg"; Image.new("RGB",(100,100),"white").save(candidate)
        tools=FakeTools().toolbox()
        counts={"analysis":0,"answer":0}
        def analyze(*a,**kw):
            counts["analysis"]+=1
            return ToolResult(ok=True,data={"loads":[{"type":"集中","raw":"P"}],"chapter_hint":"4力法"})
        tools.analyze_image=analyze
        candidates=[{"rank":1,"path":str(candidate),"name":"candidate.jpg","score":0.9}]
        tools.coarse_search=lambda *a,**kw:ToolResult(ok=True,data={"candidates":candidates})
        tools.rerank_candidates=lambda *a,**kw:ToolResult(ok=True,data={"visible_candidates":candidates,"reranked":False})
        def answer(*a,**kw):
            counts["answer"]+=1
            return ToolResult(ok=True,data={"copied_paths":[str(candidate)]})
        tools.answer_candidate=answer
        a2=AgentSessionRuntime(ExecutionSessionStore(self.authority),artifacts=SessionArtifacts(self.root/"a2"),
            agent_factory=lambda state:TikuSearchAgent(state=state,tools=tools,use_llm_intent=False))
        verifier=FakeVerifier(); observer=FakeObserver()
        a3=A3MvpRuntime(store=ExecutionSessionStore(self.authority,"workflow"),artifacts=SessionArtifacts(self.root/"a3"),
            a2_runtime=a2,page_observer=observer,crop_verifier=verifier)
        attach_execution(a3,self.authority,configuration_version="operations-a3-fixture-v1")
        app=create_app(runtime=a3,incoming_dir=self.root/"incoming")
        with TestClient(app) as client:
            def headers():
                ctx=client.get("/api/session").json()["execution"]
                return {OPERATION_HEADER:json.dumps({"key":uuid4().hex,"epoch":ctx["epoch"],"state_version":ctx["state_version"]})}
            def result(response):
                self.assertEqual(response.status_code,200,response.text)
                if response.headers.get("content-type","").startswith("application/json"): return response.json()
                events=[json.loads(line) for line in response.text.splitlines()]
                results=[event["data"] for event in events if event["type"]=="result"]
                self.assertTrue(results,response.text)
                return results[-1]
            def conflict(response):
                if response.headers.get("content-type", "").startswith("application/json"):
                    self.assertEqual(response.status_code, 409, response.text)
                    return response.json()
                self.assertEqual(response.status_code, 200, response.text)
                events=[json.loads(line) for line in response.text.splitlines()]
                errors=[event for event in events if event["type"] == "error"]
                self.assertEqual(len(errors), 1, response.text)
                return errors[0]
            h=headers(); h.update({"Content-Type":"image/jpeg","X-Filename":"page.jpg"})
            page=result(client.post("/api/image",content=image.read_bytes(),headers=h))
            repeated=result(client.post("/api/image/stream",content=image.read_bytes(),headers=h))
            self.assertTrue(repeated["operation"]["replayed"])
            self.assertEqual(observer.calls,1)
            changed = conflict(client.post("/api/image", content=candidate.read_bytes(), headers=h))
            self.assertEqual(changed["code"], "EXECUTION_INPUT_CONFLICT")
            self.assertEqual(observer.calls,1)
            workflow=page["task_state"]["workflow"]
            selection={"workflow_id":workflow["workflow_id"],"task_revision":workflow["task_revision"],"unit_id":"g1-u1"}
            h=headers()
            result(client.post("/api/a3/select",json=selection,headers=h))
            repeated=result(client.post("/api/a3/select/stream",json=selection,headers=h))
            self.assertTrue(repeated["operation"]["replayed"])
            changed = conflict(client.post("/api/a3/select", json={**selection,"unit_id":"g1-u2"}, headers=h))
            self.assertEqual(changed["code"], "EXECUTION_INPUT_CONFLICT")
            h=headers(); crop={**selection,"bounds":{"x":0,"y":0,"width":0.5,"height":1}}
            found=result(client.post("/api/a3/crop/stream",json=crop,headers=h))
            repeated=result(client.post("/api/a3/crop/stream",json=crop,headers=h))
            self.assertTrue(repeated["operation"]["replayed"])
            self.assertEqual(counts["analysis"],1)
            self.assertEqual(verifier.calls,["g1-u1"])
            changed = conflict(client.post("/api/a3/crop/stream", json={**crop,"bounds":{"x":0.1,"y":0,"width":0.5,"height":1}}, headers=h))
            self.assertEqual(changed["code"], "EXECUTION_INPUT_CONFLICT")
            self.assertEqual(counts["analysis"],1)
            if found["task_state"]["active_child_task"]["phase"] == "WAIT_CHAPTER":
                found=result(client.post("/api/message",json={"text":"力法"},headers=headers()))
            child=found["task_state"]["active_child_task"]
            action={"type":"select_candidate","task_id":child["task_id"],"task_revision":child["task_revision"],"candidate_generation":child["candidate_generation"],"rank":1}
            h=headers(); body={"text":"选择候选 1","action_context":action}
            with patch.object(a3, "persist_media", return_value=None):
                answered=result(client.post("/api/message",json=body,headers=h))
                repeat=result(client.post("/api/message/stream",json=body,headers=h))
            self.assertEqual(answered["code"], "MEDIA_ANSWERS_UNAVAILABLE")
            self.assertEqual(answered["execution"], repeat["execution"])
            self.assertEqual(answered["task_state"], repeat["task_state"])
            self.assertEqual(answered["task_state"]["workflow"]["phase"], "A2_ACTIVE")
            changed=conflict(client.post("/api/message", json={**body,"action_context":{**action,"candidate_generation":"changed"}}, headers=h))
            self.assertEqual(changed["code"], "EXECUTION_INPUT_CONFLICT")
            self.assertTrue(repeat["operation"]["replayed"])
            self.assertEqual(counts["answer"],1,json.dumps({"child":child,"answer_code":answered.get("code"),"answer_text":answered.get("text"),"answer_state":answered.get("task_state")},ensure_ascii=False))
            with self.authority.transaction() as conn:
                links=conn.execute("SELECT t.kind,t.parent_id,l.attempt_id,l.first_state_version,l.last_state_version FROM execution_task_attempts l JOIN execution_tasks t ON t.id=l.task_record_id").fetchall()
                child_links=[row for row in links if row["kind"] == "child"]
                self.assertTrue(child_links)
                for row in child_links:
                    self.assertTrue(row["parent_id"])
                    self.assertGreaterEqual(row["last_state_version"], row["first_state_version"])
                    self.assertTrue(any(p["kind"] == "workflow" and p["attempt_id"] == row["attempt_id"] for p in links))
        # A fresh page with auto crops covers prepare's own public entry.
        a3.auto_cropper=FakeAutoCropper(second_status="auto_ready")
        with TestClient(app) as client:
            ctx=client.get("/api/session").json()["execution"]
            h={OPERATION_HEADER:json.dumps({"key":uuid4().hex,"epoch":ctx["epoch"],"state_version":ctx["state_version"]}),"Content-Type":"image/jpeg","X-Filename":"page.jpg"}
            page=result(client.post("/api/image",content=image.read_bytes(),headers=h))
            ctx=client.get("/api/session").json()["execution"]; workflow=page["task_state"]["workflow"]
            h={OPERATION_HEADER:json.dumps({"key":uuid4().hex,"epoch":ctx["epoch"],"state_version":ctx["state_version"]})}
            body={"workflow_id":workflow["workflow_id"],"task_revision":workflow["task_revision"],"unit_ids":["g1-u1","g1-u2"]}
            result(client.post("/api/a3/prepare/stream",json=body,headers=h))
            count=len(verifier.calls)
            repeated=result(client.post("/api/a3/prepare/stream",json=body,headers=h))
            self.assertTrue(repeated["operation"]["replayed"])
            self.assertEqual(len(verifier.calls),count)
            changed=conflict(client.post("/api/a3/prepare/stream", json={**body,"unit_ids":["g1-u2","g1-u1"]},headers=h))
            self.assertEqual(changed["code"], "EXECUTION_INPUT_CONFLICT")
            self.assertEqual(len(verifier.calls),count)


if __name__ == "__main__":
    unittest.main()
