import json
import logging
from dataclasses import replace
import unittest

import tiku_agent.user_output as user_output_module
from tiku_agent.user_output import (
    FinalOutputKind,
    FinalOutputRequestV1,
    OutputContractError,
    OutputKind,
    ProgressOutputRequestV1,
    PublicContactV1,
    PublicMessageV1,
    UserAction,
    catalog_message_keys,
    notice_keys,
    progress_message_keys,
    render_final_output,
    render_progress_output,
    validate_catalog_configuration,
)
from tiku_shared.request_protocol import (
    RequestAction,
    RequestLayer,
    RequestProtocol,
    RequestStatus,
)


REQUEST_ID = "req_12345678"
SEARCH_ID = "search_12345678"


def dynamic_protocol(
    code,
    status,
    *,
    layer=RequestLayer.TOOL,
    retryable=False,
    action=RequestAction.NONE,
):
    return RequestProtocol(
        status=status,
        layer=layer,
        code=code,
        retryable=retryable,
        action=action,
        request_id=REQUEST_ID,
        search_id=SEARCH_ID,
    )


def final_request(
    message_key,
    protocol,
    *,
    facts=None,
    actions=(),
    notices=(),
    kind=FinalOutputKind.RESULT,
    phase=None,
    schema_version=1,
    contact=None,
):
    if phase is None:
        if message_key in {
            "conversation.current",
            "conversation.action_rejected",
        }:
            phase = "WAIT_CANDIDATE_CHOICE"
        elif message_key.startswith("conversation."):
            phase = "IDLE"
        elif message_key.startswith("triage."):
            phase = "PROCESSING"
        elif message_key == "search.cancelled":
            phase = "CANCELLED"
        elif message_key == "search.chapter.saved":
            phase = "READY_TO_ROUTE"
        elif message_key in {"search.chapter.required", "search.chapter.unsupported"}:
            phase = "WAIT_CHAPTER"
        elif message_key == "search.clarification.required":
            phase = "WAIT_CANDIDATE_CHOICE"
        elif message_key == "search.questions.ready":
            phase = "WAIT_QUESTION_CHOICE"
        elif message_key in {
            "search.candidates.ready",
            "search.global.candidates.ready",
            "search.candidates.rejected",
            "search.candidates.unavailable",
            "search.candidates.recalled",
            "search.candidates.rejected_more",
            "search.answer.missing",
        }:
            phase = "WAIT_CANDIDATE_CHOICE"
        elif message_key.startswith("search.no_match"):
            phase = "NO_MATCH"
        elif message_key in {
            "search.answer.ready",
            "search.answer.mismatch",
            "search.answer.resent",
        }:
            phase = "ANSWERED"
        elif message_key in {"search.failed.retryable", "search.failed.nonretryable"}:
            phase = "ERROR"
        elif message_key in {
            "page.selection.required",
            "page.units.prepared",
            "page.cancel.scope_required.page",
            "page.unit.cancelled_remaining",
            "page.unit.stopped_remaining",
            "page.unit.answer.delivered_remaining",
            "page.stale.selection",
            "page.current.guidance",
        }:
            phase = "WAIT_UNIT_SELECTION"
        elif message_key.startswith("page.crop"):
            phase = "CROP_REQUIRED"
        elif message_key in {
            "page.namespace.clarification",
            "page.cancel.scope_required.current",
            "page.unit.candidates.ready",
            "page.stale.candidate",
        }:
            phase = "A2_ACTIVE"
        elif message_key in {
            "page.no_units",
            "page.unit.stopped_complete",
            "page.unit.answer.delivered_complete",
            "page.completed",
            "page.ended",
        }:
            phase = "COMPLETE"
        elif message_key == "page.session.reset":
            phase = "IDLE"
        elif message_key == "page.failed.retryable":
            phase = "ERROR"
        else:
            phase = "IDLE"
    return FinalOutputRequestV1(
        schema_version=schema_version,
        kind=kind,
        message_key=message_key,
        protocol=protocol,
        phase=phase,
        facts=dict(facts or {}),
        allowed_actions=tuple(actions),
        notice_keys=tuple(notices),
        contact=contact,
    )


class UserOutputAssertions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output_logger = logging.getLogger("tiku_agent.user_output")
        cls.old_level = cls.output_logger.level
        cls.output_logger.setLevel(logging.CRITICAL + 1)

    @classmethod
    def tearDownClass(cls):
        cls.output_logger.setLevel(cls.old_level)

    def assert_service_fallback(self, output):
        self.assertEqual(output.message_key, "system.service.unavailable")
        self.assertEqual(output.kind, OutputKind.TRANSPORT_ERROR)
        self.assertEqual(output.protocol.status, RequestStatus.ERROR)
        self.assertEqual(output.protocol.code, "SERVICE_UNAVAILABLE")
        self.assertEqual(output.protocol.action, RequestAction.RETRY_REQUEST)
        self.assertEqual(output.allowed_actions, (UserAction.RETRY_REQUEST,))

    def assert_not_public(self, output, marker):
        serialized = json.dumps(output.to_dict(), ensure_ascii=False, sort_keys=True)
        self.assertNotIn(marker, serialized)


class FinalOutputContractTest(UserOutputAssertions):
    def test_dynamic_candidate_success_renders_canonical_payload(self):
        protocol = dynamic_protocol(
            "RERANK_COMPLETED", RequestStatus.SUCCESS
        )
        output = render_final_output(
            final_request(
                "search.candidates.ready",
                protocol,
                facts={"candidate_count": 3},
                actions=(UserAction.SELECT_CANDIDATE,),
            )
        )

        self.assertEqual(output.message_key, "search.candidates.ready")
        self.assertEqual(output.protocol, protocol)
        self.assertIn("3", output.text)
        self.assertEqual(output.allowed_actions, (UserAction.SELECT_CANDIDATE,))
        payload = output.to_dict()
        self.assertEqual(payload["status"], "SUCCESS")
        self.assertEqual(payload["code"], "RERANK_COMPLETED")
        self.assertNotIn("protocol", payload)
        for private_field in (
            "facts",
            "phase",
            "bounded_text",
            "notice_keys",
            "reply_source",
            "fallback_reason",
        ):
            self.assertNotIn(private_field, payload)
        self.assertEqual(output.to_stream_event()["data"], payload)

    def test_history_error_phase_does_not_change_success_status(self):
        output = render_final_output(
            final_request(
                "conversation.greeting",
                RequestProtocol.from_code(
                    "REQUEST_SUCCEEDED",
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                ),
                phase="ERROR",
                actions=(UserAction.UPLOAD_IMAGE,),
            )
        )

        self.assertEqual(output.message_key, "conversation.greeting")
        self.assertEqual(output.protocol.status, RequestStatus.SUCCESS)

    def test_registered_protocol_field_tampering_fails_closed(self):
        base = RequestProtocol.from_code(
            "QUEUE_FULL", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        variants = (
            replace(base, status=RequestStatus.SUCCESS),
            replace(base, layer=RequestLayer.TOOL),
            replace(base, retryable=False),
            replace(base, action=RequestAction.RETRY_SEARCH),
        )
        for protocol in variants:
            with self.subTest(protocol=protocol):
                output = render_final_output(
                    final_request(
                        "system.queue.full",
                        protocol,
                        kind=FinalOutputKind.TRANSPORT_ERROR,
                        actions=(UserAction.RETRY_REQUEST,),
                    )
                )
                self.assert_service_fallback(output)

    def test_dynamic_protocol_field_tampering_fails_closed(self):
        base = dynamic_protocol("RERANK_COMPLETED", RequestStatus.SUCCESS)
        variants = (
            replace(base, status=RequestStatus.ERROR),
            replace(base, layer=RequestLayer.NETWORK),
            replace(base, retryable=True),
            replace(base, action=RequestAction.RETRY_SEARCH),
        )
        for protocol in variants:
            with self.subTest(protocol=protocol):
                output = render_final_output(
                    final_request(
                        "search.candidates.ready",
                        protocol,
                        facts={"candidate_count": 2},
                        actions=(UserAction.SELECT_CANDIDATE, UserAction.RETRY_SEARCH),
                    )
                )
                self.assert_service_fallback(output)

    def test_unknown_protocol_code_fails_closed(self):
        protocol = dynamic_protocol("UNKNOWN_STABLE_CODE", RequestStatus.SUCCESS)
        output = render_final_output(
            final_request(
                "search.candidates.ready",
                protocol,
                facts={"candidate_count": 2},
                actions=(UserAction.SELECT_CANDIDATE,),
            )
        )
        self.assert_service_fallback(output)

    def test_unknown_message_key_uses_valid_status_fallback(self):
        marker = "LEAK_X9"
        protocol = RequestProtocol.from_code(
            "NO_MATCH", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        with self.assertLogs("tiku_agent.user_output", level="WARNING") as logs:
            output = render_final_output(
                final_request(
                    f"attack.{marker}",
                    protocol,
                    facts={"detail": marker},
                    actions=(UserAction.CHANGE_CHAPTER,),
                )
            )

        self.assertEqual(output.message_key, "fallback.no_match")
        self.assertEqual(output.protocol, protocol)
        self.assert_not_public(output, marker)
        self.assertIn("category=message_key_unknown", " ".join(logs.output))
        self.assertNotIn(marker, " ".join(logs.output))

    def test_message_code_mismatch_cannot_borrow_entry_actions(self):
        protocol = RequestProtocol.from_code(
            "CHAPTER_REQUIRED", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        output = render_final_output(
            final_request(
                "search.no_match.chapter",
                protocol,
                facts={"chapter_name": "力法"},
                actions=(UserAction.CHANGE_CHAPTER,),
            )
        )

        self.assertEqual(output.message_key, "fallback.needs_input")
        self.assertEqual(output.allowed_actions, (UserAction.CHANGE_CHAPTER,))

        success = RequestProtocol.from_code(
            "REQUEST_SUCCEEDED", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        for message_key, action in (
            ("system.session.expired", UserAction.NEW_CHAT),
            ("system.login.required", UserAction.RELOGIN),
            ("page.cancel.scope_required.page", UserAction.FINISH_PAGE),
            ("search.candidates.ready", UserAction.SELECT_CANDIDATE),
        ):
            with self.subTest(message_key=message_key, action=action):
                self.assert_service_fallback(
                    render_final_output(
                        final_request(
                            message_key,
                            success,
                            actions=(action,),
                            phase="IDLE",
                        )
                    )
                )

    def test_extra_facts_are_rejected_without_reflection(self):
        marker = "LEAK_X9"
        vectors = (
            final_request(
                "search.no_match.chapter",
                RequestProtocol.from_code(
                    "NO_MATCH", request_id=REQUEST_ID, search_id=SEARCH_ID
                ),
                facts={"chapter_name": "力法", "error": f"RuntimeError: {marker}"},
                actions=(UserAction.CHANGE_CHAPTER,),
            ),
            final_request(
                "search.chapter.required",
                RequestProtocol.from_code(
                    "CHAPTER_REQUIRED", request_id=REQUEST_ID, search_id=SEARCH_ID
                ),
                facts={"detail": f"C:\\private\\{marker}.jpg"},
                actions=(UserAction.CHANGE_CHAPTER,),
            ),
            final_request(
                "search.failed.retryable",
                RequestProtocol.from_code(
                    "TOOL_FAILED", request_id=REQUEST_ID, search_id=SEARCH_ID
                ),
                facts={
                    "active_image_preserved": True,
                    "message": f"Authorization: Bearer {marker}",
                },
                actions=(UserAction.RETRY_SEARCH,),
            ),
            final_request(
                "search.candidates.ready",
                dynamic_protocol(
                    "RERANK_INCOMPLETE_COARSE_FALLBACK",
                    RequestStatus.PARTIAL,
                    retryable=True,
                ),
                facts={"candidate_count": 2, "reason": f"/srv/app/{marker}.json"},
                actions=(UserAction.SELECT_CANDIDATE, UserAction.RETRY_SEARCH),
            ),
        )
        for request in vectors:
            with self.subTest(message_key=request.message_key):
                output = render_final_output(request)
                self.assert_not_public(output, marker)
                self.assertNotEqual(output.message_key, request.message_key)

    def test_pollution_in_every_free_form_position_is_not_public(self):
        marker = "LEAK_X9"
        base = RequestProtocol.from_code(
            "REQUEST_SUCCEEDED", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        vectors = (
            final_request(
                f"attack.{marker}", base, phase="READY_FOR_SEARCH"
            ),
            final_request(
                "conversation.greeting", base, phase=f"ERROR_{marker}"
            ),
            final_request(
                "conversation.greeting", base, actions=(f"retry_{marker}",)
            ),
            final_request(
                "conversation.greeting", base, notices=(f"notice.{marker}",)
            ),
        )
        for request in vectors:
            with self.subTest(request=request):
                self.assert_not_public(render_final_output(request), marker)

    def test_protocol_action_must_be_in_allowed_actions(self):
        protocol = RequestProtocol.from_code(
            "NO_MATCH", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        output = render_final_output(
            final_request(
                "search.no_match.chapter",
                protocol,
                facts={"chapter_name": "力法"},
                actions=(UserAction.RETRY_UPLOAD,),
            )
        )
        self.assert_service_fallback(output)

    def test_catalog_mentioned_action_must_be_allowed(self):
        output = render_final_output(
            final_request(
                "search.candidates.ready",
                dynamic_protocol("RERANK_COMPLETED", RequestStatus.SUCCESS),
                facts={"candidate_count": 2},
                actions=(),
            )
        )
        self.assert_service_fallback(output)

    def test_retryable_protocol_requires_retry_action(self):
        protocol = dynamic_protocol(
            "RERANK_INCOMPLETE_COARSE_FALLBACK",
            RequestStatus.PARTIAL,
            retryable=True,
        )
        output = render_final_output(
            final_request(
                "search.candidates.ready",
                protocol,
                facts={"candidate_count": 2},
                actions=(UserAction.SELECT_CANDIDATE,),
            )
        )
        self.assert_service_fallback(output)

    def test_terminal_quota_is_the_explicit_needs_input_without_action_exception(self):
        protocol = RequestProtocol.from_code(
            "GLOBAL_DAILY_QUOTA_EXCEEDED",
            request_id=REQUEST_ID,
            search_id=SEARCH_ID,
        )
        output = render_final_output(
            final_request(
                "system.quota.unavailable",
                protocol,
                kind=FinalOutputKind.TRANSPORT_ERROR,
            )
        )
        self.assertEqual(output.message_key, "system.quota.unavailable")
        self.assertEqual(output.allowed_actions, ())
        self.assertNotIn("重试", output.text)

        invalid = render_final_output(
            final_request(
                "system.quota.unavailable",
                protocol,
                kind=FinalOutputKind.TRANSPORT_ERROR,
                actions=(UserAction.RETRY_REQUEST,),
            )
        )
        self.assert_service_fallback(invalid)

    def test_nonterminal_needs_input_without_action_fails_closed(self):
        protocol = dynamic_protocol("UNKNOWN_CHAPTER", RequestStatus.NEEDS_INPUT)
        output = render_final_output(
            final_request(
                "search.chapter.unsupported",
                protocol,
                facts={"chapter_name": "未知题型"},
                actions=(),
            )
        )
        self.assert_service_fallback(output)

    def test_kind_and_schema_mismatches_fail_closed(self):
        queue_protocol = RequestProtocol.from_code(
            "QUEUE_FULL", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        vectors = (
            final_request(
                "system.queue.full",
                queue_protocol,
                actions=(UserAction.RETRY_REQUEST,),
                kind=FinalOutputKind.RESULT,
            ),
            final_request(
                "conversation.greeting",
                RequestProtocol.from_code(
                    "REQUEST_SUCCEEDED", request_id=REQUEST_ID, search_id=SEARCH_ID
                ),
                schema_version=2,
            ),
        )
        for request in vectors:
            with self.subTest(request=request):
                self.assert_service_fallback(render_final_output(request))

    def test_business_message_rejects_wrong_known_phase(self):
        output = render_final_output(
            final_request(
                "search.answer.ready",
                dynamic_protocol("ANSWER_FILES_FOUND", RequestStatus.SUCCESS),
                facts={"delivered_image_count": 1},
                phase="WAIT_CHAPTER",
            )
        )
        self.assert_service_fallback(output)

    def test_sensitive_protocol_ids_are_never_public(self):
        marker = "LEAKX9"
        protocols = (
            RequestProtocol(
                RequestStatus.SUCCESS,
                RequestLayer.TOOL,
                "REQUEST_SUCCEEDED",
                request_id=f"sk-{marker}abcdefghijklmnop",
                search_id=SEARCH_ID,
            ),
            RequestProtocol(
                RequestStatus.SUCCESS,
                RequestLayer.TOOL,
                "REQUEST_SUCCEEDED",
                request_id=REQUEST_ID,
                search_id=f"token_{marker}abcdef",
            ),
        )
        for protocol in protocols:
            with self.subTest(protocol=protocol):
                output = render_final_output(
                    final_request(
                        "conversation.greeting",
                        protocol,
                        actions=(UserAction.UPLOAD_IMAGE,),
                    )
                )
                self.assert_service_fallback(output)
                self.assert_not_public(output, marker)

    def test_catalog_rejects_unrelated_extra_actions(self):
        output = render_final_output(
            final_request(
                "conversation.greeting",
                RequestProtocol.from_code(
                    "REQUEST_SUCCEEDED", request_id=REQUEST_ID, search_id=SEARCH_ID
                ),
                actions=(UserAction.UPLOAD_IMAGE, UserAction.RETRY_FEEDBACK),
            )
        )
        self.assert_service_fallback(output)

    def test_fallback_does_not_publish_unproven_actions(self):
        protocol = RequestProtocol.from_code(
            "REQUEST_SUCCEEDED", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        unknown = render_final_output(
            final_request(
                "unknown.safe.key",
                protocol,
                actions=(UserAction.RETRY_FEEDBACK,),
            )
        )
        self.assert_service_fallback(unknown)

        marker = "LEAKX9"
        invalid_known = render_final_output(
            final_request(
                "conversation.greeting",
                protocol,
                facts={"extra": marker},
                actions=(UserAction.UPLOAD_IMAGE, UserAction.RETRY_FEEDBACK),
            )
        )
        self.assert_service_fallback(invalid_known)
        self.assert_not_public(invalid_known, marker)

    def test_protocol_variant_narrows_retry_actions(self):
        invalid_vectors = (
            final_request(
                "search.answer.ready",
                dynamic_protocol("ANSWER_FILES_FOUND", RequestStatus.SUCCESS),
                facts={"delivered_image_count": 1},
                actions=(UserAction.RETRY_REQUEST,),
            ),
            final_request(
                "search.candidates.ready",
                dynamic_protocol("RERANK_COMPLETED", RequestStatus.SUCCESS),
                facts={"candidate_count": 2},
                actions=(UserAction.SELECT_CANDIDATE, UserAction.RETRY_SEARCH),
            ),
        )
        for request in invalid_vectors:
            with self.subTest(request=request):
                self.assert_service_fallback(render_final_output(request))

        valid_partial = render_final_output(
            final_request(
                "search.candidates.ready",
                dynamic_protocol(
                    "RERANK_INCOMPLETE_COARSE_FALLBACK",
                    RequestStatus.PARTIAL,
                    retryable=True,
                ),
                facts={"candidate_count": 2},
                actions=(UserAction.SELECT_CANDIDATE, UserAction.RETRY_SEARCH),
                notices=("notice.rerank_coarse_fallback",),
            )
        )
        self.assertEqual(valid_partial.message_key, "search.candidates.ready")

    def test_only_public_chapter_display_names_may_render(self):
        marker = "4力法"
        vectors = (
            final_request(
                "search.no_match.chapter",
                RequestProtocol.from_code(
                    "NO_MATCH", request_id=REQUEST_ID, search_id=SEARCH_ID
                ),
                facts={"chapter_name": marker},
                actions=(UserAction.CHANGE_CHAPTER,),
            ),
            final_request(
                "search.chapter.required",
                RequestProtocol.from_code(
                    "CHAPTER_REQUIRED", request_id=REQUEST_ID, search_id=SEARCH_ID
                ),
                facts={"supported_chapters": [marker]},
                actions=(UserAction.CHANGE_CHAPTER,),
            ),
            final_request(
                "search.global.candidates.ready",
                dynamic_protocol("GLOBAL_CANDIDATES_FOUND", RequestStatus.SUCCESS),
                facts={"candidate_count": 1, "source_chapters": [marker]},
                actions=(UserAction.SELECT_CANDIDATE,),
            ),
        )
        for request in vectors:
            with self.subTest(request=request):
                self.assert_not_public(render_final_output(request), marker)

    def test_schema_version_requires_an_actual_integer(self):
        base_protocol = RequestProtocol.from_code(
            "REQUEST_SUCCEEDED", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        valid_public = render_final_output(
            final_request(
                "conversation.greeting",
                base_protocol,
                actions=(UserAction.UPLOAD_IMAGE,),
            )
        )
        for version in (True, 1.0):
            with self.subTest(scope="final", version=version):
                self.assert_service_fallback(
                    render_final_output(
                        final_request(
                            "conversation.greeting",
                            base_protocol,
                            actions=(UserAction.UPLOAD_IMAGE,),
                            schema_version=version,
                        )
                    )
                )
            with self.subTest(scope="protocol", version=version):
                # RequestProtocol rejects malformed schema versions at its
                # construction boundary, before a renderer can receive one.
                with self.assertRaises(ValueError):
                    replace(base_protocol, schema_version=version)
            with self.subTest(scope="public", version=version):
                with self.assertRaises(ValueError):
                    replace(
                        valid_public,
                        schema_version=version,
                        _factory_token=user_output_module._PUBLIC_MESSAGE_FACTORY_TOKEN,
                    )

    def test_public_message_is_renderer_only(self):
        protocol = RequestProtocol.from_code(
            "REQUEST_SUCCEEDED", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        with self.assertRaises(ValueError):
            PublicMessageV1(
                schema_version=1,
                kind=OutputKind.RESULT,
                message_key="conversation.greeting",
                text="安全回复。",
                protocol=protocol,
                allowed_actions=(UserAction.UPLOAD_IMAGE,),
                request_id=REQUEST_ID,
                search_id=SEARCH_ID,
            )

        rendered = render_final_output(
            final_request(
                "conversation.greeting",
                protocol,
                actions=(UserAction.UPLOAD_IMAGE,),
            )
        )
        with self.assertRaises(ValueError):
            replace(rendered, text="绕过渲染器的公开文本。")

    def test_triage_rejects_unrelated_known_phase(self):
        output = render_final_output(
            final_request(
                "triage.a1.no_external_load",
                RequestProtocol.from_code(
                    "TRIAGE_A1_STOPPED", request_id=REQUEST_ID, search_id=SEARCH_ID
                ),
                actions=(UserAction.RETRY_UPLOAD,),
                phase="WAIT_CHAPTER",
            )
        )
        self.assert_service_fallback(output)

    def test_unknown_but_well_formed_phase_is_rejected(self):
        marker = "BANANA_STATE"
        output = render_final_output(
            final_request(
                "conversation.greeting",
                RequestProtocol.from_code(
                    "REQUEST_SUCCEEDED", request_id=REQUEST_ID, search_id=SEARCH_ID
                ),
                phase=marker,
                actions=(UserAction.UPLOAD_IMAGE,),
            )
        )
        self.assert_service_fallback(output)
        self.assert_not_public(output, marker)

    def test_allowed_action_facts_are_authoritative(self):
        protocol = dynamic_protocol("RERANK_COMPLETED", RequestStatus.SUCCESS)
        invalid_vectors = (
            ({"candidate_count": 2}, UserAction.CONTINUE_SEARCH),
            ({"candidate_count": 2}, UserAction.GLOBAL_SEARCH),
        )
        for facts, action in invalid_vectors:
            with self.subTest(action=action):
                output = render_final_output(
                    final_request(
                        "search.candidates.ready",
                        protocol,
                        facts=facts,
                        actions=(UserAction.SELECT_CANDIDATE, action),
                    )
                )
                self.assert_service_fallback(output)

        output = render_final_output(
            final_request(
                "search.candidates.ready",
                protocol,
                facts={"candidate_count": 2, "continuation_available": True},
                actions=(UserAction.SELECT_CANDIDATE, UserAction.CONTINUE_SEARCH),
            )
        )
        self.assertEqual(output.message_key, "search.candidates.ready")

    def test_contact_requires_flag_action_and_bounded_structure(self):
        protocol = RequestProtocol.from_code(
            "NO_MATCH", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        contact = PublicContactV1("联系作者", "微信", "jglxfd6666")
        output = render_final_output(
            final_request(
                "search.no_match.chapter",
                protocol,
                facts={"chapter_name": "力法", "author_contact_available": True},
                actions=(UserAction.CHANGE_CHAPTER, UserAction.CONTACT_AUTHOR),
                contact=contact,
            )
        )
        self.assertEqual(output.to_dict()["contact"]["value"], "jglxfd6666")

        invalid = render_final_output(
            final_request(
                "search.no_match.chapter",
                protocol,
                facts={"chapter_name": "力法", "author_contact_available": True},
                actions=(UserAction.CHANGE_CHAPTER,),
                contact=contact,
            )
        )
        self.assert_service_fallback(invalid)

        marker = "LEAK_X9"
        invalid = render_final_output(
            final_request(
                "search.no_match.chapter",
                protocol,
                facts={"chapter_name": "力法", "author_contact_available": True},
                actions=(UserAction.CHANGE_CHAPTER, UserAction.CONTACT_AUTHOR),
                contact=PublicContactV1("联系作者", "微信", f"https://{marker}.example"),
            )
        )
        self.assert_service_fallback(invalid)
        self.assert_not_public(invalid, marker)


class ResultEvidenceContractTest(UserOutputAssertions):
    def test_answer_text_requires_at_least_one_delivered_image(self):
        protocol = dynamic_protocol("ANSWER_FILES_FOUND", RequestStatus.SUCCESS)
        for facts in (
            {},
            {"delivered_image_count": 0},
            {"delivered_image_count": False},
            {"delivered_image_count": -1},
        ):
            with self.subTest(facts=facts):
                output = render_final_output(
                    final_request("search.answer.ready", protocol, facts=facts)
                )
                self.assert_service_fallback(output)
                self.assertNotIn("答案图片已发出", output.text)

        poisoned = render_final_output(
            final_request(
                "search.answer.ready",
                protocol,
                facts={"delivered_image_count": 1, "detail": "LEAK_X9"},
            )
        )
        self.assert_service_fallback(poisoned)
        self.assert_not_public(poisoned, "LEAK_X9")

        output = render_final_output(
            final_request(
                "search.answer.ready",
                protocol,
                facts={"delivered_image_count": 2},
            )
        )
        self.assertEqual(output.message_key, "search.answer.ready")
        self.assertIn("共 2 张", output.text)

    def test_media_partial_requires_real_delivery(self):
        protocol = RequestProtocol.from_code(
            "MEDIA_PERSIST_FAILED", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        actions = (UserAction.RETRY_REQUEST,)
        valid = render_final_output(
            final_request(
                "search.answer.ready",
                protocol,
                facts={"delivered_image_count": 1},
                actions=actions,
                notices=("notice.media_partial",),
            )
        )
        self.assertEqual(valid.protocol.status, RequestStatus.PARTIAL)
        self.assertIn("部分结果图片未能交付", valid.text)

        invalid = render_final_output(
            final_request(
                "search.answer.ready",
                protocol,
                facts={"delivered_image_count": 0},
                actions=actions,
                notices=("notice.media_partial",),
            )
        )
        self.assert_service_fallback(invalid)

    def test_answer_missing_is_valid_no_match_and_never_claims_delivery(self):
        protocol = dynamic_protocol("ANSWER_FILES_NOT_FOUND", RequestStatus.NO_MATCH)
        output = render_final_output(
            final_request(
                "search.answer.missing",
                protocol,
                actions=(UserAction.SHOW_CANDIDATES,),
            )
        )
        self.assertEqual(output.protocol.status, RequestStatus.NO_MATCH)
        self.assertNotIn("已发出", output.text)

    def test_partial_candidates_require_positive_candidate_count(self):
        protocol = dynamic_protocol(
            "RERANK_INCOMPLETE_COARSE_FALLBACK",
            RequestStatus.PARTIAL,
            retryable=True,
        )
        actions = (UserAction.SELECT_CANDIDATE, UserAction.RETRY_SEARCH)
        for count in (0, False):
            with self.subTest(count=count):
                output = render_final_output(
                    final_request(
                        "search.candidates.ready",
                        protocol,
                        facts={"candidate_count": count},
                        actions=actions,
                    )
                )
                self.assert_service_fallback(output)

        output = render_final_output(
            final_request(
                "search.candidates.ready",
                protocol,
                facts={"candidate_count": 2},
                actions=actions,
                notices=("notice.rerank_coarse_fallback",),
            )
        )
        self.assertEqual(output.protocol.status, RequestStatus.PARTIAL)
        self.assertEqual(output.message_key, "search.candidates.ready")

    def test_page_unit_counts_must_partition_identified_questions(self):
        protocol = dynamic_protocol("QUESTION_UNITS_PREPARED", RequestStatus.SUCCESS)
        valid = render_final_output(
            final_request(
                "page.units.prepared",
                protocol,
                facts={"question_count": 3, "ready_count": 2, "manual_count": 1},
                actions=(UserAction.SELECT_QUESTION,),
            )
        )
        self.assertEqual(valid.message_key, "page.units.prepared")

        for facts in (
            {"question_count": 3, "ready_count": 2, "manual_count": 0},
            {"question_count": 0, "ready_count": 0, "manual_count": 0},
            {"question_count": 2, "ready_count": 3, "manual_count": 0},
        ):
            with self.subTest(facts=facts):
                self.assert_service_fallback(
                    render_final_output(
                        final_request(
                            "page.units.prepared",
                            protocol,
                            facts=facts,
                            actions=(UserAction.SELECT_QUESTION,),
                        )
                    )
                )

    def test_page_partial_crop_has_usable_units_and_retry_action(self):
        protocol = dynamic_protocol(
            "MULTI_CROPS_UNAVAILABLE", RequestStatus.PARTIAL, retryable=True
        )
        output = render_final_output(
            final_request(
                "page.units.prepared",
                protocol,
                facts={"question_count": 3, "ready_count": 2, "manual_count": 1},
                actions=(UserAction.SELECT_QUESTION, UserAction.RETRY_CURRENT_STAGE),
                notices=("notice.multi_crop_partial",),
            )
        )
        self.assertEqual(output.protocol.status, RequestStatus.PARTIAL)
        self.assertIn("手动裁剪", output.text)


class ExpandedSemanticCatalogTest(UserOutputAssertions):
    def success_protocol(self):
        return RequestProtocol.from_code(
            "REQUEST_SUCCEEDED", request_id=REQUEST_ID, search_id=SEARCH_ID
        )

    def test_common_conversation_families_use_fixed_registered_copy(self):
        success_cases = {
            "conversation.courtesy": "不客气",
            "conversation.farewell": "需要时",
            "conversation.identity": "我是力答",
            "conversation.capability": "结构力学题图",
            "conversation.supported_chapters": "静定结构受力",
            "conversation.workflow": "候选确认",
            "conversation.general": "题库检索",
        }
        for key, expected in success_cases.items():
            with self.subTest(key=key):
                output = render_final_output(final_request(key, self.success_protocol()))
                self.assertEqual(output.message_key, key)
                self.assertIn(expected, output.text)

        current = render_final_output(
            final_request(
                "conversation.current",
                RequestProtocol.from_code(
                    "CLARIFICATION_REQUIRED",
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                ),
                actions=(UserAction.SELECT_CANDIDATE,),
            )
        )
        self.assertEqual(current.message_key, "conversation.current")

        out_of_scope = render_final_output(
            final_request(
                "conversation.out_of_scope",
                RequestProtocol.from_code(
                    "REQUEST_OUT_OF_SCOPE",
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                ),
                actions=(UserAction.UPLOAD_IMAGE,),
            )
        )
        self.assertEqual(out_of_scope.message_key, "conversation.out_of_scope")

        rejected = render_final_output(
            final_request(
                "conversation.action_rejected",
                RequestProtocol.from_code(
                    "ACTION_NOT_ALLOWED",
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                ),
                actions=(UserAction.CONTINUE_CURRENT,),
            )
        )
        self.assertEqual(rejected.message_key, "conversation.action_rejected")

    def test_a2_common_semantic_families_render_from_bounded_facts(self):
        cases = (
            final_request(
                "search.cancelled",
                self.success_protocol(),
                actions=(UserAction.UPLOAD_IMAGE,),
            ),
            final_request(
                "search.chapter.saved",
                self.success_protocol(),
                facts={"chapter_name": "力法"},
            ),
            final_request(
                "search.clarification.required",
                RequestProtocol.from_code(
                    "CLARIFICATION_REQUIRED",
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                ),
                actions=(UserAction.SELECT_CANDIDATE,),
            ),
            final_request(
                "search.candidates.unavailable",
                RequestProtocol.from_code(
                    "CANDIDATE_LIST_UNAVAILABLE",
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                ),
                actions=(UserAction.RETRY_UPLOAD,),
            ),
            final_request(
                "search.candidates.recalled",
                self.success_protocol(),
                facts={"candidate_count": 2},
                actions=(UserAction.SELECT_CANDIDATE,),
            ),
            final_request(
                "search.candidates.rejected_more",
                self.success_protocol(),
                facts={"continuation_available": True},
                actions=(UserAction.CONTINUE_SEARCH,),
            ),
            final_request(
                "search.answer.mismatch",
                self.success_protocol(),
                actions=(UserAction.SHOW_CANDIDATES,),
            ),
            final_request(
                "search.answer.resent",
                self.success_protocol(),
                facts={"delivered_image_count": 2},
            ),
            final_request(
                "search.failed.nonretryable",
                RequestProtocol.from_code(
                    "BANK_ROUTE_FAILED",
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                ),
                actions=(UserAction.UPLOAD_IMAGE,),
            ),
        )
        for request in cases:
            with self.subTest(key=request.message_key):
                output = render_final_output(request)
                self.assertEqual(output.message_key, request.message_key)

        nonretryable = render_final_output(cases[-1])
        self.assertFalse(nonretryable.protocol.retryable)
        self.assertNotIn(UserAction.RETRY_SEARCH, nonretryable.allowed_actions)
        self.assertNotIn("重试", nonretryable.text)

    def test_a3_common_semantic_families_render_from_parent_state(self):
        cases = (
            final_request(
                "page.no_units",
                RequestProtocol.from_code(
                    "PAGE_NO_SEARCHABLE_UNITS",
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                ),
                facts={"question_count": 0},
                actions=(UserAction.RETRY_UPLOAD,),
            ),
            final_request(
                "page.ended",
                self.success_protocol(),
                actions=(UserAction.UPLOAD_IMAGE,),
            ),
            final_request(
                "page.session.reset",
                self.success_protocol(),
                actions=(UserAction.UPLOAD_IMAGE,),
            ),
            final_request(
                "page.current.guidance",
                RequestProtocol.from_code(
                    "CLARIFICATION_REQUIRED",
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                ),
                actions=(UserAction.SELECT_QUESTION,),
            ),
            final_request(
                "page.unit.stopped_remaining",
                self.success_protocol(),
                facts={"question_label": "四-2", "remaining_count": 2},
                actions=(UserAction.SELECT_QUESTION,),
            ),
            final_request(
                "page.unit.stopped_complete",
                self.success_protocol(),
                facts={"question_label": "四-2", "remaining_count": 0},
                actions=(UserAction.UPLOAD_IMAGE,),
            ),
            final_request(
                "page.crop.verification_failed",
                RequestProtocol.from_code(
                    "SERVICE_UNAVAILABLE",
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                ),
                facts={"crop_draft_preserved": True},
                actions=(UserAction.RETRY_REQUEST,),
            ),
        )
        for request in cases:
            with self.subTest(key=request.message_key):
                output = render_final_output(request)
                self.assertEqual(output.message_key, request.message_key)

    def test_new_state_claims_fail_closed_when_their_evidence_is_false(self):
        invalid = (
            final_request(
                "page.no_units",
                RequestProtocol.from_code(
                    "PAGE_NO_SEARCHABLE_UNITS",
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                ),
                facts={"question_count": 1},
                actions=(UserAction.RETRY_UPLOAD,),
            ),
            final_request(
                "page.unit.stopped_complete",
                self.success_protocol(),
                facts={"question_label": "四-2", "remaining_count": 1},
                actions=(UserAction.UPLOAD_IMAGE,),
            ),
            final_request(
                "page.crop.verification_failed",
                RequestProtocol.from_code(
                    "SERVICE_UNAVAILABLE",
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                ),
                facts={"crop_draft_preserved": False},
                actions=(UserAction.RETRY_REQUEST,),
            ),
            final_request(
                "search.candidates.rejected_more",
                self.success_protocol(),
                facts={"continuation_available": False},
                actions=(UserAction.CONTINUE_SEARCH,),
            ),
        )
        for request in invalid:
            with self.subTest(key=request.message_key):
                self.assert_service_fallback(render_final_output(request))

    def test_stage4_compatibility_codes_keep_structured_semantics(self):
        chapter = render_final_output(
            final_request(
                "search.chapter.required",
                RequestProtocol.from_code(
                    "CHAPTER_REQUIRED",
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                ),
                facts={"global_search_offered": True},
                actions=(UserAction.CHANGE_CHAPTER, UserAction.GLOBAL_SEARCH),
            )
        )
        self.assertEqual(chapter.message_key, "search.chapter.required")

        partial_questions = render_final_output(
            final_request(
                "search.questions.ready",
                RequestProtocol.from_code(
                    "MULTI_CROPS_UNAVAILABLE",
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                ),
                facts={"question_count": 2},
                actions=(UserAction.SELECT_QUESTION, UserAction.RETRY_SEARCH),
                notices=("notice.multi_crop_partial",),
            )
        )
        self.assertEqual(partial_questions.message_key, "search.questions.ready")
        self.assertEqual(partial_questions.protocol.status, RequestStatus.PARTIAL)

        for key, facts in (
            ("search.candidates.ready", {"candidate_count": 2}),
            (
                "search.global.candidates.ready",
                {"candidate_count": 2, "source_chapters": ("力法",)},
            ),
        ):
            with self.subTest(key=key):
                output = render_final_output(
                    final_request(
                        key,
                        self.success_protocol(),
                        facts=facts,
                        actions=(UserAction.SELECT_CANDIDATE,),
                    )
                )
                self.assertEqual(output.message_key, key)

        page_global = render_final_output(
            final_request(
                "page.unit.candidates.ready",
                RequestProtocol.from_code(
                    "GLOBAL_CANDIDATES_FOUND",
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                ),
                facts={
                    "question_label": "四-2",
                    "candidate_count": 2,
                    "source_chapters": ("力法",),
                },
                actions=(UserAction.SELECT_CANDIDATE,),
            )
        )
        self.assertEqual(page_global.message_key, "page.unit.candidates.ready")
        self.assertIn("力法", page_global.text)

        global_no_match = render_final_output(
            final_request(
                "search.no_match.global",
                RequestProtocol.from_code(
                    "NO_MATCH", request_id=REQUEST_ID, search_id=SEARCH_ID
                ),
                actions=(UserAction.CHANGE_CHAPTER,),
            )
        )
        self.assertEqual(global_no_match.message_key, "search.no_match.global")

    def test_global_search_action_still_requires_explicit_availability_fact(self):
        output = render_final_output(
            final_request(
                "search.chapter.required",
                RequestProtocol.from_code(
                    "CHAPTER_REQUIRED",
                    request_id=REQUEST_ID,
                    search_id=SEARCH_ID,
                ),
                facts={"global_search_offered": False},
                actions=(UserAction.CHANGE_CHAPTER, UserAction.GLOBAL_SEARCH),
            )
        )
        self.assert_service_fallback(output)

    def test_answer_resent_uses_actual_persisted_media_count(self):
        protocol = RequestProtocol.from_code(
            "MEDIA_PERSIST_FAILED", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        partial = render_final_output(
            final_request(
                "search.answer.resent",
                protocol,
                facts={"delivered_image_count": 1},
                actions=(UserAction.RETRY_REQUEST,),
                notices=("notice.media_partial",),
            )
        )
        self.assertEqual(partial.message_key, "search.answer.resent")
        self.assertIn("共 1 张", partial.text)

        missing = render_final_output(
            final_request(
                "search.answer.resent",
                protocol,
                facts={"delivered_image_count": 0},
                actions=(UserAction.RETRY_REQUEST,),
                notices=("notice.media_partial",),
            )
        )
        self.assert_service_fallback(missing)


class DynamicLabelAndA1ReasonTest(UserOutputAssertions):
    def test_short_question_labels_are_preserved(self):
        protocol = RequestProtocol.from_code(
            "CLARIFICATION_REQUIRED", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        for label in ("四-1", "3-15", "12", "第 3 题"):
            with self.subTest(label=label):
                output = render_final_output(
                    final_request(
                        "page.crop.required",
                        protocol,
                        facts={"question_label": label},
                        actions=(UserAction.CROP_QUESTION,),
                    )
                )
                self.assertEqual(output.message_key, "page.crop.required")
                self.assertIn(label.replace(" ", ""), output.text.replace(" ", ""))

    def test_invalid_question_labels_use_stable_page_index(self):
        protocol = RequestProtocol.from_code(
            "CLARIFICATION_REQUIRED", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        marker = "LEAK_X9"
        labels = (
            f"C:\\private\\{marker}.jpg",
            f"https://example.test/{marker}",
            f"<b>{marker}</b>",
            f"第1题\n{marker}",
            marker * 10,
        )
        for label in labels:
            with self.subTest(label=label):
                output = render_final_output(
                    final_request(
                        "page.crop.required",
                        protocol,
                        facts={"question_label": label, "page_index": 7},
                        actions=(UserAction.CROP_QUESTION,),
                    )
                )
                self.assertEqual(output.message_key, "page.crop.required")
                self.assertIn("图片第 7 题", output.text)
                self.assert_not_public(output, marker)

    def test_invalid_label_without_valid_page_index_fails_closed(self):
        protocol = RequestProtocol.from_code(
            "CLARIFICATION_REQUIRED", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        for facts in (
            {"question_label": "这是一整句模型说明"},
            {"question_label": "bad", "page_index": 0},
            {"question_label": "bad", "page_index": -1},
            {"question_label": "bad", "page_index": True},
            {"question_label": "bad", "page_index": "1"},
        ):
            with self.subTest(facts=facts):
                self.assert_service_fallback(
                    render_final_output(
                        final_request(
                            "page.crop.required",
                            protocol,
                            facts=facts,
                            actions=(UserAction.CROP_QUESTION,),
                        )
                    )
                )

    def test_page_selection_requires_a_positive_count(self):
        protocol = RequestProtocol.from_code(
            "QUESTION_INDEX_REQUIRED", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        for facts in (
            {},
            {"question_count": 0},
            {"remaining_count": 0},
            {"question_count": 3, "remaining_count": 0},
            {"question_count": 2, "remaining_count": 3},
        ):
            with self.subTest(facts=facts):
                self.assert_service_fallback(
                    render_final_output(
                        final_request(
                            "page.selection.required",
                            protocol,
                            facts=facts,
                            actions=(UserAction.SELECT_QUESTION,),
                        )
                    )
                )

    def test_a1_reason_enum_renders_only_fixed_copy(self):
        protocol = RequestProtocol.from_code(
            "TRIAGE_A1_STOPPED", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        cases = {
            "unrelated_image": "这张图不是可检索的结构力学题，请重新上传一张完整的结构力学题图。",
            "image_unclear": "题图不够清楚，无法确认完整结构和荷载，请重新上传一张清楚、完整的题图。",
            "structure_incomplete": "题图没有完整显示结构和支座，请重新上传包含完整结构、支座和实际荷载的题图。",
            "load_incomplete": "题图没有完整显示实际荷载，请重新上传包含完整结构和实际荷载的题图。",
            "original_structure_missing": "题图没有完整的原结构图，请重新上传包含原结构、支座和实际荷载的题图。",
            "unsupported_content": "当前图片不适合进入题库检索，请重新上传包含完整结构和实际荷载的题图。",
        }
        for reason, expected in cases.items():
            with self.subTest(reason=reason):
                output = render_final_output(
                    final_request(
                        "triage.a1.reasoned",
                        protocol,
                        facts={"a1_reason": reason},
                        actions=(UserAction.RETRY_UPLOAD,),
                    )
                )
                self.assertEqual(output.message_key, "triage.a1.reasoned")
                self.assertEqual(output.text, expected)

    def test_invalid_a1_reason_is_logged_and_never_reflected(self):
        protocol = RequestProtocol.from_code(
            "TRIAGE_A1_STOPPED", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        marker = "LEAK_X9"
        with self.assertLogs("tiku_agent.user_output", level="WARNING") as captured:
            output = render_final_output(
                final_request(
                    "triage.a1.reasoned",
                    protocol,
                    facts={"a1_reason": f"image_unclear_{marker}"},
                    actions=(UserAction.RETRY_UPLOAD,),
                )
            )
        self.assertNotEqual(output.message_key, "triage.a1.reasoned")
        self.assert_not_public(output, marker)
        self.assertTrue(
            any("user_output_contract_violation category=" in line for line in captured.output)
        )

    def test_arbitrary_a1_model_text_is_not_in_the_request_contract(self):
        self.assertNotIn("bounded_text", FinalOutputRequestV1.__dataclass_fields__)
        protocol = RequestProtocol.from_code(
            "TRIAGE_A1_STOPPED", request_id=REQUEST_ID, search_id=SEARCH_ID
        )
        phrases = (
            "题图不完整，但我已取得结果，请重新上传。",
            "题图不清楚，但解答已给，请重新上传。",
            "题图不清楚，但已处理完毕，请重新上传。",
            "题图不清楚，但结果已生成，请重新上传。",
            "题图不清楚，但已命中题库，请重新上传。",
            "题图不清楚，但我已删除题库内容，请重新上传。",
        )
        base = {
            "schema_version": 1,
            "kind": FinalOutputKind.RESULT,
            "message_key": "triage.a1.reasoned",
            "protocol": protocol,
            "phase": "PROCESSING",
            "facts": {"a1_reason": "image_unclear"},
            "allowed_actions": (UserAction.RETRY_UPLOAD,),
        }
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                with self.assertRaises(TypeError):
                    FinalOutputRequestV1(**base, bounded_text=phrase)


class NoticeCatalogTest(UserOutputAssertions):
    FINAL_KEYS = {
        "conversation.greeting",
        "conversation.courtesy",
        "conversation.farewell",
        "conversation.identity",
        "conversation.capability",
        "conversation.supported_chapters",
        "conversation.workflow",
        "conversation.general",
        "conversation.current",
        "conversation.out_of_scope",
        "conversation.action_rejected",
        "search.upload.required",
        "search.cancelled",
        "search.chapter.required",
        "search.chapter.saved",
        "search.chapter.unsupported",
        "search.clarification.required",
        "search.questions.ready",
        "search.candidates.ready",
        "search.global.candidates.ready",
        "search.candidates.unavailable",
        "search.candidates.recalled",
        "search.candidates.rejected_more",
        "search.candidates.rejected",
        "search.no_match.chapter",
        "search.no_match.global",
        "search.answer.ready",
        "search.answer.mismatch",
        "search.answer.resent",
        "search.answer.missing",
        "search.failed.retryable",
        "search.failed.nonretryable",
        "triage.a1.reasoned",
        "triage.a1.fallback",
        "triage.a1.no_external_load",
        "page.no_units",
        "page.selection.required",
        "page.current.guidance",
        "page.units.prepared",
        "page.crop.required",
        "page.crop.rejected",
        "page.crop.verification_failed",
        "page.namespace.clarification",
        "page.cancel.scope_required.current",
        "page.cancel.scope_required.page",
        "page.unit.cancelled_remaining",
        "page.unit.stopped_remaining",
        "page.unit.stopped_complete",
        "page.unit.candidates.ready",
        "page.unit.answer.delivered_remaining",
        "page.unit.answer.delivered_complete",
        "page.completed",
        "page.ended",
        "page.session.reset",
        "page.stale.selection",
        "page.stale.candidate",
        "page.failed.retryable",
        "system.login.required",
        "system.quota.unavailable",
        "system.quota.identity_missing",
        "system.queue.full",
        "system.upload.required",
        "system.upload.persist_failed",
        "system.network.unavailable",
        "system.session.expired",
        "system.media.not_found",
        "system.feedback.invalid",
        "system.feedback.save_failed",
        "system.service.unavailable",
    }

    def candidate_request(self, notices):
        return final_request(
            "search.candidates.ready",
            dynamic_protocol(
                "RERANK_INCOMPLETE_COARSE_FALLBACK",
                RequestStatus.PARTIAL,
                retryable=True,
            ),
            facts={"candidate_count": 2},
            actions=(UserAction.SELECT_CANDIDATE, UserAction.RETRY_SEARCH),
            notices=notices,
        )

    def test_registered_notice_is_deduplicated(self):
        first = render_final_output(
            self.candidate_request(
                (
                    "notice.rerank_coarse_fallback",
                    "notice.rerank_coarse_fallback",
                )
            )
        )
        second = render_final_output(
            self.candidate_request(("notice.rerank_coarse_fallback",))
        )

        self.assertEqual(first.text, second.text)
        self.assertEqual(first.text.count("精排未完整完成"), 1)

    def test_unknown_or_incompatible_notice_uses_safe_fallback(self):
        marker = "LEAK_X9"
        unknown = render_final_output(
            self.candidate_request((f"notice.{marker}",))
        )
        self.assertEqual(unknown.message_key, "fallback.partial")
        self.assert_not_public(unknown, marker)

        incompatible = render_final_output(
            self.candidate_request(("notice.structure_filter_skipped",))
        )
        self.assertEqual(incompatible.message_key, "fallback.partial")

    def test_catalog_and_notice_registries_are_stable_and_self_validating(self):
        validate_catalog_configuration()
        self.assertEqual(set(catalog_message_keys()), self.FINAL_KEYS)
        self.assertEqual(
            set(notice_keys()),
            {
                "notice.multi_detection_fallback",
                "notice.multi_crop_partial",
                "notice.structure_filter_skipped",
                "notice.rerank_coarse_fallback",
                "notice.media_partial",
            },
        )


class ProgressOutputContractTest(UserOutputAssertions):
    CASES = {
        "progress.queue.waiting": ({}, "queue"),
        "progress.queue.started": ({}, "queue"),
        "progress.image.triage": ({}, "triage"),
        "progress.image.analysis": ({}, "image_analysis"),
        "progress.search.chapter": ({"chapter_name": "力法"}, "chapter_search"),
        "progress.search.global": ({}, "global_search"),
        "progress.page.understanding": ({}, "page_understanding"),
        "progress.page.reunderstanding": ({}, "page_reunderstanding"),
        "progress.page.auto_grounding": ({}, "auto_grounding"),
        "progress.page.crop_validating": ({"question_label": "第1题"}, "crop_validating"),
        "progress.page.unit_analysis": ({"question_label": "第1题"}, "unit_analysis"),
        "progress.page.auto_crop_ready": ({}, "auto_crop"),
    }

    def progress_request(
        self,
        key,
        facts,
        *,
        request_id=REQUEST_ID,
        search_id=SEARCH_ID,
        sequence=3,
        schema_version=1,
    ):
        return ProgressOutputRequestV1(
            schema_version=schema_version,
            progress_key=key,
            request_id=request_id,
            search_id=search_id,
            sequence=sequence,
            facts=dict(facts),
        )

    def test_every_registered_progress_key_renders_stage_from_catalog(self):
        self.assertEqual(set(progress_message_keys()), set(self.CASES))
        for key, (facts, stage) in self.CASES.items():
            with self.subTest(key=key):
                output = render_progress_output(self.progress_request(key, facts))
                self.assertEqual(output.message_key, key)
                self.assertEqual(output.kind, OutputKind.PROGRESS)
                self.assertIsNone(output.protocol)
                self.assertEqual(output.allowed_actions, ())
                self.assertEqual(output.request_id, REQUEST_ID)
                self.assertEqual(output.search_id, SEARCH_ID)
                self.assertEqual(output.sequence, 3)
                self.assertEqual(output.stage, stage)
                self.assertEqual(output.to_stream_event()["data"], output.to_dict())

    def test_transport_needs_input_uses_error_stream_envelope(self):
        output = render_final_output(
            final_request(
                "system.login.required",
                RequestProtocol.from_code(
                    "LOGIN_REQUIRED", request_id=REQUEST_ID, search_id=SEARCH_ID
                ),
                kind=FinalOutputKind.TRANSPORT_ERROR,
                actions=(UserAction.RELOGIN,),
            )
        )
        self.assertEqual(output.protocol.status, RequestStatus.NEEDS_INPUT)
        self.assertEqual(output.to_stream_event()["type"], "error")

    def test_search_id_may_be_empty_before_search_is_created(self):
        output = render_progress_output(
            self.progress_request("progress.image.triage", {}, search_id="")
        )
        self.assertEqual(output.message_key, "progress.image.triage")
        self.assertEqual(output.search_id, "")

    def test_invalid_question_label_in_progress_uses_page_index(self):
        marker = "LEAK_X9"
        output = render_progress_output(
            self.progress_request(
                "progress.page.unit_analysis",
                {"question_label": f"https://{marker}.test", "page_index": 4},
            )
        )
        self.assertEqual(output.message_key, "progress.page.unit_analysis")
        self.assertIn("图片第 4 题", output.text)
        self.assert_not_public(output, marker)

    def test_invalid_progress_inputs_return_nonreflective_generic_progress(self):
        marker = "LEAK_X9"
        vectors = (
            self.progress_request(f"progress.{marker}", {}),
            self.progress_request(
                "progress.search.chapter", {"chapter_name": "力法", "detail": marker}
            ),
            self.progress_request(
                "progress.search.chapter",
                {"chapter_name": f"C:\\private\\{marker}.jpg"},
            ),
            self.progress_request(
                "progress.image.triage", {"text": f"Traceback {marker}"}
            ),
        )
        for request in vectors:
            with self.subTest(request=request):
                output = render_progress_output(request)
                self.assertEqual(output.message_key, "progress.safe")
                self.assertEqual(output.kind, OutputKind.PROGRESS)
                self.assertIsNone(output.protocol)
                self.assertEqual(output.allowed_actions, ())
                self.assert_not_public(output, marker)

    def test_invalid_progress_envelope_is_not_publishable(self):
        marker = "LEAKX9"
        vectors = (
            self.progress_request("progress.image.triage", {}, schema_version=2),
            self.progress_request("progress.image.triage", {}, schema_version=True),
            self.progress_request("progress.image.triage", {}, schema_version=1.0),
            self.progress_request("progress.image.triage", {}, sequence=True),
            self.progress_request("progress.image.triage", {}, sequence=0),
            self.progress_request("progress.image.triage", {}, sequence=-1),
            self.progress_request(
                "progress.image.triage", {}, request_id=f"sk-{marker}abcdefghijklmnop"
            ),
            self.progress_request(
                "progress.image.triage", {}, search_id=f"token_{marker}abcdef"
            ),
        )
        for request in vectors:
            with self.subTest(request=request):
                with self.assertRaisesRegex(OutputContractError, "progress_not_publishable"):
                    render_progress_output(request)

    def test_internal_chapter_storage_key_is_not_public_progress(self):
        marker = "4力法"
        output = render_progress_output(
            self.progress_request(
                "progress.search.chapter",
                {"chapter_name": marker},
            )
        )
        self.assertEqual(output.message_key, "progress.safe")
        self.assert_not_public(output, marker)


if __name__ == "__main__":
    unittest.main()
