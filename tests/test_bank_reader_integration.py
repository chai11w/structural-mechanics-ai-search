"""Reader protection covers state persistence and channel media hand-off."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

import test_bank_readers as gates
from test_bank_versions import publish, question
import test_tiku_agent_fastapi_demo as web_fixture
from scripts.feishu_tiku_bot import BotResponse, FeishuTikuBridge
from tiku_agent.agent import AgentResponse
from tiku_agent.fastapi_demo import create_app
from tiku_agent.feedback_store import SQLiteFeedbackStore
from tiku_agent.session_artifacts import SessionArtifacts
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.task_log import TaskLogger
from tiku_agent.tools import AgentToolConfig, answer_candidate_tool
from tiku_agent import tools as agent_tools
from tiku_shared.bank_readers import maintenance_gate


class ReaderIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.gate = gates.ReaderGateTests(); self.gate.setUp()
        self.addCleanup(self.gate.doCleanups)
        temporary = tempfile.TemporaryDirectory(prefix="lida-reader-runtime-")
        self.addCleanup(temporary.cleanup)
        self.runtime_root = Path(temporary.name).resolve()
        self.first = publish(self.gate.root, "original", 1)
        self.question = question(self.first, 6)
        Image.new("RGB", (4, 4), "white").save(self.question)
        self.second = publish(self.gate.root, "deleted-question", 2)

    def test_actual_runtime_holds_reader_from_session_load_until_sqlite_save(self):
        observations = []
        check, source = self.gate.assert_maintenance_busy, self.question
        class Store(SQLiteSessionStore):
            def load(self, session_id):
                check(); observations.append("load")
                return super().load(session_id)
            def save(self, state):
                super().save(state)
                check(); observations.append("saved")
        class Logger(TaskLogger):
            def write(self, entry):
                pass
        class Agent:
            def __init__(self, state):
                self.state = state
            def handle_text(self, text):
                self.state.current_chapter = "4力法"
                self.state.set_candidates([{"rank": 1, "path": str(source), "name": source.name, "score": 1.0}])
                return AgentResponse(text="已找到候选", state=self.state.to_dict(), intent="search_loads")
        store = Store(self.runtime_root / "sessions.sqlite3")
        runtime = AgentSessionRuntime(store, artifacts=SessionArtifacts(self.runtime_root / "sessions"),
                                      task_logger=Logger(), agent_factory=Agent)
        response = runtime.handle_text("owner-session", "继续")
        self.assertEqual(response.state["candidates"][0]["path"], str(self.question))
        self.assertIn("load", observations)
        self.assertIn("saved", observations)
        with maintenance_gate(self.gate.root):
            restored = SQLiteSessionStore(store.database_path).load("owner-session")
            self.assertEqual(restored.candidates[0]["path"], str(self.question))

    def test_old_answer_pages_remain_protected_until_all_copies_finish(self):
        copied = []
        original = agent_tools.atomic_copy
        def guarded_copy(source, destination):
            self.gate.assert_maintenance_busy()
            original(source, destination)
            copied.append((source.read_bytes(), destination.read_bytes()))
        with patch.object(agent_tools, "atomic_copy", side_effect=guarded_copy):
            result = answer_candidate_tool([{"rank": 1, "path": str(self.question)}], rank=1,
                                          config=AgentToolConfig(runtime_dir=self.runtime_root))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(copied), 6)
        self.assertTrue(all(before == after for before, after in copied))
        with maintenance_gate(self.gate.root):
            self.assertEqual([Path(path).read_bytes() for path in result.data["copied_paths"]],
                             [f"answer-{index}".encode() for index in range(6)])

    def test_json_and_stream_workers_hold_gate_through_public_media_copy(self):
        observations = []
        check = self.gate.assert_maintenance_busy
        artifacts = SessionArtifacts(self.runtime_root / "web-media")
        class Runtime(web_fixture.FakeRuntime):
            def handle_text(self, session_id, text, **kwargs):
                check(); observations.append("handle")
                kwargs.pop("request_id", None)
                return super().handle_text(session_id, text, **kwargs)
            def persist_media(self, session_id, source):
                check(); observations.append("copy")
                return artifacts.persist_media(session_id, source)
            def resolve_media(self, session_id, filename):
                return artifacts.resolve_media(session_id, filename)
        runtime = Runtime(self.question)
        app = create_app(runtime=runtime, incoming_dir=self.runtime_root / "incoming",
                         feedback_store=SQLiteFeedbackStore(self.runtime_root / "feedback.sqlite3"))
        original = self.question.read_bytes()
        with TestClient(app) as client:
            first = client.post("/api/message", json={"text": "查看答案"})
            self.assertEqual(first.status_code, 200, first.text)
            second = client.post("/api/message/stream", json={"text": "查看答案"})
            self.assertEqual(second.status_code, 200, second.text)
            events = [json.loads(line) for line in second.text.splitlines() if line]
            payloads = [first.json(), events[-1]["data"]]
            urls = [url for payload in payloads for url in payload["images"]]
            self.assertEqual(len(urls), 2)
            with maintenance_gate(self.gate.root):
                directory = self.first.main.parent
                hidden = self.gate.root / "old-version-unavailable"
                self.assertIn(self.gate.root, directory.resolve().parents)
                self.assertIn(self.gate.root, hidden.resolve().parents)
                directory.rename(hidden)
            for url in urls:
                response = client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, original)
        self.assertEqual(observations, ["handle", "copy", "handle", "copy"])

    def test_feishu_managed_search_copies_response_before_releasing_gate(self):
        observed = []
        def receive(sender, text):
            self.gate.assert_maintenance_busy()
            observed.append("search")
            return BotResponse(texts=["候选"], images=[self.question])
        def save(raw):
            self.gate.assert_maintenance_busy()
            observed.append("save")
            identity = hashlib.sha256(raw).hexdigest() + ".jpg"
            (self.runtime_root / identity).write_bytes(raw)
            return identity
        bridge = object.__new__(FeishuTikuBridge)
        bridge.bot = SimpleNamespace(receive_text=receive)
        bridge.management = SimpleNamespace(_save_image=save)
        result = bridge._managed_search({"kind": "text", "text": "第一个答案", "sender": "ou_owner", "chat_id": "chat"})
        self.assertEqual(observed, ["search", "save"])
        self.assertEqual((self.runtime_root / result["items"][1]["image"]).read_bytes(), self.question.read_bytes())
        with maintenance_gate(self.gate.root):
            pass


if __name__ == "__main__":
    unittest.main()
