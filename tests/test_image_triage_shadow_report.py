import json
from pathlib import Path
import tempfile
import unittest

from scripts.report_image_triage_shadow import load_report


class ImageTriageShadowReportTest(unittest.TestCase):
    def test_report_aggregates_routes_latency_tokens_and_errors(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "triage_shadow.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "status": "ok",
                                "route_candidate": "A2",
                                "final_route": "A2",
                                "duration_ms": 120,
                                "prompt_tokens": 10,
                                "completion_tokens": 3,
                                "total_tokens": 13,
                            }
                        ),
                        json.dumps(
                            {
                                "status": "ok",
                                "route_candidate": "A3",
                                "final_route": "A3",
                                "duration_ms": 300,
                                "prompt_tokens": 20,
                                "completion_tokens": 5,
                                "total_tokens": 25,
                            }
                        ),
                        json.dumps(
                            {
                                "status": "error",
                                "error_kind": "parse_error",
                                "duration_ms": 50,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report = load_report([path])

        self.assertEqual(report["record_count"], 3)
        self.assertEqual(report["route_candidate_counts"], {"A2": 1, "A3": 1, "unknown": 1})
        self.assertEqual(report["final_route_counts"], {"A2": 1, "A3": 1, "unknown": 1})
        self.assertEqual(report["status_counts"], {"error": 1, "ok": 2})
        self.assertEqual(report["error_counts"], {"parse_error": 1})
        self.assertEqual(report["latency_ms"]["p50_ms"], 120)
        self.assertEqual(report["latency_ms"]["p95_ms"], 300)
        self.assertEqual(report["token_totals"]["total_tokens"], 38)

    def test_missing_log_is_safe(self):
        report = load_report([Path("missing-triage-shadow.jsonl")])
        self.assertEqual(report["status"], "no_data")
        self.assertEqual(report["record_count"], 0)


if __name__ == "__main__":
    unittest.main()
