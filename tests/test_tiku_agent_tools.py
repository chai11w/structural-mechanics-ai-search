from pathlib import Path
import unittest

from tiku_agent.tools import (
    AgentToolConfig,
    classify_structure_tool,
    parse_candidate_action_tool,
    route_bank_tool,
)


class TikuAgentToolsTest(unittest.TestCase):
    def test_agent_runtime_is_isolated_from_old_feishu_state(self):
        config = AgentToolConfig()
        self.assertEqual(config.runtime_dir, Path(__file__).resolve().parents[1] / ".tmp_tiku_agent")
        self.assertNotIn(".tmp_feishu_tiku", str(config.runtime_dir))
        self.assertNotIn(".tmp_feishu_tiku", str(config.qwen_cache_path))
        self.assertNotIn(".tmp_feishu_tiku", str(config.answer_output_dir))

    def test_route_bank_symbolic_load(self):
        result = route_bank_tool([{"type": "集中", "raw": "P"}])
        self.assertTrue(result.ok)
        self.assertEqual(result.data["route"], "symbolic")
        self.assertEqual(result.next_state, "READY_FOR_STRUCTURE")

    def test_structure_tool_skips_non_symbolic_routes(self):
        result = classify_structure_tool(None, route="main")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["structure_type"], "")
        self.assertFalse(result.data["filter_applicable"])

    def test_candidate_action_parser_answer_delete_and_cancel(self):
        self.assertEqual(
            parse_candidate_action_tool("1", candidate_count=3).data,
            {"action": "answer", "rank": 1},
        )
        self.assertEqual(
            parse_candidate_action_tool("-2", candidate_count=3).data,
            {"action": "delete_candidate", "rank": 2},
        )
        self.assertEqual(parse_candidate_action_tool("0", candidate_count=3).data, {"action": "cancel"})


if __name__ == "__main__":
    unittest.main()
