from pathlib import Path
import shutil
import unittest
from uuid import uuid4

from scripts.run_tiku_agent_demo import build_argument_parser, build_runtime


class MainlineLauncherTest(unittest.TestCase):
    def test_safe_answers_are_enabled_by_default_with_explicit_rollback(self):
        parser = build_argument_parser()

        self.assertTrue(parser.parse_args([]).enable_safe_answer_v0)
        self.assertFalse(
            parser.parse_args(["--disable-safe-answer-v0"]).enable_safe_answer_v0
        )

    def test_enabled_runtime_uses_model_only_for_safe_conversation(self):
        root = Path(__file__).resolve().parents[1] / f".tmp_test_8790_{uuid4().hex}"
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        requests = []

        def model_client(request):
            requests.append(request)
            return "我是力答，专注结构力学题库搜索，通过题图检索相似候选题。"

        runtime = build_runtime(root, safe_answer_model_client=model_client)
        safe_response = runtime.handle_text("safe-session", "你是谁")
        business_response = runtime.handle_text("business-session", "帮我搜个题")

        self.assertEqual(safe_response.intent, "safe_answer")
        self.assertEqual(safe_response.reply_source, "model")
        self.assertEqual(len(requests), 1)
        self.assertNotEqual(business_response.intent, "safe_answer")
        self.assertEqual(runtime.session_snapshot("safe-session")["phase"], "IDLE")


if __name__ == "__main__":
    unittest.main()
