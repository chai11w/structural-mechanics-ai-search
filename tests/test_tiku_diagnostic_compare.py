from __future__ import annotations

import json
import unittest

from tiku_diagnostics.compare import (
    COMPARISON_CLASSIFICATIONS,
    COMPARISON_FIELD_ALLOWLIST,
    compare_bundles,
)


class TikuDiagnosticCompareTest(unittest.TestCase):
    @staticmethod
    def cost_item(
        *,
        association: str,
        run_id: str = "run_shared",
        cost: int = 1200,
        completeness: str | None = None,
    ) -> dict[str, object]:
        return {
            "source": "model_cost_runs",
            "association": association,
            "completeness": completeness
            or ("partial" if association == "legacy_compatibility" else "complete"),
            "timestamp": "2026-08-27T01:02:03+00:00",
            "record": {
                "run_id": run_id,
                "trace_id": "trace_" + "a" * 32,
                "identity_key": "invite-privacy-safe-01",
                "search_key": "search-question-01",
                "task_kind": "image",
                "started_at": "2026-08-27T01:02:03+00:00",
                "finished_at": "2026-08-27T01:02:04+00:00",
                "outcome": "success",
                "call_count": 1,
                "total_tokens": 42,
                "estimated_cost_micros": cost,
                "warning_codes": [],
                "schema_version": 3,
                "session_key": "private-session-key",
                "request_id": "provider-request-mirror",
                "provider_request_id": "provider-secret-id",
                "detail": "private free-form detail",
                "conversation": [{"message": "private conversation"}],
                "local_path": "C:\\private\\question.jpg",
            },
        }

    @staticmethod
    def feedback_item(
        *, association: str, rated_response_id: str, legacy_binding: bool
    ) -> dict[str, object]:
        return {
            "source": "feedback",
            "association": association,
            "completeness": (
                "partial" if association == "legacy_compatibility" else "complete"
            ),
            "record": {
                "feedback_id": "f" * 32,
                "feedback_number": "FB-20260827-FFFFFFFFFF",
                "rated_response_id": rated_response_id,
                "identity_key": "invite-privacy-safe-01",
                "rating": "negative",
                "tags": ["ranking_issue"],
                "status": "SUCCESS",
                "layer": "tool",
                "code": "REQUEST_SUCCEEDED",
                "schema_version": 8 if rated_response_id else 7,
                "legacy_binding": legacy_binding,
                "message_id": "private-browser-message",
                "detail": "private feedback detail",
                "admin_note": "private admin note",
            },
        }

    @staticmethod
    def bundle(
        evidence: list[dict[str, object]],
        *,
        cost_status: str = "ok",
        feedback_status: str = "ok",
    ) -> dict[str, object]:
        return {
            "summary": {"complete": True, "evidence_gaps": []},
            "evidence": evidence,
            "sources": [
                {"name": "model_costs", "status": cost_status, "record_count": 0},
                {"name": "feedback", "status": feedback_status, "record_count": 0},
            ],
        }

    def test_match_is_deterministic_and_returns_only_safe_whitelisted_fields(self):
        context = {
            "source": "trace_events",
            "association": "direct_selector",
            "completeness": "complete",
            "record": {"trace_id": "trace_" + "a" * 32},
        }
        authoritative = self.bundle(
            [context, self.cost_item(association="trace_exact")]
        )
        legacy = self.bundle(
            [
                self.cost_item(association="legacy_compatibility"),
                {
                    "source": "responses",
                    "association": "authoritative_trace_id",
                    "completeness": "complete",
                    "record": {"response_id": "resp_" + "b" * 32},
                },
            ]
        )

        first = compare_bundles(authoritative, legacy).to_dict()
        second = compare_bundles(
            {**authoritative, "evidence": list(reversed(authoritative["evidence"]))},
            {**legacy, "evidence": list(reversed(legacy["evidence"]))},
        ).to_dict()

        self.assertEqual(first, second)
        self.assertEqual(first["classification"], "match")
        self.assertEqual(first["summary"]["authoritative_count"], 1)
        authoritative_item = first["authoritative"][0]
        self.assertEqual(authoritative_item["source"], "model_cost_runs")
        self.assertEqual(authoritative_item["association"], "trace_exact")
        self.assertEqual(authoritative_item["completeness"], "complete")
        self.assertEqual(first["legacy"][0]["completeness"], "partial")
        rendered = json.dumps(first, ensure_ascii=False, sort_keys=True)
        for forbidden in (
            "session_key",
            "private-session-key",
            "request_id",
            "provider_request_id",
            "provider-secret-id",
            "private free-form detail",
            "private conversation",
            "C:\\private\\question.jpg",
        ):
            self.assertNotIn(forbidden, rendered)

    def test_healthy_empty_side_distinguishes_authoritative_and_legacy_only(self):
        authoritative_only = compare_bundles(
            self.bundle([self.cost_item(association="trace_exact")]),
            self.bundle([]),
        )
        legacy_only = compare_bundles(
            self.bundle([]),
            self.bundle([self.cost_item(association="legacy_compatibility")]),
        )

        self.assertEqual(authoritative_only.classification, "authoritative_only")
        self.assertEqual(len(authoritative_only.authoritative_only), 1)
        self.assertEqual(legacy_only.classification, "legacy_only")
        self.assertEqual(len(legacy_only.legacy_only), 1)

    def test_shared_items_do_not_hide_an_extra_item_on_one_side(self):
        shared_authoritative = self.cost_item(association="trace_exact")
        shared_legacy = self.cost_item(association="legacy_compatibility")
        extra_authoritative = self.cost_item(
            association="trace_exact", run_id="run_authoritative_extra"
        )
        extra_legacy = self.cost_item(
            association="legacy_compatibility", run_id="run_legacy_extra"
        )

        authoritative_only = compare_bundles(
            self.bundle([shared_authoritative, extra_authoritative]),
            self.bundle([shared_legacy]),
        )
        legacy_only = compare_bundles(
            self.bundle([shared_authoritative]),
            self.bundle([shared_legacy, extra_legacy]),
        )

        self.assertEqual(authoritative_only.classification, "authoritative_only")
        self.assertEqual(len(authoritative_only.authoritative_only), 1)
        self.assertEqual(legacy_only.classification, "legacy_only")
        self.assertEqual(len(legacy_only.legacy_only), 1)

    def test_conflict_reports_both_safe_fact_projections(self):
        result = compare_bundles(
            self.bundle([self.cost_item(association="trace_exact", cost=1200)]),
            self.bundle(
                [self.cost_item(association="legacy_compatibility", cost=9900)]
            ),
        ).to_dict()

        self.assertEqual(result["classification"], "conflict")
        self.assertEqual(result["summary"]["authoritative_only_count"], 1)
        self.assertEqual(result["summary"]["legacy_only_count"], 1)
        self.assertEqual(
            result["differences"]["authoritative_only"][0]["fields"][
                "estimated_cost_micros"
            ],
            1200,
        )
        self.assertEqual(
            result["differences"]["legacy_only"][0]["fields"][
                "estimated_cost_micros"
            ],
            9900,
        )

    def test_unavailable_or_failed_opposite_source_is_evidence_missing(self):
        missing = compare_bundles(
            self.bundle([self.cost_item(association="trace_exact")]),
            self.bundle([], cost_status="missing"),
        )
        failed = compare_bundles(
            self.bundle([self.cost_item(association="trace_exact")]),
            self.bundle([], cost_status="query_failed"),
        )
        malformed = compare_bundles(self.bundle([]), {"sources": []})

        self.assertEqual(missing.classification, "evidence_missing")
        self.assertEqual(failed.classification, "evidence_missing")
        self.assertEqual(malformed.classification, "evidence_missing")

    def test_failed_source_state_precedes_leftover_evidence(self):
        authoritative = self.bundle([self.cost_item(association="trace_exact")])
        legacy = self.bundle(
            [self.cost_item(association="legacy_compatibility")],
            cost_status="missing",
        )

        result = compare_bundles(authoritative, legacy).to_dict()

        self.assertEqual(result["classification"], "evidence_missing")
        self.assertIn("legacy:model_costs:missing", result["evidence_gaps"])

    def test_feedback_v8_and_v7_share_a_stable_key_but_expose_binding_conflict(self):
        response_id = "resp_" + "c" * 32
        authoritative = self.feedback_item(
            association="feedback_response_exact",
            rated_response_id=response_id,
            legacy_binding=False,
        )
        legacy = self.feedback_item(
            association="legacy_compatibility",
            rated_response_id="",
            legacy_binding=True,
        )

        result = compare_bundles(
            self.bundle([authoritative]), self.bundle([legacy])
        ).to_dict()

        self.assertEqual(result["classification"], "conflict")
        self.assertEqual(
            result["authoritative"][0]["comparison_key"],
            result["legacy"][0]["comparison_key"],
        )
        self.assertEqual(
            result["authoritative"][0]["fields"]["rated_response_id"],
            response_id,
        )
        self.assertEqual(result["legacy"][0]["fields"]["rated_response_id"], "")
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("private-browser-message", rendered)
        self.assertNotIn("private feedback detail", rendered)
        self.assertNotIn("private admin note", rendered)

    def test_contract_has_exact_classifications_and_no_request_id_fields(self):
        self.assertEqual(
            COMPARISON_CLASSIFICATIONS,
            {
                "match",
                "authoritative_only",
                "legacy_only",
                "conflict",
                "evidence_missing",
            },
        )
        for fields in COMPARISON_FIELD_ALLOWLIST.values():
            self.assertNotIn("request_id", fields)
            self.assertNotIn("provider_request_id", fields)
            self.assertNotIn("session_key", fields)
        self.assertNotIn("task_id", COMPARISON_FIELD_ALLOWLIST["task_logs"])


if __name__ == "__main__":
    unittest.main()
