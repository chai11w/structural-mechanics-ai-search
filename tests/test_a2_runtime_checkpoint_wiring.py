import unittest

from tiku_agent.agent import TikuSearchAgent
from tiku_agent.session_runtime import AgentSessionRuntime
from tiku_agent.session_store import SQLiteSessionStore
from tiku_agent.state import AgentState
from tiku_agent.tool_result import ToolResult
from tiku_agent.checkpoint_capture_gate import A2CheckpointCaptureGateV1
from tiku_agent.a2_checkpoint_recorder import A2CaptureRecordResultV1
from tiku_shared.trace_context import TraceContext, trace_context_scope


class _Recorder:
    producer = object()
    gate = A2CheckpointCaptureGateV1(enabled=True)

    def __init__(self):
        self.calls = []

    def capture_stage(self, context, **kwargs):
        self.calls.append((context, kwargs))
        return A2CaptureRecordResultV1(True, False, "TEST")

    def last_successful(self, context):
        return ""


class A2RuntimeCheckpointWiringTest(unittest.TestCase):
    def test_runtime_attaches_optional_emitter_without_changing_agent_default(self):
        state = AgentState(session_id="session-test", task_revision=1, current_search_id="search_test")
        recorder = _Recorder()
        runtime = AgentSessionRuntime(SQLiteSessionStore(":memory:"), checkpoint_recorder=recorder)
        agent = TikuSearchAgent(state=state, use_llm_intent=False)
        runtime._attach_checkpoint_emitter(agent, identity_key="invite_test", request_id="req_" + "1" * 32)
        with trace_context_scope(TraceContext.create(request_id="req_" + "1" * 32)):
            agent._emit_checkpoint(
                "question_analyzed",
                ToolResult.success(code="IMAGE_ANALYZED", data={}),
                {"analysis": {}},
            )
        self.assertEqual(len(recorder.calls), 1)
        context, kwargs = recorder.calls[0]
        self.assertEqual(context.owner().workflow_search_id, "search_test")
        self.assertEqual(kwargs["admission"].request_kind, "search")


if __name__ == "__main__":
    unittest.main()
