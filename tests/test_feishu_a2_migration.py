import unittest
from unittest.mock import patch

from multi_agent_pipeline import MultiAgentCoordinator
from tiku_agent.tool_result import ToolResult


class FeishuA2MigrationTest(unittest.TestCase):
    def test_symbolic_search_uses_mainline_tool_order(self):
        calls = []

        def route_bank(loads):
            calls.append("route")
            return ToolResult.success(
                code="BANK_ROUTE_SELECTED",
                data={
                    "route": "symbolic",
                    "category": "symbolic_unassigned",
                    "reason": "unassigned symbolic load",
                    "excel_root": "F:/symbolic",
                    "load_details": [{"type": "均布", "raw": "q"}],
                },
            )

        def classify_structure(image, *, route, classified, config):
            calls.append("structure")
            self.assertEqual(route, "symbolic")
            self.assertEqual(image, "query.jpg")
            return ToolResult.success(
                code="STRUCTURE_CLASSIFIED_FROM_IMAGE",
                data={"structure_type": "梁", "confidence": 0.99, "reason": "mock"},
            )

        def coarse_search(loads, *, chapter, route, structure_type, top_k, query_image_path, config):
            calls.append("coarse")
            self.assertEqual((chapter, route, structure_type), ("2静定结构", "symbolic", "梁"))
            self.assertEqual(query_image_path, "query.jpg")
            return ToolResult.success(
                code="COARSE_CANDIDATES_FOUND",
                data={
                    "candidates": [{
                        "rank": 1,
                        "path": "candidate.jpg",
                        "name": "candidate.jpg",
                        "score": 1.0,
                        "long_width": "3L×0",
                        "single_side": "",
                    }],
                    "structure_filter_applied": True,
                    "dimension_filter": {"enabled": True, "applied": True},
                },
            )

        def rerank(image, candidates, *, route, rerank_top, **kwargs):
            calls.append("rerank")
            self.assertEqual((image, route), ("query.jpg", "symbolic"))
            self.assertEqual(candidates[0]["long_width"], "3L×0")
            return ToolResult.success(
                code="RERANK_COMPLETED",
                data={
                    "reranked": True,
                    "visible_candidates": [{
                        **candidates[0],
                        "final_score": 0.96,
                    }],
                    "rerank_note": "",
                },
            )

        with patch("tiku_agent.tools.route_bank_tool", side_effect=route_bank), patch(
            "tiku_agent.tools.classify_structure_tool", side_effect=classify_structure
        ), patch("tiku_agent.tools.coarse_search_tool", side_effect=coarse_search), patch(
            "tiku_agent.tools.rerank_candidates_tool", side_effect=rerank
        ), patch("multi_agent_pipeline.write_last_search"):
            result = MultiAgentCoordinator(dimension_filter_enabled=True).search_loads(
                [{"type": "均布", "raw": "q"}],
                "2静定结构",
                query_image_path="query.jpg",
                rerank=True,
            )

        self.assertEqual(calls, ["route", "structure", "coarse", "rerank"])
        self.assertEqual(result.route.route, "symbolic")
        self.assertEqual(result.structure_type, "梁")
        self.assertTrue(result.structure_filter_applied)
        self.assertTrue(result.dimension_filter["applied"])
        self.assertTrue(result.reranked)
        self.assertEqual(result.results[0]["final_score"], 0.96)

    def test_main_route_skips_symbolic_structure_classifier(self):
        route_result = ToolResult.success(
            code="BANK_ROUTE_SELECTED",
            data={
                "route": "main",
                "category": "main_numeric",
                "reason": "numeric load",
                "excel_root": "F:/main",
                "load_details": [],
            },
        )
        coarse_result = ToolResult.success(
            code="COARSE_CANDIDATES_FOUND",
            data={
                "candidates": [{"rank": 1, "path": "candidate.jpg", "name": "candidate.jpg", "score": 1.0}],
                "structure_filter_applied": False,
                "dimension_filter": {"reason": "not_symbolic"},
            },
        )
        with patch("tiku_agent.tools.route_bank_tool", return_value=route_result), patch(
            "tiku_agent.tools.classify_structure_tool"
        ) as classify, patch("tiku_agent.tools.coarse_search_tool", return_value=coarse_result), patch(
            "multi_agent_pipeline.write_last_search"
        ):
            result = MultiAgentCoordinator().search_loads(
                [{"type": "集中", "raw": "10"}],
                "2静定结构",
                query_image_path="query.jpg",
                rerank=False,
            )

        classify.assert_called_once()
        self.assertEqual(result.route.route, "main")
        self.assertFalse(result.reranked)
        self.assertEqual(result.results[0]["name"], "candidate.jpg")


if __name__ == "__main__":
    unittest.main()
