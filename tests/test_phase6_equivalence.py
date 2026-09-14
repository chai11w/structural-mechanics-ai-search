"""F18: same real A2/A3 core, inputs and counted adapters through both schedulers."""
from contextlib import closing
import sqlite3
import unittest

from tests import test_execution_dispatch_a3 as fixtures
from tests.test_a3_runtime import FakeAutoCropper
from tiku_agent.tools import ToolResult


class Phase6EquivalenceTests(unittest.TestCase):
    def fixture(self, detached):
        fixture = fixtures.ExecutionDispatchA3Tests()
        fixture.detached = detached
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def run_pair(self, *, automatic):
        old, new = self.fixture(False), self.fixture(True)
        answer = old.root / "answer.png"
        answer.write_bytes(old.image)
        candidates = [{"rank": 1, "path": str(answer), "name": "answer.png", "score": .9}]
        for fixture in (old, new):
            fixture.tools.answer_candidate = lambda *args, **kwargs: ToolResult(ok=True, data={"copied_paths": [str(answer)]})
            fixture.tools.coarse_search = lambda *args, **kwargs: ToolResult(ok=True, data={"candidates": candidates})
            fixture.tools.rerank_candidates = lambda *args, **kwargs: ToolResult(ok=True, data={"reranked": False, "visible_candidates": candidates})
        if automatic:
            for fixture in (old, new):
                fixture.a3.auto_cropper = FakeAutoCropper(second_status="auto_ready")

        def command(kind, values=None):
            replies = []
            for fixture in (old, new):
                parameters = dict(values or {})
                if kind in {"select_unit", "handle_crop", "prepare_units"}:
                    parameters.update(fixture.target())
                if fixture.detached:
                    kwargs = {"image": fixture.image} if kind == "handle_image" else {}
                    reply = fixture.run_command(kind, parameters, **kwargs)["result"]
                else:
                    if kind == "handle_image":
                        image = fixture.root / "source.png"
                        image.write_bytes(fixture.image)
                        parameters["image_path"] = image
                    reply = getattr(fixture.a3, kind)("s", **parameters, identity_key="invite", operation_request=fixture.request())
                protocol = {key: value for key, value in reply.protocol.items() if key not in {"request_id", "search_id"}}
                replies.append((reply.text, reply.intent, reply.images, protocol))
            self.assertEqual(replies[0], replies[1], kind)
            self.assertCountEqual(old.calls, new.calls)
            self.assertEqual(old.a3.store.load("s").phase, new.a3.store.load("s").phase)

        command("handle_image")
        command("handle_text", {"text": "你好"})
        if automatic:
            command("prepare_units", {"unit_ids": ["g1-u1", "g1-u2"]})
        command("select_unit", {"unit_id": "g1-u1"})
        if not automatic:
            command("handle_crop", {"unit_id": "g1-u1", "bounds": {"x": .1, "y": .1, "width": .7, "height": .7}})
        # A2 chapter selection, ranking and answer selection after the A3 handoff.
        command("handle_text", {"text": "4力法"})
        command("handle_text", {"text": "1"})
        states = []
        costs = []
        for fixture in (old, new):
            state = fixture.a2.store.load("s").to_dict()
            states.append({key: state.get(key) for key in ("phase", "current_chapter", "selected_rank", "candidates", "last_answer_paths")})
            with closing(sqlite3.connect(fixture.ledger.path)) as conn:
                costs.append(conn.execute("SELECT provider,model,call_type,total_tokens,estimated_cost_micros FROM model_cost_calls ORDER BY rowid").fetchall())
        self.assertEqual(states[0], states[1])
        self.assertEqual(states[0]["current_chapter"], "4力法")
        self.assertEqual(states[0]["selected_rank"], 1)
        self.assertEqual(states[0]["last_answer_paths"], [str(answer)])
        self.assertCountEqual(costs[0], costs[1])
        self.assertEqual(len(costs[0]), 4 if automatic else 3)

    def test_manual_crop_child_chapter_ranking_and_answer_match(self):
        self.run_pair(automatic=False)

    def test_batch_prepare_child_chapter_ranking_and_answer_match(self):
        self.run_pair(automatic=True)
