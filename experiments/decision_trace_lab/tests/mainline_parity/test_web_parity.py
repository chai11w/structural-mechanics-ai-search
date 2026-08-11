from __future__ import annotations

from io import BytesIO
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from mainline_mirror.integrity import activate_verified_source


activate_verified_source()

from mainline_mirror.observation.core import _response_type  # noqa: E402
from mainline_mirror.observation.storage import ObservationStore, scan_events  # noqa: E402
from mainline_mirror.observation.web import EXTERNAL_COOKIE, create_observed_app, strip_observer_markup  # noqa: E402
from tests.mainline_parity.test_agent_parity import DeterministicTools  # noqa: E402
from tiku_agent.agent import AgentResponse, TikuSearchAgent  # noqa: E402
from tiku_agent.fastapi_demo import create_app as create_mainline_app  # noqa: E402
from tiku_agent.session_artifacts import SessionArtifacts  # noqa: E402
from tiku_agent.session_runtime import AgentSessionRuntime  # noqa: E402
from tiku_agent.session_store import SQLiteSessionStore  # noqa: E402
from tiku_agent.task_log import JsonlTaskLogger  # noqa: E402
from tiku_agent.tools import AgentToolConfig  # noqa: E402


TEST_TMP = Path(__file__).resolve().parents[2] / "runtime" / "t"
TEST_TMP.mkdir(parents=True, exist_ok=True)


def runtime_at(root: Path, *, tools: DeterministicTools | None = None) -> AgentSessionRuntime:
    tools = tools or DeterministicTools()

    def factory(state):
        return TikuSearchAgent(
            state=state,
            tools=tools.toolbox(),
            config=AgentToolConfig(runtime_dir=root, session_dir=root / "sessions" / state.session_id),
            use_llm_intent=False,
        )

    return AgentSessionRuntime(
        SQLiteSessionStore(root / "session.db"),
        artifacts=SessionArtifacts(root / "sessions"),
        task_logger=JsonlTaskLogger(root / "tasks.jsonl"),
        agent_factory=factory,
    )


def png_bytes() -> bytes:
    stream = BytesIO()
    Image.new("RGB", (4, 4), "white").save(stream, format="PNG")
    return stream.getvalue()


class MainlineWebParityTest(unittest.TestCase):
    def setUp(self):
        root = TEST_TMP / uuid4().hex[:6]
        root.mkdir(parents=True, exist_ok=True)
        self.base = create_mainline_app(runtime=runtime_at(root / "b"), incoming_dir=root / "b" / "in")
        self.observed = create_observed_app(
            runtime_root=root / "o",
            store=ObservationStore(root / "data"),
            agent_factory=lambda state: TikuSearchAgent(
                state=state,
                tools=DeterministicTools().toolbox(),
                config=AgentToolConfig(runtime_dir=root / "o", session_dir=root / "o" / "s" / state.session_id),
                use_llm_intent=False,
            ),
        )
        self.base_client = TestClient(self.base)
        self.observed_client = TestClient(self.observed)

    def tearDown(self):
        self.observed.state.hook_manager.uninstall()

    def test_left_dom_assets_and_candidate_action_are_mainline_exact(self):
        baseline = self.base_client.get("/")
        observed = self.observed_client.get("/")
        self.assertEqual(baseline.status_code, observed.status_code)
        self.assertEqual(baseline.text, strip_observer_markup(observed.text))
        self.assertIn("评审本轮回答", observed.text)
        self.assertIn("回答结果", observed.text)
        self.assertIn("可能出错的步骤", observed.text)
        self.assertIn("技术详情（开发排查用）", observed.text)
        self.assertIn("observer.js?v=20260804-question-index-fix-v1", observed.text)
        for asset in ("demo.css", "demo.js"):
            self.assertEqual(self.base_client.get(f"/assets/{asset}").content, self.observed_client.get(f"/assets/{asset}").content)
        script = self.observed_client.get("/assets/demo.js").text
        self.assertIn("选择候选 ${index + 1}", script)
        self.assertNotIn("offline-injected", observed.text)

    def test_cookie_is_isolated_and_source_commit_is_provable(self):
        response = self.observed_client.get("/")
        self.assertIn(EXTERNAL_COOKIE, response.headers.get("set-cookie", ""))
        self.assertNotIn("tiku_agent_session=", response.headers.get("set-cookie", ""))
        source = self.observed_client.get("/api/observation/source").json()
        self.assertEqual(source["source_commit"], "9b13de835100340f1b3feb6e92c596ca542e55f9")
        self.assertEqual(source["runtime_namespace"], "decision-trace-dev")
        self.assertEqual(source["verified_files"], 131)

    def test_default_8793_runtime_uses_mainline_safe_answers_and_records_reply_mode(self):
        root = TEST_TMP / uuid4().hex[:6]
        root.mkdir(parents=True, exist_ok=True)
        store = ObservationStore(root / "data")
        with patch(
            "tiku_agent.safe_answer_qwen_v0.QwenSafeAnswerClientV0.__call__",
            return_value="我是力答，专注结构力学题库搜索，通过题图检索相似候选题。",
        ):
            app = create_observed_app(runtime_root=root / "runtime", store=store)
            try:
                client = TestClient(app)
                response = client.post("/api/message", json={"text": "你是谁"})

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["intent"], "safe_answer")
                turns = client.get("/api/observation/turns").json()["turns"]
                detail = client.get(
                    f"/api/observation/turns/{turns[0]['turn_id']}"
                ).json()
                completed = next(
                    row for row in detail["events"]
                    if row["event_type"] == "turn_completed"
                )
                self.assertEqual(completed["payload"]["reply_mode"], "llm_safe_reply")
            finally:
                app.state.hook_manager.uninstall()

    def test_external_session_wins_over_stale_legacy_internal_cookie(self):
        self.observed_client.get("/")
        external_session = self.observed_client.cookies.get(EXTERNAL_COOKIE)
        self.assertTrue(external_session)
        self.observed_client.cookies.set("tiku_agent_session", "stale-legacy-session")

        response = self.observed_client.post("/api/message", json={"text": "你好"})
        self.assertEqual(response.status_code, 200)
        turns = self.observed_client.get("/api/observation/turns").json()["turns"]
        self.assertEqual(len(turns), 1)

    def test_session_text_stream_reset_and_image_payloads_match(self):
        self.assertEqual(self.base_client.get("/api/session").json(), self.observed_client.get("/api/session").json())

        base_text = self.base_client.post("/api/message", json={"text": "你好"})
        observed_text = self.observed_client.post("/api/message", json={"text": "你好"})
        self.assertEqual(base_text.status_code, observed_text.status_code)
        self.assertEqual(base_text.json(), observed_text.json())

        base_stream = self.base_client.post("/api/message/stream", json={"text": "你能做什么"})
        observed_stream = self.observed_client.post("/api/message/stream", json={"text": "你能做什么"})
        self.assertEqual(base_stream.status_code, observed_stream.status_code)
        self.assertEqual(base_stream.text, observed_stream.text)

        image = png_bytes()
        base_image = self.base_client.post("/api/image", files={"file": ("q.png", image, "image/png")})
        observed_image = self.observed_client.post("/api/image", files={"file": ("q.png", image, "image/png")})
        self.assertEqual(base_image.status_code, observed_image.status_code)
        base_payload = base_image.json(); observed_payload = observed_image.json()
        for key in ("text", "images", "intent"):
            self.assertEqual(base_payload[key], observed_payload[key])
        self.assertTrue(base_payload["uploaded_image"])
        self.assertTrue(observed_payload["uploaded_image"])

        self.assertEqual(self.base_client.post("/api/reset").json(), self.observed_client.post("/api/reset").json())

    def test_upload_validation_and_traversal_match(self):
        for payload, content_type in ((b"", "image/png"), (b"not-image", "image/png")):
            base = self.base_client.post("/api/image", content=payload, headers={"content-type": content_type})
            observed = self.observed_client.post("/api/image", content=payload, headers={"content-type": content_type})
            self.assertEqual((base.status_code, base.json()), (observed.status_code, observed.json()))
        self.assertEqual(self.base_client.get("/api/media/..%2Fsecret").status_code, self.observed_client.get("/api/media/..%2Fsecret").status_code)

    def test_result_first_ui_safe_summary_and_legacy_revision_compatibility(self):
        self.assertEqual(
            _response_type(AgentResponse(text="继续看题", state={"phase": "ANSWERED"}, intent="small_talk")),
            "text",
        )
        self.observed_client.post("/api/message", json={"text": "你好"})
        turns = self.observed_client.get("/api/observation/turns").json()["turns"]
        self.assertEqual(len(turns), 1)
        detail = self.observed_client.get(f"/api/observation/turns/{turns[0]['turn_id']}").json()
        self.assertGreaterEqual(len(detail["events"]), 3)
        self.assertEqual(detail["review_summary"]["input_summary"], "")
        self.assertEqual(detail["review_summary"]["result_summary"], "")
        self.assertNotIn("你好", str(detail["review_summary"]))
        self.assertEqual(detail["review_summary"]["automatic_issue_count"], len(detail["issues"]))
        completed = next(row for row in detail["events"] if row["event_type"] == "turn_completed")
        self.assertEqual(completed["payload"]["reply_mode"], "fixed_shell")
        self.assertEqual(completed["payload"]["reply_kind"], "greeting")
        script = self.observed_client.get("/observer-assets/observer.js").text
        for required in (
            "你的问题", "Agent 的回答", "这次回答对吗？", "部分正确", "无法判断",
            "选择后立即保存，不需要再提交", "已保存 · 再点一次当前选项可取消",
            "已取消，本轮现在是未评审状态", "withdraw-review", "判断结果：",
            "需要你补充章节", "renderCausalChain", "查看原始 JSON",
            "isUsefulCausalEvent", "本轮没有可继续定位的步骤", "open ? '关闭' : '评审'",
            "处理结果：", "等待用户选择候选题", "识别题图", "判断荷载形式",
            "按字母荷载题检索", "按数值荷载题检索", "humanLoads(summary.loads)",
            "识别为“${summary.structure_type}”", "复筛未完成，已回退使用粗筛排序",
            "if (event.event_type === 'tool_completed') return humanToolResult(payload)",
            "payload.tool_name === 'coarse_search' || payload.tool_name === 'global_search'",
            "payload.code === 'STRUCTURE_FILTER_NOT_APPLICABLE'",
            "从“${beforeText}”进入“${afterText}”",
            "${TOOL_OUTCOME_TEXT[outcome]}：${detail}", "正常未命中", "需要补充信息",
            "部分完成", "工具故障", "可以重试",
            "评审信息暂时没有加载出来，请稍后重试。",
            "道候选进入复筛，最高最终相似度 ${score}%，低于 80%，不予展示",
            "道候选进入复筛，最终相似度低于 80%，不予展示",
            "started?.payload?.input_summary?.candidate_count",
        ):
            self.assertIn(required, script)
        self.assertIn(
            "勾选你认为出错的步骤；勾选后立即保存",
            self.observed_client.get("/").text,
        )
        for removed in (
            "正文未记录", "内容未记录", "章节：未记录", "结束状态：",
            "自动检查：未发现异常", "决定动作：", "来源：${payload.source}",
            "NODE_VERDICTS", "错误类别（可选）", "期望结果（可选",
            "执行结果",
            "noMatchControls", "这次“没有结果”是否合理？", "const NO_MATCH",
        ):
            self.assertNotIn(removed, script)
        self.assertNotIn("人工复核队列", script)
        self.assertNotIn("待复核 ${pending.length}", script)
        self.assertTrue(script.lstrip().startswith("(() => {"))
        self.assertNotIn("评审面板加载失败：${error", script)
        self.assertNotIn("变化：${changed}", script)
        self.assertIn("payload.phase_before !== payload.phase_after", script)
        self.assertIn("Boolean(PHASE_TEXT[payload.phase_after])", script)
        self.assertNotIn("event.event_type === 'turn_completed') return true", script)
        self.assertIn("if (!response.ok)", script)
        self.assertIn("保存失败，请重试", script)
        self.assertIn("文字可能包含本地路径、敏感信息或内容过长", script)
        css = self.observed_client.get("/observer-assets/observer.css").text
        self.assertIn("@media (max-width: 900px)", css)
        self.assertIn("min-height: 44px", css)
        self.assertIn(".observer-causal-node.is-selected", css)
        self.assertIn("accent-color: #15803d", css)
        self.assertIn("node.classList.toggle('is-selected', selected)", script)

        node = shutil.which("node")
        if node:
            mainline_script = self.observed_client.get("/assets/demo.js").text
            syntax = subprocess.run(
                [node, "--check", "-"],
                input=f"{mainline_script}\n{script}",
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(syntax.returncode, 0, syntax.stderr)

        event = next(row for row in detail["events"] if row["event_type"] == "intent_decided")
        first = self.observed_client.post("/api/observation/labels", json={"target_id": event["event_id"], "target_type": "event", "dimension": "intent", "verdict": "correct"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["label_revision"], 1)
        duplicate = self.observed_client.post("/api/observation/labels", json={"target_id": event["event_id"], "target_type": "event", "dimension": "intent", "verdict": "correct"})
        self.assertTrue(duplicate.json()["unchanged"])
        self.assertEqual(duplicate.json()["label_revision"], 1)
        changed = self.observed_client.post("/api/observation/labels", json={"target_id": event["event_id"], "target_type": "event", "dimension": "intent", "verdict": "incorrect"})
        self.assertEqual(changed.json()["label_revision"], 2)
        changed_detail = self.observed_client.get(f"/api/observation/turns/{turns[0]['turn_id']}").json()
        changed_latest = next(row for row in changed_detail["latest_labels"] if row["target_id"] == event["event_id"])
        self.assertEqual((changed_latest["verdict"], changed_latest["label_revision"]), ("incorrect", 2))
        explained = self.observed_client.post("/api/observation/labels", json={
            "target_id": event["event_id"], "target_type": "event", "dimension": "intent",
            "verdict": "incorrect", "expected": "route_search", "reason": "intent mismatch",
            "error_category": "routing",
        })
        self.assertEqual(explained.json()["label_revision"], 3)

        refreshed = self.observed_client.get(f"/api/observation/turns/{turns[0]['turn_id']}").json()
        latest = next(row for row in refreshed["latest_labels"] if row["target_id"] == event["event_id"])
        self.assertEqual((latest["verdict"], latest["label_revision"]), ("incorrect", 3))
        self.assertEqual((latest["expected"], latest["reason"], latest["error_category"]), ("route_search", "intent mismatch", "routing"))
        self.assertEqual(self.observed_client.get("/api/observation/summary").json()["result_reviewed"], 0)

    def test_reply_generation_can_be_selected_as_the_suspected_cause(self):
        self.observed_client.post("/api/message", json={"text": "你好"})
        turn = self.observed_client.get("/api/observation/turns").json()["turns"][0]
        detail = self.observed_client.get(f"/api/observation/turns/{turn['turn_id']}").json()
        completed = next(row for row in detail["events"] if row["event_type"] == "turn_completed")

        selected = self.observed_client.post("/api/observation/labels", json={
            "target_id": completed["event_id"], "target_type": "event",
            "dimension": "causal_suspicion", "verdict": "incorrect",
            "error_category": "suspected", "reason": "回答方式不适合用户问题",
        })

        self.assertEqual(selected.status_code, 200)
        refreshed = self.observed_client.get(f"/api/observation/turns/{turn['turn_id']}").json()
        label = next(row for row in refreshed["latest_labels"] if row["target_id"] == completed["event_id"])
        self.assertEqual((label["dimension"], label["verdict"]), ("causal_suspicion", "incorrect"))

    def test_correct_result_is_one_click_turn_label_without_event_labels(self):
        self.observed_client.post("/api/message", json={"text": "你好"})
        turn = self.observed_client.get("/api/observation/turns").json()["turns"][0]
        response = self.observed_client.post("/api/observation/labels", json={
            "target_id": turn["turn_id"], "target_type": "turn",
            "dimension": "result_interpretation", "verdict": "correct",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["label_revision"], 1)
        detail = self.observed_client.get(f"/api/observation/turns/{turn['turn_id']}").json()
        self.assertEqual(len(detail["latest_labels"]), 1)
        self.assertEqual(detail["latest_labels"][0]["target_type"], "turn")
        self.assertFalse(any(row["target_type"] == "event" for row in detail["latest_labels"]))
        summary = self.observed_client.get("/api/observation/summary").json()
        self.assertEqual((summary["result_turns"], summary["result_reviewed"]), (1, 1))
        self.assertEqual(summary["suspicious_nodes"], 0)

    def test_clicking_selected_result_again_withdraws_entire_turn_review(self):
        self.observed_client.post("/api/message", json={"text": "你好"})
        turn = self.observed_client.get("/api/observation/turns").json()["turns"][0]
        detail = self.observed_client.get(f"/api/observation/turns/{turn['turn_id']}").json()
        intent = next(row for row in detail["events"] if row["event_type"] == "intent_decided")
        for payload in (
            {
                "target_id": turn["turn_id"], "target_type": "turn",
                "dimension": "result_interpretation", "verdict": "incorrect",
            },
            {
                "target_id": intent["event_id"], "target_type": "event",
                "dimension": "causal_suspicion", "verdict": "incorrect",
                "error_category": "suspected",
            },
        ):
            self.assertEqual(self.observed_client.post("/api/observation/labels", json=payload).status_code, 200)

        withdrawn = self.observed_client.post(
            f"/api/observation/turns/{turn['turn_id']}/withdraw-review"
        )
        self.assertEqual(withdrawn.status_code, 200)
        self.assertEqual(len(withdrawn.json()["withdrawn_labels"]), 2)
        refreshed = self.observed_client.get(f"/api/observation/turns/{turn['turn_id']}").json()
        self.assertEqual(refreshed["latest_labels"], [])
        summary = self.observed_client.get("/api/observation/summary").json()
        self.assertEqual(summary["result_reviewed"], 0)
        self.assertEqual(summary["suspicious_nodes"], 0)
        audit_rows = self.observed.state.observation_store.labels()
        self.assertEqual(sum(row.get("label_state") == "withdrawn" for row in audit_rows), 2)

    def test_withdraw_review_rejects_unknown_and_cross_session_turns(self):
        self.observed_client.post("/api/message", json={"text": "你好"})
        turn = self.observed_client.get("/api/observation/turns").json()["turns"][0]
        self.assertEqual(
            self.observed_client.post("/api/observation/turns/not-real/withdraw-review").status_code,
            404,
        )
        other_client = TestClient(self.observed)
        other_client.post("/api/message", json={"text": "你好"})
        self.assertEqual(
            other_client.post(f"/api/observation/turns/{turn['turn_id']}/withdraw-review").status_code,
            404,
        )

    def test_partial_result_and_multiple_optional_causal_nodes_preserve_unreviewed(self):
        self.observed_client.post("/api/message", json={"text": "你好"})
        turn = self.observed_client.get("/api/observation/turns").json()["turns"][0]
        detail = self.observed_client.get(f"/api/observation/turns/{turn['turn_id']}").json()
        events = [row for row in detail["events"] if row["event_type"] != "turn_completed"]
        self.assertGreaterEqual(len(events), 2)

        partial = self.observed_client.post("/api/observation/labels", json={
            "target_id": turn["turn_id"], "target_type": "turn",
            "dimension": "result_interpretation", "verdict": "uncertain",
            "error_category": "partial_correct", "reason": "候选里只有部分符合",
        })
        self.assertEqual(partial.status_code, 200)
        self.assertNotIn("expected", partial.json())
        for event in events[:2]:
            selected = self.observed_client.post("/api/observation/labels", json={
                "target_id": event["event_id"], "target_type": "event",
                "dimension": "causal_suspicion", "verdict": "uncertain",
                "error_category": "suspected",
            })
            self.assertEqual(selected.status_code, 200)

        refreshed = self.observed_client.get(f"/api/observation/turns/{turn['turn_id']}").json()
        causal = [row for row in refreshed["latest_labels"] if row["dimension"] == "causal_suspicion"]
        self.assertEqual(len(causal), 2)
        unlabeled_ids = {row["event_id"] for row in events[2:]}
        self.assertTrue(unlabeled_ids.isdisjoint({row["target_id"] for row in refreshed["latest_labels"]}))
        self.assertEqual(self.observed_client.get("/api/observation/summary").json()["suspicious_nodes"], 2)

    def test_revisions_optional_expected_no_match_and_privacy(self):
        self.observed_client.post("/api/message", json={"text": "你好"})
        turn = self.observed_client.get("/api/observation/turns").json()["turns"][0]
        detail = self.observed_client.get(f"/api/observation/turns/{turn['turn_id']}").json()
        event = next(row for row in detail["events"] if row["event_type"] == "intent_decided")

        first = self.observed_client.post("/api/observation/labels", json={
            "target_id": turn["turn_id"], "target_type": "turn", "dimension": "result_interpretation",
            "verdict": "incorrect", "reason": "最终回答不符合题意",
        })
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["label_revision"], 1)
        self.assertNotIn("expected", first.json())
        revised = self.observed_client.post("/api/observation/labels", json={
            "target_id": turn["turn_id"], "target_type": "turn", "dimension": "result_interpretation",
            "verdict": "incorrect", "reason": "最终回答不符合题意",
            "no_match_classification": "false_no_match",
        })
        self.assertEqual(revised.json()["label_revision"], 2)
        self.assertEqual(revised.json()["no_match_classification"], "false_no_match")

        selected = self.observed_client.post("/api/observation/labels", json={
            "target_id": event["event_id"], "target_type": "event", "dimension": "causal_suspicion",
            "verdict": "incorrect", "reason": "意图判断偏了", "error_category": "suspected",
        })
        self.assertEqual(selected.json()["label_revision"], 1)
        changed = self.observed_client.post("/api/observation/labels", json={
            "target_id": event["event_id"], "target_type": "event", "dimension": "causal_suspicion",
            "label_state": "withdrawn",
        })
        self.assertEqual(changed.json()["label_revision"], 2)
        self.assertEqual(changed.json()["label_state"], "withdrawn")
        self.assertNotIn("verdict", changed.json())
        refreshed = self.observed_client.get(f"/api/observation/turns/{turn['turn_id']}").json()
        self.assertFalse(any(
            row["target_id"] == event["event_id"] and row["dimension"] == "causal_suspicion"
            for row in refreshed["latest_labels"]
        ))
        audit_rows = [
            row for row in self.observed.state.observation_store.labels()
            if row["target_id"] == event["event_id"] and row["dimension"] == "causal_suspicion"
        ]
        self.assertEqual([row["label_revision"] for row in audit_rows], [1, 2])
        self.assertEqual(audit_rows[-1]["label_state"], "withdrawn")
        summary = self.observed_client.get("/api/observation/summary").json()
        self.assertEqual(summary["suspicious_nodes"], 0)
        self.assertGreater(summary["unreviewed_nodes"], 0)
        self.assertNotIn("correct", summary["verdicts"])

        rejected = self.observed_client.post("/api/observation/labels", json={
            "target_id": turn["turn_id"], "target_type": "turn", "dimension": "result_interpretation",
            "verdict": "incorrect", "reason": r"C:\private\answer.png",
        })
        self.assertEqual(rejected.status_code, 400)

    def test_label_api_rejects_forged_mismatched_and_cross_session_targets_without_writes(self):
        self.observed_client.post("/api/message", json={"text": "你好"})
        turn = self.observed_client.get("/api/observation/turns").json()["turns"][0]
        detail = self.observed_client.get(f"/api/observation/turns/{turn['turn_id']}").json()
        intent = next(row for row in detail["events"] if row["event_type"] == "intent_decided")
        store = self.observed.state.observation_store
        before = len(store.labels())

        attempts = [
            ({"target_id": "forged", "target_type": "event", "dimension": "causal_suspicion", "verdict": "incorrect"}, 404),
            ({"target_id": intent["event_id"], "target_type": "turn", "dimension": "result_interpretation", "verdict": "incorrect"}, 400),
            ({"target_id": turn["turn_id"], "target_type": "event", "dimension": "causal_suspicion", "verdict": "incorrect"}, 400),
            ({"target_id": intent["event_id"], "target_type": "event", "dimension": "tool_output", "verdict": "incorrect"}, 400),
            ({"target_id": intent["event_id"], "target_type": "unknown", "dimension": "intent", "verdict": "incorrect"}, 400),
        ]
        for payload, expected_status in attempts:
            response = self.observed_client.post("/api/observation/labels", json=payload)
            self.assertEqual(response.status_code, expected_status, response.text)
            self.assertEqual(len(store.labels()), before)

        other_client = TestClient(self.observed)
        other_client.post("/api/message", json={"text": "你好"})
        cross = other_client.post("/api/observation/labels", json={
            "target_id": intent["event_id"], "target_type": "event",
            "dimension": "causal_suspicion", "verdict": "incorrect",
        })
        self.assertEqual(cross.status_code, 404)
        self.assertEqual(len(store.labels()), before)

    def test_missing_real_authorization_record_is_promoted_without_fixed_cardinality(self):
        events = [
            {"turn_id": "t", "event_id": "1", "sequence": 1, "event_type": "turn_started", "payload": {}},
            {"turn_id": "t", "event_id": "2", "sequence": 2, "event_type": "turn_completed", "payload": {"authorization_count": 1}},
        ]
        codes = {issue["code"] for issue in scan_events(events)}
        self.assertIn("authorization_trace_count_mismatch", codes)
        events[1]["payload"]["authorization_count"] = 0
        codes = {issue["code"] for issue in scan_events(events)}
        self.assertNotIn("authorization_trace_count_mismatch", codes)


if __name__ == "__main__":
    unittest.main()
