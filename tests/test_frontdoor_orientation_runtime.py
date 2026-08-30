from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.run_tiku_agent_8896 import build_runtime
from tiku_agent.a3_runtime import A3MvpRuntime, A3SessionState
from tiku_agent.frontdoor_orientation_runtime import FrontdoorOrientationA3Runtime


class FrontdoorOrientationRuntimeTest(unittest.TestCase):
    def test_orientation_runs_before_the_shared_a1_a2_a3_router(self):
        source = Path("source.jpg").resolve()
        upright = Path("source.a3-upright.jpg").resolve()
        orienter = Mock(return_value=upright)
        store = Mock()
        runtime = object.__new__(FrontdoorOrientationA3Runtime)
        runtime.frontdoor_orienter = orienter
        runtime.store = store
        state = A3SessionState(session_id="session")
        expected = object()

        with patch.object(
            A3MvpRuntime,
            "_route_persisted_image",
            autospec=True,
            return_value=expected,
        ) as shared_router:
            actual = runtime._route_persisted_image(
                state,
                source,
                identity_key="identity",
                progress=None,
                request_id="request",
            )

        self.assertIs(actual, expected)
        orienter.assert_called_once_with(source)
        self.assertEqual(state.source_page_path, str(upright))
        store.save.assert_called_once_with(state)
        shared_router.assert_called_once_with(
            runtime,
            state,
            upright,
            identity_key="identity",
            progress=None,
            request_id="request",
        )

    def test_8896_builder_disables_the_old_a3_only_orientation_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            orienter = Mock()
            runtime = build_runtime(
                Path(temp),
                enable_triage=False,
                enable_auto_crop=False,
                enable_a3_intent_v1=False,
                a3_page_orienter=orienter,
                orient_before_routing=True,
            )

        self.assertIsInstance(runtime, FrontdoorOrientationA3Runtime)
        self.assertIs(runtime.frontdoor_orienter, orienter)
        self.assertIsNone(runtime.a3_page_orienter)


if __name__ == "__main__":
    unittest.main()
