"""A1 acceptance through authenticated HTTP and authoritative response persistence."""

from contextlib import closing, nullcontext
import json
from hashlib import sha256
import sqlite3
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from tests import checkpoint_acceptance_harness as harness
from tiku_agent.fastapi_demo import create_app, SESSION_COOKIE
from tiku_agent.invite_access import InviteAccess, build_invitation_config
from tiku_shared.model_costs import SQLiteModelCostLedger, timed_model_call
from tiku_shared.trace_events import TERMINAL_EVENT_TYPES


VOLATILE = {"workflow_id", "workflow_search_id", "search_id", "task_id", "candidate_generation",
            "trace_id", "request_id", "response_id", "identity_key", "session_key",
            "created_at", "expires_at", "duration_ms"}
PROTOCOL = ("status", "layer", "code", "retryable", "action")


def stable(value):
    if isinstance(value, dict):
        return {key: stable(item) for key, item in value.items() if key not in VOLATILE}
    if isinstance(value, (list, tuple)):
        return [stable(item) for item in value]
    return value


class CheckpointFinalAcceptanceTest(unittest.TestCase):
    def run_workflow(self, mode):
        fixture = harness.Harness()
        fixture.setUp()
        try:
            fixture.configure(units=3, enabled=mode != "off")
            config, codes = build_invitation_config(1)
            config_path = fixture.root / "invites.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            fixture.runtime.cost_ledger = SQLiteModelCostLedger(fixture.root / "costs.sqlite3")
            verify = fixture.verifier.verify
            def compare(*args):
                return timed_model_call(lambda: verify(*args), provider="dashscope", model="qwen3.7-plus",
                    call_type="qwen_a3_crop_compare", usage_getter=lambda _: {"input_tokens": 1000, "output_tokens": 100})
            app = create_app(runtime=fixture.runtime, incoming_dir=fixture.root / "incoming",
                invite_access=InviteAccess(config_path), cleanup_interval_seconds=0,
                trace_event_recorder=fixture.traces,
                checkpoint_capture_start=fixture.recorder.start, checkpoint_capture_close=fixture.recorder.close)
            results = []
            def capture(response):
                self.assertEqual(response.status_code, 200, response.text[:200])
                payload = response.json()
                state = payload["task_state"]
                self.assertEqual(state["consistency"], {"status": "OK", "codes": []})
                self.assertTrue(state["workflow"]["workflow_id"])
                record = app.state.response_store.get(payload["response_id"])
                self.assertIsNotNone(record)
                self.assertEqual(record.request_id, payload["request_id"])
                self.assertTrue(fixture.traces.flush())
                terminals = [event for event in fixture.trace_store.events_for_trace(record.trace_id)
                             if event.event_type in TERMINAL_EVENT_TYPES]
                self.assertEqual([event.response_id for event in terminals], [record.response_id])
                self.assertEqual(record.workflow_search_id, state["workflow"]["workflow_id"])
                projection = record.to_dict()
                child = fixture.a2.store.load(client.cookies.get(SESSION_COOKIE))
                candidates = [{key: item.get(key) for key in ("rank", "name", "score", "rerank_score", "final_score")}
                              for item in child.candidates] if child else []
                media = []
                for url in payload.get("images", []):
                    image = client.get(url)
                    self.assertEqual(image.status_code, 200)
                    media.append(sha256(image.content).hexdigest())
                # Full Task State, all authoritative response fields, and user-visible result.
                results.append(stable({"task_state": state, "response": projection,
                    "text": payload.get("text"), "intent": payload.get("intent"), "candidates": candidates, "media": media,
                    "protocol": {key: payload.get(key) for key in PROTOCOL}}))
                return payload
            fault = patch.object(fixture.store, "put_checkpoint", side_effect=OSError("injected")) if mode == "failure" else nullcontext()
            with TestClient(app) as client, fault, patch.object(fixture.verifier, "verify", side_effect=compare):
                login = client.post("/api/invite/login", data={"code": codes[0][1]}, follow_redirects=False)
                self.assertEqual(login.status_code, 303)
                with fixture.business():
                    uploaded = capture(client.post("/api/image", files={"file": ("page.png", fixture.source.read_bytes(), "image/png")}))
                workflow = uploaded["task_state"]["workflow"]
                with fixture.business():
                    selected = capture(client.post("/api/a3/select", json={"unit_id": "g1-u3",
                        "workflow_id": workflow["workflow_id"], "task_revision": workflow["task_revision"]}))
                self.assertEqual([item["name"] for item in results[-1]["candidates"]], ["q1.png", "q2.png", "q3.png"])
                with patch("tiku_agent.tools.search.find_answer_files", return_value=fixture.answers):
                    answer = capture(client.post("/api/message", json={"text": "1"}))
                self.assertEqual(selected["task_state"]["current_unit"]["unit_id"], "g1-u3")
                self.assertEqual(app.state.response_store.get(answer["response_id"]).image_count, 3)
                # A stale action cannot become authorized by successful or missing evidence.
                stale_id = workflow["workflow_id"][:-1] + ("0" if workflow["workflow_id"][-1] != "0" else "1")
                stale = client.post("/api/a3/select", json={"unit_id": "g1-u1",
                    "workflow_id": stale_id, "task_revision": workflow["task_revision"]})
                self.assertEqual(stale.json()["code"], "STALE_ACTION")
                results.append({"stale_status": stale.status_code,
                    "stale_protocol": {key: stale.json().get(key) for key in PROTOCOL}})
                self.assertTrue(fixture.recorder.flush(10))
                counts = fixture.recorder.health()["counters"]
                if mode == "on":
                    self.assertGreater(counts["stored"], 0)
                    self.assertEqual(counts["failed"], 0)
                    answer_row = next(row for row in fixture.records() if row["stage"] == "answer_prepared")
                    self.assertEqual(len(answer_row["result"]["delivery"]["answer_refs"]), 3)
                    self.assertEqual(answer_row["owner"]["unit_id"], "g1-u3")
                    self.assertEqual(answer_row["owner"]["workflow_search_id"], workflow["workflow_id"])
                    self.assertEqual(answer_row["trace_id"], app.state.response_store.get(answer["response_id"]).trace_id)
                elif mode == "failure":
                    self.assertGreater(counts["failed"], 0)
                else:
                    self.assertEqual(counts["queued"], 0)
            with closing(sqlite3.connect(fixture.root / "costs.sqlite3")) as connection:
                costs = connection.execute("SELECT call_type, input_tokens, output_tokens, estimated_cost_micros, attempt_count "
                                           "FROM model_cost_calls ORDER BY call_type, input_tokens").fetchall()
            self.assertEqual(len(costs), 3)
            self.assertTrue(all(row[3] > 0 and row[4] == 1 for row in costs))
            self.assertEqual(len(fixture.verifier.calls), 3)
            results.append({"costs": costs})
            return results
        finally:
            fixture.doCleanups()

    def test_capture_toggle_and_storage_failure_preserve_results_state_responses_fees_and_actions(self):
        expected = self.run_workflow("off")
        self.assertEqual(self.run_workflow("on"), expected)
        self.assertEqual(self.run_workflow("failure"), expected)
