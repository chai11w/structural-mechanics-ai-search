from __future__ import annotations

import unittest

from tiku_agent.user_output import FinalOutputKind, UserAction
from tiku_agent.user_output_integration import (
    OutputDraftV1,
    build_a2_output_draft,
    build_a3_output_draft,
    finalize_output_draft,
)
from tiku_shared.request_protocol import (
    RequestProtocol,
    RequestStatus,
)


def _protocol(code: str) -> RequestProtocol:
    return RequestProtocol.from_code(
        code,
        request_id="req-integration-001",
        search_id="search-integration-001",
    )


class OutputIntegrationTests(unittest.TestCase):
    def test_output_draft_is_frozen_and_defensively_copies_facts(self):
        source = {"candidate_count": 2, "source_chapters": ["力法"]}
        draft = OutputDraftV1(
            message_key="search.candidates.ready",
            phase="WAIT_CANDIDATE_CHOICE",
            facts=source,
            allowed_actions=(UserAction.SELECT_CANDIDATE,),
            media_policy="candidate_set",
        )
        source["candidate_count"] = 99
        source["source_chapters"].append("位移法")

        self.assertEqual(draft.facts["candidate_count"], 2)
        self.assertEqual(draft.facts["source_chapters"], ("力法",))
        with self.assertRaises(TypeError):
            draft.facts["candidate_count"] = 3  # type: ignore[index]
        with self.assertRaises(Exception):
            draft.phase = "ERROR"  # type: ignore[misc]

    def test_a2_chapter_storage_key_becomes_public_display_name(self):
        protocol = _protocol("REQUEST_SUCCEEDED")
        draft = build_a2_output_draft(
            "set_chapter",
            {"phase": "READY_TO_ROUTE", "current_chapter": "4力法"},
            protocol,
        )
        public = finalize_output_draft(draft, protocol, 0, 0)

        self.assertEqual(public.message_key, "search.chapter.saved")
        self.assertIn("力法", public.text)
        self.assertNotIn("4力法", public.text)

    def test_a2_saved_chapter_prefers_pending_chapter_over_previous_search(self):
        protocol = _protocol("REQUEST_SUCCEEDED")
        draft = build_a2_output_draft(
            "set_chapter",
            {
                "phase": "ANSWERED",
                "current_chapter": "4力法",
                "pending_chapter": "5位移法",
            },
            protocol,
        )
        public = finalize_output_draft(draft, protocol, 0, 0)

        self.assertEqual(public.message_key, "search.chapter.saved")
        self.assertIn("位移法", public.text)
        self.assertNotIn("力法。", public.text)

    def test_unknown_chapter_without_public_name_uses_actionable_chapter_prompt(self):
        marker = "SECRET_UNKNOWN_CHAPTER"
        protocol = _protocol("UNKNOWN_CHAPTER")
        draft = build_a2_output_draft(
            "out_of_scope",
            {"phase": "WAIT_CHAPTER", "current_chapter": marker},
            protocol,
        )
        public = finalize_output_draft(draft, protocol, 0, 0)

        self.assertEqual(public.message_key, "search.chapter.required")
        self.assertEqual(public.protocol.code, "UNKNOWN_CHAPTER")
        self.assertIn(UserAction.CHANGE_CHAPTER, public.allowed_actions)
        self.assertIn("目前支持", public.text)
        self.assertNotIn(marker, public.text)

    def test_current_protocol_wins_over_stale_candidate_state(self):
        marker = "SECRET_STALE_STATE"
        cases = (
            (
                "UNKNOWN_CHAPTER",
                "search.chapter.required",
                UserAction.CHANGE_CHAPTER,
            ),
            (
                "CHAPTER_REQUIRED",
                "search.chapter.required",
                UserAction.CHANGE_CHAPTER,
            ),
            (
                "COARSE_SEARCH_FAILED",
                "search.failed.retryable",
                UserAction.RETRY_SEARCH,
            ),
        )
        for code, expected_key, expected_action in cases:
            with self.subTest(code=code):
                protocol = _protocol(code)
                draft = build_a2_output_draft(
                    "out_of_scope",
                    {
                        "phase": "WAIT_CANDIDATE_CHOICE",
                        "candidate_count": 2,
                        "current_chapter": marker,
                        "current_image_path": f"C:/private/{marker}.png",
                    },
                    protocol,
                )
                public = finalize_output_draft(draft, protocol, 0, 0)

                self.assertEqual(draft.message_key, expected_key)
                self.assertEqual(public.message_key, expected_key)
                self.assertEqual(public.protocol.code, code)
                self.assertIn(expected_action, public.allowed_actions)
                self.assertNotIn(marker, public.text)

    def test_safe_conversation_does_not_inherit_state_default_protocol(self):
        cases = (
            ("CHAPTER_REQUIRED", "WAIT_CHAPTER"),
            ("NO_MATCH", "NO_MATCH"),
            ("AGENT_FAILED", "ERROR"),
        )
        for code, phase in cases:
            with self.subTest(code=code):
                protocol = _protocol(code)
                draft = build_a2_output_draft(
                    "safe_answer",
                    {"phase": phase, "last_error": "SECRET_DO_NOT_ECHO"},
                    protocol,
                    variant="courtesy",
                )
                public = finalize_output_draft(draft, protocol, 0, 0)
                self.assertEqual(public.message_key, "conversation.courtesy")
                self.assertEqual(public.protocol.code, "REQUEST_SUCCEEDED")
                self.assertNotIn("SECRET_DO_NOT_ECHO", public.text)

    def test_grounded_supported_chapter_reply_keeps_the_full_scope_fact(self):
        protocol = _protocol("REQUEST_SUCCEEDED")
        draft = build_a2_output_draft(
            "safe_answer",
            {"phase": "WAIT_CHAPTER"},
            protocol,
            variant="supported_chapters",
        )
        public = finalize_output_draft(draft, protocol, 0, 0)

        self.assertEqual(public.message_key, "conversation.supported_chapters")
        self.assertIn("静定结构受力", public.text)
        self.assertIn("矩阵位移法和影响线仅支持含具体外荷载", public.text)

    def test_a2_candidates_render_and_partial_protocol_has_one_notice(self):
        state = {
            "phase": "WAIT_CANDIDATE_CHOICE",
            "current_chapter": "4力法",
            "candidate_count": 2,
            "candidates": [{"chapter": "4力法"}, {"chapter": "4力法"}],
        }
        success_protocol = _protocol("COARSE_CANDIDATES_FOUND")
        success_draft = build_a2_output_draft("search_image", state, success_protocol)
        success = finalize_output_draft(success_draft, success_protocol, 2, 2)
        self.assertEqual(success.message_key, "search.candidates.ready")
        self.assertEqual(success.protocol.code, "COARSE_CANDIDATES_FOUND")
        self.assertIn("2 道", success.text)

        partial_protocol = _protocol("RERANK_INCOMPLETE_COARSE_FALLBACK")
        partial_draft = build_a2_output_draft("search_image", state, partial_protocol)
        self.assertEqual(
            partial_draft.notice_keys,
            ("notice.rerank_coarse_fallback",),
        )
        partial = finalize_output_draft(partial_draft, partial_protocol, 2, 2)
        self.assertEqual(partial.protocol.status, RequestStatus.PARTIAL)
        self.assertEqual(partial.text.count("精排未完整完成"), 1)

    def test_candidate_media_is_atomic_when_any_image_is_missing(self):
        protocol = _protocol("COARSE_CANDIDATES_FOUND")
        draft = build_a2_output_draft(
            "search_image",
            {"phase": "WAIT_CANDIDATE_CHOICE", "candidate_count": 3},
            protocol,
        )
        public = finalize_output_draft(draft, protocol, 2, 3)

        self.assertEqual(public.message_key, "system.media.not_found")
        self.assertEqual(public.protocol.code, "MEDIA_NOT_FOUND")
        self.assertEqual(public.kind.value, "transport_error")
        self.assertNotIn("候选 2", public.text)

    def test_global_candidates_without_public_source_fail_closed(self):
        protocol = _protocol("GLOBAL_CANDIDATES_FOUND")
        draft = build_a2_output_draft(
            "global_search",
            {
                "phase": "WAIT_CANDIDATE_CHOICE",
                "candidate_count": 2,
                "candidates": [
                    {"chapter": "SECRET_UNKNOWN_CHAPTER"},
                    {"chapter": ""},
                ],
            },
            protocol,
        )
        public = finalize_output_draft(draft, protocol, 2, 2)

        self.assertEqual(draft.message_key, "system.service.unavailable")
        self.assertEqual(public.message_key, "system.service.unavailable")
        self.assertEqual(public.protocol.code, "SERVICE_UNAVAILABLE")
        self.assertNotIn("SECRET_UNKNOWN_CHAPTER", public.text)

    def test_a2_clarification_wins_over_stale_result_phase(self):
        cases = (
            (
                "QUESTION_INDEX_REQUIRED",
                {"phase": "WAIT_QUESTION_CHOICE", "question_count": 3},
            ),
            (
                "CANDIDATE_RANK_REQUIRED",
                {"phase": "WAIT_CANDIDATE_CHOICE", "candidate_count": 3},
            ),
            (
                "SELECTION_OUT_OF_RANGE",
                {"phase": "WAIT_CANDIDATE_CHOICE", "candidate_count": 3},
            ),
        )
        for code, state in cases:
            with self.subTest(code=code):
                protocol = _protocol(code)
                draft = build_a2_output_draft("clarification", state, protocol)
                public = finalize_output_draft(draft, protocol, 0, 0)
                self.assertEqual(draft.message_key, "search.clarification.required")
                self.assertEqual(public.message_key, "search.clarification.required")

        rejected_protocol = _protocol("ACTION_NOT_ALLOWED")
        rejected = build_a2_output_draft(
            "reject",
            {"phase": "WAIT_CANDIDATE_CHOICE", "candidate_count": 3},
            rejected_protocol,
        )
        rejected_public = finalize_output_draft(rejected, rejected_protocol, 0, 0)
        self.assertEqual(rejected_public.message_key, "conversation.action_rejected")

    def test_answer_delivery_zero_partial_and_full_are_distinct(self):
        marker = "SECRET_ANSWER_PATH_MARKER"
        state = {
            "phase": "ANSWERED",
            "selected_question": 2,
            "last_answer_paths": [f"C:/private/{marker}/1.png", f"C:/private/{marker}/2.png"],
            "last_error": marker,
        }
        protocol = _protocol("ANSWER_FILES_FOUND")
        draft = build_a2_output_draft("select_candidate", state, protocol, variant=marker)

        zero = finalize_output_draft(draft, protocol, 0, 2)
        self.assertEqual(zero.message_key, "system.media.not_found")
        self.assertEqual(zero.protocol.code, "MEDIA_NOT_FOUND")

        partial = finalize_output_draft(draft, protocol, 1, 2)
        self.assertEqual(partial.message_key, "search.answer.ready")
        self.assertEqual(partial.protocol.code, "MEDIA_PERSIST_FAILED")
        self.assertEqual(partial.protocol.status, RequestStatus.PARTIAL)
        self.assertIn("共 1 张", partial.text)
        self.assertEqual(partial.text.count("部分结果图片未能交付"), 1)
        self.assertIn(UserAction.RETRY_REQUEST, partial.allowed_actions)

        full = finalize_output_draft(draft, protocol, 2, 2)
        self.assertEqual(full.message_key, "search.answer.ready")
        self.assertEqual(full.protocol.code, "ANSWER_FILES_FOUND")
        self.assertIn("共 2 张", full.text)
        self.assertNotIn(marker, zero.text + partial.text + full.text)

    def test_a3_combines_parent_unit_with_child_candidates_without_text_parsing(self):
        marker = "SECRET_CHILD_TEXT_MARKER"
        protocol = _protocol("COARSE_CANDIDATES_FOUND")
        child = build_a2_output_draft(
            "search_image",
            {
                "phase": "WAIT_CANDIDATE_CHOICE",
                "candidate_count": 2,
                "legacy_text": marker,
            },
            protocol,
            variant=marker,
        )
        a3 = {
            "phase": "A2_ACTIVE",
            "remaining_count": 3,
            "selected_unit": {"unit_id": "u2", "display_label": "2-1"},
            "units": [
                {"unit_id": "u1", "page_index": 1, "display_label": "1"},
                {"unit_id": "u2", "page_index": 2, "display_label": "2-1", "selected": True},
            ],
            "old_text": marker,
        }
        draft = build_a3_output_draft(
            "select_question", a3, protocol, child_draft=child, variant=marker
        )
        public = finalize_output_draft(draft, protocol, 2, 2)

        self.assertEqual(draft.message_key, "page.unit.candidates.ready")
        self.assertEqual(draft.facts["question_label"], "2-1")
        self.assertEqual(draft.facts["page_index"], 2)
        self.assertIn("2-1", public.text)
        self.assertNotIn(marker, public.text)

    def test_a3_conflicting_selected_and_canonical_unit_binding_fails_closed(self):
        protocol = _protocol("COARSE_CANDIDATES_FOUND")
        child = build_a2_output_draft(
            "search_image",
            {"phase": "WAIT_CANDIDATE_CHOICE", "candidate_count": 2},
            protocol,
        )
        cases = (
            {"unit_id": "u2", "display_label": "1-1", "page_index": 2},
            {"unit_id": "u2", "display_label": "2-1", "page_index": 1},
        )
        for selected in cases:
            with self.subTest(selected=selected):
                draft = build_a3_output_draft(
                    "select_question",
                    {
                        "phase": "A2_ACTIVE",
                        "selected_unit": selected,
                        "units": [
                            {
                                "unit_id": "u2",
                                "display_label": "2-1",
                                "page_index": 2,
                                "selected": True,
                            }
                        ],
                    },
                    protocol,
                    child_draft=child,
                )
                public = finalize_output_draft(draft, protocol, 2, 2)

                self.assertEqual(draft.message_key, "system.service.unavailable")
                self.assertEqual(public.message_key, "system.service.unavailable")
                self.assertEqual(public.protocol.code, "SERVICE_UNAVAILABLE")

    def test_a3_child_media_without_unit_binding_fails_closed(self):
        candidate_protocol = _protocol("COARSE_CANDIDATES_FOUND")
        answer_protocol = _protocol("ANSWER_FILES_FOUND")
        cases = (
            (
                "candidate",
                candidate_protocol,
                build_a2_output_draft(
                    "search_image",
                    {"phase": "WAIT_CANDIDATE_CHOICE", "candidate_count": 2},
                    candidate_protocol,
                ),
            ),
            (
                "answer",
                answer_protocol,
                build_a2_output_draft(
                    "select_candidate",
                    {"phase": "ANSWERED", "last_answer_paths": ["one.png", "two.png"]},
                    answer_protocol,
                ),
            ),
        )
        for label, protocol, child in cases:
            with self.subTest(label=label):
                draft = build_a3_output_draft(
                    "select_question",
                    {"phase": "A2_ACTIVE", "remaining_count": 1},
                    protocol,
                    child_draft=child,
                )
                public = finalize_output_draft(draft, protocol, 2, 2)

                self.assertEqual(draft.message_key, "system.service.unavailable")
                self.assertEqual(public.message_key, "system.service.unavailable")
                self.assertEqual(public.protocol.code, "SERVICE_UNAVAILABLE")
                self.assertEqual(public.kind.value, "transport_error")

    def test_a3_answer_combination_reports_remaining_then_complete(self):
        protocol = _protocol("ANSWER_FILES_FOUND")
        child = build_a2_output_draft(
            "select_candidate",
            {"phase": "ANSWERED", "last_answer_paths": ["one.png", "two.png"]},
            protocol,
        )
        base = {
            "phase": "WAIT_UNIT_SELECTION",
            "selected_unit": {"unit_id": "u4", "display_label": "污染_MARKER"},
            "units": [{"unit_id": "u4", "page_index": 4, "display_label": "污染_MARKER"}],
        }

        remaining_draft = build_a3_output_draft(
            "select_candidate",
            {**base, "remaining_count": 2},
            protocol,
            child_draft=child,
        )
        remaining = finalize_output_draft(remaining_draft, protocol, 2, 2)
        self.assertEqual(
            remaining.message_key, "page.unit.answer.delivered_remaining"
        )
        self.assertIn("图片第 4 题", remaining.text)
        self.assertIn("还剩 2 道", remaining.text)
        self.assertNotIn("污染_MARKER", remaining.text)

        complete_draft = build_a3_output_draft(
            "select_candidate",
            {**base, "phase": "COMPLETE", "remaining_count": 0},
            protocol,
            child_draft=child,
        )
        complete = finalize_output_draft(complete_draft, protocol, 2, 2)
        self.assertEqual(
            complete.message_key, "page.unit.answer.delivered_complete"
        )
        self.assertIn("已经处理完成", complete.text)

    def test_a3_stopped_unit_uses_explicit_structured_context(self):
        protocol = _protocol("REQUEST_SUCCEEDED")
        draft = build_a3_output_draft(
            "a3_reselect",
            {
                "phase": "WAIT_UNIT_SELECTION",
                "remaining_count": 2,
                "selected_unit": {},
                "stopped_unit": {
                    "unit_id": "u3",
                    "display_label": "3-2",
                    "page_index": 3,
                },
            },
            protocol,
        )
        public = finalize_output_draft(draft, protocol, 0, 0)

        self.assertEqual(draft.message_key, "page.unit.stopped_remaining")
        self.assertIn("3-2", public.text)
        self.assertIn("还有 2 道", public.text)

    def test_a3_no_units_uses_registered_no_match_protocol(self):
        protocol = _protocol("PAGE_NO_SEARCHABLE_UNITS")
        draft = build_a3_output_draft(
            "a3_page_ready",
            {"phase": "COMPLETE", "units": [], "question_count": 0},
            protocol,
        )
        public = finalize_output_draft(draft, protocol, 0, 0)

        self.assertEqual(public.message_key, "page.no_units")
        self.assertEqual(public.protocol.status, RequestStatus.NO_MATCH)
        self.assertIn(UserAction.RETRY_UPLOAD, public.allowed_actions)

    def test_a3_reuses_fixed_a1_triage_outputs(self):
        no_load_protocol = _protocol("EXTERNAL_LOAD_NOT_FOUND")
        no_load_draft = build_a3_output_draft(
            "image_triage_stop",
            {"phase": "COMPLETE", "old_text": "SECRET_DO_NOT_ECHO"},
            no_load_protocol,
            variant="SECRET_DO_NOT_ECHO",
        )
        no_load = finalize_output_draft(no_load_draft, no_load_protocol, 0, 0)
        self.assertEqual(no_load.message_key, "triage.a1.no_external_load")
        self.assertNotIn("SECRET_DO_NOT_ECHO", no_load.text)

        stopped_protocol = _protocol("TRIAGE_A1_STOPPED")
        stopped_draft = build_a3_output_draft(
            "image_triage_stop",
            {"phase": "COMPLETE"},
            stopped_protocol,
            variant="structure_incomplete",
        )
        stopped = finalize_output_draft(stopped_draft, stopped_protocol, 0, 0)
        self.assertEqual(stopped.message_key, "triage.a1.reasoned")
        self.assertIn("结构和支座", stopped.text)

        a3_reupload_protocol = _protocol("TRIAGE_A3_REQUIRES_REUPLOAD")
        a3_reupload_draft = build_a3_output_draft(
            "image_triage_stop",
            {"phase": "COMPLETE"},
            a3_reupload_protocol,
            variant="fallback",
        )
        a3_reupload = finalize_output_draft(
            a3_reupload_draft, a3_reupload_protocol, 0, 0
        )
        self.assertEqual(a3_reupload.message_key, "triage.a1.fallback")
        self.assertEqual(a3_reupload.protocol.code, "TRIAGE_A1_STOPPED")

    def test_a3_prepare_without_loaded_state_does_not_invent_a_count(self):
        protocol = _protocol("CLARIFICATION_REQUIRED")
        draft = build_a3_output_draft(
            "a3_prepare_required",
            {"phase": "IDLE", "units": []},
            protocol,
        )
        public = finalize_output_draft(draft, protocol, 0, 0)

        self.assertEqual(draft.message_key, "page.current.guidance")
        self.assertNotIn("question_count", draft.facts)
        self.assertEqual(public.message_key, "page.current.guidance")

    def test_a3_single_unit_page_ready_enters_crop_guidance(self):
        protocol = _protocol("CLARIFICATION_REQUIRED")
        draft = build_a3_output_draft(
            "a3_page_ready",
            {
                "phase": "CROP_REQUIRED",
                "remaining_count": 1,
                "selected_unit": {"unit_id": "u1", "display_label": "1"},
                "units": [
                    {
                        "unit_id": "u1",
                        "page_index": 1,
                        "display_label": "1",
                        "selected": True,
                    }
                ],
            },
            protocol,
        )
        public = finalize_output_draft(draft, protocol, 0, 0)

        self.assertEqual(draft.message_key, "page.crop.required")
        self.assertEqual(draft.facts["page_index"], 1)
        self.assertEqual(public.message_key, "page.crop.required")
        self.assertIn(UserAction.CROP_QUESTION, public.allowed_actions)

    def test_a3_switching_units_keeps_previous_stop_fact(self):
        protocol = _protocol("CLARIFICATION_REQUIRED")
        draft = build_a3_output_draft(
            "a3_unit_selected",
            {
                "phase": "CROP_REQUIRED",
                "selected_unit": {"unit_id": "u2", "display_label": "2-1"},
                "previous_selected_unit": {
                    "unit_id": "u1",
                    "display_label": "1-1",
                },
            },
            protocol,
        )
        public = finalize_output_draft(draft, protocol, 0, 0)

        self.assertEqual(draft.message_key, "page.crop.required")
        self.assertIn("已停止1-1，现在处理2-1", public.text)

    def test_load_route_review_is_normalized_before_render(self):
        protocol = RequestProtocol.from_code(
            "LOAD_ROUTE_NEEDS_REVIEW",
            request_id="req-integration-002",
            search_id="search-integration-002",
        )
        draft = build_a2_output_draft(
            "clarification",
            {"phase": "WAIT_CHAPTER"},
            protocol,
        )
        public = finalize_output_draft(draft, protocol, 0, 0)

        self.assertEqual(public.message_key, "search.clarification.required")
        self.assertEqual(public.protocol.code, "CLARIFICATION_REQUIRED")
        self.assertIn(UserAction.CHANGE_CHAPTER, public.allowed_actions)

    def test_nonretryable_bank_route_failure_does_not_offer_retry_search(self):
        protocol = _protocol("BANK_ROUTE_FAILED")
        draft = build_a2_output_draft(
            "search_image",
            {"phase": "ERROR", "last_error": "do-not-echo"},
            protocol,
        )
        public = finalize_output_draft(draft, protocol, 0, 0)

        self.assertEqual(public.message_key, "search.failed.nonretryable")
        self.assertEqual(public.allowed_actions, (UserAction.UPLOAD_IMAGE,))
        self.assertNotIn(UserAction.RETRY_SEARCH, public.allowed_actions)

    def test_non_media_draft_rejects_unexpected_media_evidence(self):
        protocol = _protocol("REQUEST_SUCCEEDED")
        draft = build_a2_output_draft(
            "safe_answer", {"phase": "IDLE"}, protocol, variant="general"
        )
        public = finalize_output_draft(draft, protocol, 1, 1)

        self.assertEqual(public.message_key, "system.service.unavailable")
        self.assertEqual(public.protocol.code, "SERVICE_UNAVAILABLE")
        self.assertEqual(public.kind, FinalOutputKind.TRANSPORT_ERROR.value)


if __name__ == "__main__":
    unittest.main()
