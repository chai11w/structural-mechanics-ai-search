import unittest

from scripts.run_tiku_agent_8897 import (
    A2_RERANK_POLICY,
    DEFAULT_PORT,
    DEFAULT_RUNTIME_DIR,
    SESSION_COOKIE,
    build_argument_parser,
)


class Agent8897LauncherTest(unittest.TestCase):
    def test_isolated_defaults_and_qwen_v1_policy(self):
        args = build_argument_parser().parse_args([])

        self.assertEqual(DEFAULT_PORT, 8897)
        self.assertEqual(args.port, 8897)
        self.assertEqual(SESSION_COOKIE, "tiku_agent_8897_session")
        self.assertTrue(str(DEFAULT_RUNTIME_DIR).endswith(".tmp_tiku_agent_a3_mvp_8897"))
        self.assertEqual(A2_RERANK_POLICY["provider"], "qwen")
        self.assertEqual(A2_RERANK_POLICY["model"], "qwen3.7-plus")


if __name__ == "__main__":
    unittest.main()
