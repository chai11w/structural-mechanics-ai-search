from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
import unittest

from tiku_agent import checkpoint_contract as contract


NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
TRACE_ID = "trace_" + "1" * 32
REQUEST_ID = "req_" + "2" * 32
SESSION_KEY = "3" * 64
REVISION = "4" * 40
HASH = "5" * 64


def _timestamp(days: int = 0) -> str:
    return (NOW + timedelta(days=days)).isoformat()


def _owner(
    *,
    scope: str = contract.SCOPE_WORKFLOW,
    unit_id: str = "",
    search_id: str = "",
    workflow_search_id: str = "search_parent",
    candidate_generation: str = "",
    workflow_task_revision: int = 2,
    task_revision: int = 2,
    identity_key: str = "invite_1",
) -> contract.CheckpointOwnerV1:
    return contract.CheckpointOwnerV1(
        scope=scope,
        session_key=SESSION_KEY,
        identity_key=identity_key,
        workflow_search_id=workflow_search_id,
        workflow_task_revision=workflow_task_revision,
        search_id=search_id,
        unit_id=unit_id,
        task_revision=task_revision,
        candidate_generation=candidate_generation,
    )


def _producer(*, model: bool = False) -> contract.ProducerVersionV1:
    return contract.ProducerVersionV1(
        code_revision=REVISION,
        component="checkpoint_fixture",
        component_version="v1",
        model_provider="qwen" if model else "",
        model_name="qwen-vl-max" if model else "",
        prompt_sha256=HASH if model else "",
        input_schema_version="fixture-v1",
        policy_version="policy-v1",
        data_version="bank-v1",
    )


def _artifact(role: str, ordinal: int = 1) -> contract.ArtifactLinkV1:
    marker = len(role) + ordinal
    return contract.ArtifactLinkV1(
        artifact_id=f"art_{marker:032x}",
        role=role,
        ordinal=ordinal,
    )


def _image_metadata() -> dict[str, object]:
    return {
        "width_px": 1920,
        "height_px": 1080,
        "byte_size": 123_456,
        "mime_type": "image/jpeg",
        "sha256": HASH,
        "orientation_status": "not_run",
        "applied_rotation_degrees": 0,
    }


def _unit_result(index: int = 1) -> dict[str, object]:
    return {
        "unit_id": f"unit_{index}",
        "group_id": f"group_{index}",
        "parent_question_label": str(index),
        "question_label": f"{index}a",
        "display_label": f"第{index}题",
        "searchability": "searchable_candidate",
        "status": "clear",
        "reason_codes": [],
        "diagram_roles": ["original_structure"],
        "recognized_text_excerpt": "连续梁，跨中作用集中荷载 P。",
    }


def _candidate_score() -> dict[str, object]:
    return {
        "candidate_id": "candidate_1",
        "coarse_rank": 1,
        "coarse_score": 0.96,
        "rerank_rank": 1,
        "rerank_score": 0.94,
        "final_score": 0.95,
        "score_status": "completed",
        "reason_code": "SCORE_AVAILABLE",
        "structure_type": "梁",
        "long_width": "4m x 2m",
        "single_side": "2m",
    }


def _stage_fixture(stage: str):
    if stage == contract.STAGE_IMAGE_ACCEPTED:
        return (
            _owner(),
            {contract.SECTION_IMAGE_METADATA: _image_metadata()},
            (_artifact(contract.ARTIFACT_ROLE_SOURCE_PAGE),),
            contract.OUTCOME_SUCCESS,
        )
    if stage == contract.STAGE_IMAGE_ROUTED:
        return (
            _owner(),
            {
                contract.SECTION_ROUTE_DECISION: {
                    "route": "A3",
                    "decision_source": "authority_v1",
                    "reason_code": "MULTI_QUESTION_PAGE",
                    "confidence": 0.98,
                }
            },
            (),
            contract.OUTCOME_SUCCESS,
        )
    if stage == contract.STAGE_PAGE_UNDERSTOOD:
        return (
            _owner(),
            {
                contract.SECTION_PAGE_SUMMARY: {
                    "source_schema_version": "a3-page-understanding-v2",
                    "page_disposition": "has_searchable_candidates",
                    "group_count": 1,
                    "unit_count": 1,
                    "stored_unit_count": 1,
                    "units_truncated": False,
                    "searchable_unit_count": 1,
                    "diagram_count": 1,
                    "unknown_count": 0,
                },
                contract.SECTION_UNIT_RESULTS: [_unit_result()],
            },
            (),
            contract.OUTCOME_SUCCESS,
        )
    if stage == contract.STAGE_CROP_PREPARED:
        return (
            _owner(unit_id="unit_1"),
            {
                contract.SECTION_CROP_GEOMETRY: {
                    "unit_id": "unit_1",
                    "method": "glm_bbox_pillow",
                    "model_bbox": [100, 100, 900, 900],
                    "expanded_bbox": [80, 80, 920, 920],
                    "pixel_bounds": {"left": 154, "top": 86, "right": 1766, "bottom": 994},
                    "source_width_px": 1920,
                    "source_height_px": 1080,
                    "crop_width_px": 1612,
                    "crop_height_px": 908,
                },
                contract.SECTION_CROP_GROUNDING: {
                    "schema_version": "a3-page-crops-v1",
                    "page_status": "auto_ready",
                    "grounding_status": "auto_ready",
                    "reason_codes": [],
                    "binding_evidence_excerpt": "题号与结构图邻接。",
                },
            },
            (_artifact(contract.ARTIFACT_ROLE_QUESTION_CROP),),
            contract.OUTCOME_SUCCESS,
        )
    if stage == contract.STAGE_CROP_VALIDATED:
        return (
            _owner(unit_id="unit_1"),
            {
                contract.SECTION_CROP_VALIDATION: {
                    "schema_version": "a3-crop-compare-v2",
                    "verdict": "verified",
                    "checks": {
                        "selected_diagram_match": True,
                        "single_target_diagram": True,
                        "structure_complete": True,
                        "supports_complete": True,
                        "external_loads_complete": True,
                        "image_clear": True,
                    },
                    "external_load_status": "yes",
                    "reason_codes": [],
                }
            },
            (_artifact(contract.ARTIFACT_ROLE_QUESTION_CROP),),
            contract.OUTCOME_SUCCESS,
        )
    child = _owner(
        scope=contract.SCOPE_CHILD_TASK,
        search_id="search_child",
        unit_id="unit_1",
        candidate_generation="2:1" if stage in {
            contract.STAGE_RERANK_COMPLETED,
            contract.STAGE_ANSWER_PREPARED,
        } else "",
    )
    if stage == contract.STAGE_QUESTION_ANALYZED:
        return (
            child,
            {
                contract.SECTION_QUESTION_CONTEXT: {
                    "analysis_schema_version": "a3-unit-analysis-v1",
                    "recognized_text_excerpt": "连续梁受集中荷载 P。",
                    "category": "concentrated",
                },
                contract.SECTION_CHAPTER_DECISION: {
                    "chapter": "4力法",
                    "confidence": 0.96,
                    "source": "qwen_guarded",
                    "scope_status": "supported",
                    "reason_code": "CHAPTER_RESOLVED",
                    "evidence_excerpt": "题目要求用力法计算。",
                },
                contract.SECTION_LOAD_OBSERVATIONS: [{"type": "集中", "raw": "P"}],
                contract.SECTION_STRUCTURE_DECISION: {
                    "structure_type": "梁",
                    "source": "vision",
                    "filter_applicable": True,
                    "reason_code": "STRUCTURE_RECOGNIZED",
                    "confidence": 0.91,
                },
            },
            (_artifact(contract.ARTIFACT_ROLE_QUESTION_CROP),),
            contract.OUTCOME_SUCCESS,
        )
    counts = {
        "chapter_scanned": 428,
        "load_scored": 428,
        "positive_score": 18,
        "rerank_pool": 10,
        "after_dimension_filter": 8,
        "stored_score_count": 1,
        "scores_truncated": True,
        "remaining": 8,
    }
    if stage == contract.STAGE_COARSE_SEARCH_COMPLETED:
        return (
            child,
            {
                contract.SECTION_DIMENSION_OBSERVATIONS: [
                    {
                        "kind": "span",
                        "raw": "4m",
                        "normalized": "4",
                        "unit": "m",
                        "source": "vision",
                        "status": "recognized",
                        "reason_code": "DIMENSION_RECOGNIZED",
                    }
                ],
                contract.SECTION_CANDIDATE_COUNTS: counts,
                contract.SECTION_FILTER_DECISIONS: [
                    {
                        "filter": "dimension",
                        "status": "applied",
                        "before": 10,
                        "after": 8,
                        "reason_code": "DIMENSION_MATCH",
                        "policy_version": "dimension-v1",
                    }
                ],
                contract.SECTION_CANDIDATE_SCORES: [_candidate_score()],
            },
            (),
            contract.OUTCOME_SUCCESS,
        )
    if stage == contract.STAGE_RERANK_COMPLETED:
        return (
            child,
            {
                contract.SECTION_RERANK_POLICY: {
                    "reranked": True,
                    "input_count": 8,
                    "completed_count": 8,
                    "failed_count": 0,
                    "threshold": 0.9,
                    "display_all_score": 0.95,
                    "fallback_limit": 3,
                    "fallback_used": False,
                    "reason_code": "RERANK_COMPLETED",
                    "policy_version": "shared-rerank-v1",
                    "visible": 1,
                    "stored_score_count": 1,
                    "scores_truncated": True,
                },
                contract.SECTION_CANDIDATE_SCORES: [
                    dict(_candidate_score(), visible=True)
                ],
            },
            (),
            contract.OUTCOME_SUCCESS,
        )
    if stage == contract.STAGE_ANSWER_PREPARED:
        return (
            child,
            {
                contract.SECTION_SELECTION: {
                    "candidate_id": "candidate_1",
                    "selected_rank": 1,
                    "candidate_generation": "2:1",
                    "selection_source": "user",
                },
                contract.SECTION_DELIVERY: {
                    "answer_artifact_count": 1,
                    "media_status": "complete",
                    "delivery_code": "ANSWER_READY",
                    "response_id": "resp_" + "6" * 32,
                },
            },
            (_artifact(contract.ARTIFACT_ROLE_ANSWER_IMAGE),),
            contract.OUTCOME_SUCCESS,
        )
    raise AssertionError(stage)


def _checkpoint(stage: str, **changes) -> contract.IntermediateCheckpointV1:
    owner, result, artifacts, outcome = _stage_fixture(stage)
    values = {
        "checkpoint_id": "ckpt_" + "7" * 32,
        "trace_id": TRACE_ID,
        "request_id": REQUEST_ID,
        "stage": stage,
        "outcome": outcome,
        "occurred_at": _timestamp(),
        "expires_at": _timestamp(30),
        "retention_class": contract.RETENTION_NORMAL,
        "owner": owner,
        "producer": _producer(model=stage in {
            contract.STAGE_PAGE_UNDERSTOOD,
            contract.STAGE_CROP_PREPARED,
            contract.STAGE_CROP_VALIDATED,
            contract.STAGE_QUESTION_ANALYZED,
            contract.STAGE_RERANK_COMPLETED,
        }),
        "input_fingerprint": HASH,
        "result": result,
        "artifacts": artifacts,
    }
    values.update(changes)
    return contract.IntermediateCheckpointV1(**values)


class CheckpointContractTests(unittest.TestCase):
    def test_v1_literal_sets_and_trace_first_stage_matrix_are_exact(self):
        self.assertEqual(
            contract.CHECKPOINT_STAGES,
            {
                "image_accepted",
                "image_routed",
                "page_understood",
                "crop_prepared",
                "crop_validated",
                "question_analyzed",
                "coarse_search_completed",
                "rerank_completed",
                "answer_prepared",
            },
        )
        self.assertEqual(set(contract.STAGE_CONTRACTS), contract.CHECKPOINT_STAGES)
        self.assertTrue(
            all(contract.OUTCOME_FAILED in item.outcomes for key, item in contract.STAGE_CONTRACTS.items() if key != contract.STAGE_IMAGE_ACCEPTED)
        )
        self.assertEqual(
            contract.STAGE_CONTRACTS[contract.STAGE_IMAGE_ACCEPTED].outcomes,
            {contract.OUTCOME_SUCCESS, contract.OUTCOME_PARTIAL},
        )

    def test_section_contracts_cover_every_declared_result_section(self):
        self.assertEqual(set(contract.SECTION_CONTRACTS), contract.CHECKPOINT_RESULT_SECTIONS)
        for stage in contract.STAGE_CONTRACTS.values():
            self.assertFalse(set(stage.required_sections) & set(stage.optional_sections))
            self.assertTrue(
                (set(stage.required_sections) | set(stage.optional_sections))
                <= contract.CHECKPOINT_RESULT_SECTIONS
            )

    def test_every_success_stage_has_one_valid_canonical_record(self):
        for stage in sorted(contract.CHECKPOINT_STAGES):
            with self.subTest(stage=stage):
                value = _checkpoint(stage)
                payload = value.to_dict()
                self.assertEqual(payload["contract"], "intermediate_checkpoint")
                self.assertEqual(payload["schema_version"], 1)
                self.assertEqual(payload["stage"], stage)
                self.assertEqual(payload["trace_id"], TRACE_ID)

    def test_direct_a2_uses_explicit_equal_parent_and_child_ids(self):
        owner = _owner(
            scope=contract.SCOPE_CHILD_TASK,
            workflow_search_id="search_direct",
            search_id="search_direct",
        )
        value = _checkpoint(contract.STAGE_QUESTION_ANALYZED, owner=owner)
        self.assertEqual(value.owner.workflow_search_id, value.owner.search_id)
        self.assertEqual(value.owner.workflow_task_revision, value.owner.task_revision)

    def test_a3_child_keeps_parent_and_child_revisions_distinct(self):
        owner = _owner(
            scope=contract.SCOPE_CHILD_TASK,
            search_id="search_child",
            unit_id="unit_1",
            workflow_task_revision=7,
            task_revision=2,
            candidate_generation="2:1",
        )
        value = _checkpoint(contract.STAGE_RERANK_COMPLETED, owner=owner)
        self.assertEqual(value.owner.workflow_task_revision, 7)
        self.assertEqual(value.owner.task_revision, 2)
        self.assertEqual(value.owner.candidate_generation, "2:1")

        with self.assertRaisesRegex(ValueError, "does not match task revision"):
            _owner(
                scope=contract.SCOPE_CHILD_TASK,
                search_id="search_child",
                unit_id="unit_1",
                workflow_task_revision=7,
                task_revision=2,
                candidate_generation="7:1",
            )

    def test_workflow_and_child_topology_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "must not overload search_id"):
            _owner(search_id="search_child")
        with self.assertRaisesRegex(ValueError, "requires search_id"):
            _owner(scope=contract.SCOPE_CHILD_TASK)
        with self.assertRaisesRegex(ValueError, "standalone child"):
            _owner(scope=contract.SCOPE_CHILD_TASK, search_id="search_child")
        with self.assertRaisesRegex(ValueError, "workflow checkpoint revisions must match"):
            _owner(workflow_task_revision=7, task_revision=2)
        with self.assertRaisesRegex(ValueError, "standalone child checkpoint revisions must match"):
            _owner(
                scope=contract.SCOPE_CHILD_TASK,
                workflow_search_id="search_direct",
                search_id="search_direct",
                workflow_task_revision=7,
                task_revision=2,
            )
        with self.assertRaisesRegex(ValueError, "requires distinct workflow and search ids"):
            _owner(
                scope=contract.SCOPE_CHILD_TASK,
                workflow_search_id="search_direct",
                search_id="search_direct",
                unit_id="unit_1",
            )
        with self.assertRaisesRegex(ValueError, "does not match task revision"):
            _owner(
                scope=contract.SCOPE_CHILD_TASK,
                search_id="search_child",
                unit_id="unit_1",
                candidate_generation="3:1",
            )
        with self.assertRaisesRegex(ValueError, "scope does not match stage"):
            _checkpoint(
                contract.STAGE_IMAGE_ROUTED,
                owner=_owner(
                    scope=contract.SCOPE_CHILD_TASK,
                    search_id="search_child",
                    unit_id="unit_1",
                ),
            )
        with self.assertRaisesRegex(ValueError, "requires unit binding"):
            _checkpoint(contract.STAGE_CROP_PREPARED, owner=_owner())
        with self.assertRaisesRegex(ValueError, "forbids unit binding"):
            _checkpoint(contract.STAGE_PAGE_UNDERSTOOD, owner=_owner(unit_id="unit_1"))

    def test_checkpoint_ids_are_server_shaped_and_generated(self):
        self.assertTrue(contract.is_valid_checkpoint_id(contract.new_checkpoint_id()))
        self.assertTrue(contract.is_valid_artifact_id(contract.new_artifact_id()))
        for bad in ("", "ckpt_123", "art_" + "A" * 32, "../checkpoint"):
            self.assertFalse(contract.is_valid_checkpoint_id(bad))
        with self.assertRaisesRegex(ValueError, "trace id"):
            _checkpoint(contract.STAGE_IMAGE_ROUTED, trace_id="req_" + "1" * 32)
        with self.assertRaisesRegex(ValueError, "own predecessor"):
            _checkpoint(
                contract.STAGE_IMAGE_ROUTED,
                predecessor_checkpoint_id="ckpt_" + "7" * 32,
            )

    def test_result_is_deeply_detached_immutable_and_serializes_as_json(self):
        source = {contract.SECTION_PAGE_SUMMARY: dict(_stage_fixture(contract.STAGE_PAGE_UNDERSTOOD)[1][contract.SECTION_PAGE_SUMMARY]), contract.SECTION_UNIT_RESULTS: [_unit_result()]}
        value = _checkpoint(contract.STAGE_PAGE_UNDERSTOOD, result=source)
        source[contract.SECTION_UNIT_RESULTS][0]["display_label"] = "changed"
        self.assertEqual(
            value.result[contract.SECTION_UNIT_RESULTS][0]["display_label"],
            "第1题",
        )
        with self.assertRaises(TypeError):
            value.result[contract.SECTION_PAGE_SUMMARY]["unit_count"] = 99
        first = value.to_dict()
        first["result"][contract.SECTION_UNIT_RESULTS][0]["display_label"] = "mutable copy"
        self.assertEqual(
            value.to_dict()["result"][contract.SECTION_UNIT_RESULTS][0]["display_label"],
            "第1题",
        )

    def test_page_units_are_bounded_with_explicit_truncation(self):
        owner, result, artifacts, _ = _stage_fixture(contract.STAGE_PAGE_UNDERSTOOD)
        summary = dict(result[contract.SECTION_PAGE_SUMMARY])
        summary.update(
            group_count=12,
            unit_count=12,
            stored_unit_count=12,
            units_truncated=False,
            searchable_unit_count=12,
            diagram_count=12,
        )
        twelve = {
            contract.SECTION_PAGE_SUMMARY: summary,
            contract.SECTION_UNIT_RESULTS: [_unit_result(index) for index in range(1, 13)],
        }
        value = _checkpoint(
            contract.STAGE_PAGE_UNDERSTOOD,
            owner=owner,
            result=twelve,
            artifacts=artifacts,
        )
        self.assertEqual(len(value.result[contract.SECTION_UNIT_RESULTS]), 12)

        summary.update(
            group_count=60,
            unit_count=60,
            stored_unit_count=contract.MAX_COLLECTION_ITEMS,
            units_truncated=True,
            searchable_unit_count=60,
            diagram_count=60,
        )
        truncated = {
            contract.SECTION_PAGE_SUMMARY: summary,
            contract.SECTION_UNIT_RESULTS: [
                _unit_result(index)
                for index in range(1, contract.MAX_COLLECTION_ITEMS + 1)
            ],
        }
        value = _checkpoint(contract.STAGE_PAGE_UNDERSTOOD, result=truncated)
        self.assertTrue(
            value.result[contract.SECTION_PAGE_SUMMARY]["units_truncated"]
        )

        for bad_summary in (
            dict(summary, stored_unit_count=49),
            dict(summary, units_truncated=False),
            dict(summary, stored_unit_count=61),
        ):
            changed = dict(truncated)
            changed[contract.SECTION_PAGE_SUMMARY] = bad_summary
            with self.subTest(summary=bad_summary), self.assertRaises(ValueError):
                _checkpoint(contract.STAGE_PAGE_UNDERSTOOD, result=changed)

    def test_stage_results_only_require_information_available_at_that_boundary(self):
        owner, result, artifacts, _ = _stage_fixture(contract.STAGE_QUESTION_ANALYZED)
        premature_dimensions = dict(result)
        premature_dimensions[contract.SECTION_DIMENSION_OBSERVATIONS] = []
        with self.assertRaisesRegex(ValueError, "unsupported sections"):
            _checkpoint(
                contract.STAGE_QUESTION_ANALYZED,
                owner=owner,
                result=premature_dimensions,
                artifacts=artifacts,
            )

        owner, result, artifacts, _ = _stage_fixture(
            contract.STAGE_COARSE_SEARCH_COMPLETED
        )
        missing_dimensions = dict(result)
        del missing_dimensions[contract.SECTION_DIMENSION_OBSERVATIONS]
        with self.assertRaisesRegex(ValueError, "missing required"):
            _checkpoint(
                contract.STAGE_COARSE_SEARCH_COMPLETED,
                owner=owner,
                result=missing_dimensions,
                artifacts=artifacts,
            )

        empty_dimensions = dict(result)
        empty_dimensions[contract.SECTION_DIMENSION_OBSERVATIONS] = []
        with self.assertRaisesRegex(ValueError, "requires a dimension observation"):
            _checkpoint(
                contract.STAGE_COARSE_SEARCH_COMPLETED,
                owner=owner,
                result=empty_dimensions,
                artifacts=artifacts,
            )

        missing_reason = dict(result)
        dimension = dict(result[contract.SECTION_DIMENSION_OBSERVATIONS][0])
        del dimension["reason_code"]
        missing_reason[contract.SECTION_DIMENSION_OBSERVATIONS] = [dimension]
        with self.assertRaisesRegex(ValueError, "invalid fields"):
            _checkpoint(
                contract.STAGE_COARSE_SEARCH_COMPLETED,
                owner=owner,
                result=missing_reason,
                artifacts=artifacts,
            )

        not_run = dict(result)
        not_run[contract.SECTION_DIMENSION_OBSERVATIONS] = [
            {
                "kind": "other",
                "raw": "",
                "normalized": "",
                "unit": "",
                "source": "not_run",
                "status": "not_run",
                "reason_code": "DIMENSION_NOT_RUN",
            }
        ]
        value = _checkpoint(
            contract.STAGE_COARSE_SEARCH_COMPLETED,
            owner=owner,
            result=not_run,
            artifacts=artifacts,
        )
        self.assertEqual(
            value.result[contract.SECTION_DIMENSION_OBSERVATIONS][0]["status"],
            "not_run",
        )

        premature_visible = dict(result)
        premature_counts = dict(result[contract.SECTION_CANDIDATE_COUNTS])
        premature_counts["visible"] = 1
        premature_visible[contract.SECTION_CANDIDATE_COUNTS] = premature_counts
        with self.assertRaisesRegex(ValueError, "invalid fields"):
            _checkpoint(
                contract.STAGE_COARSE_SEARCH_COMPLETED,
                owner=owner,
                result=premature_visible,
                artifacts=artifacts,
            )

        owner, result, artifacts, _ = _stage_fixture(contract.STAGE_RERANK_COMPLETED)
        missing_visible = dict(result)
        policy = dict(result[contract.SECTION_RERANK_POLICY])
        del policy["visible"]
        missing_visible[contract.SECTION_RERANK_POLICY] = policy
        with self.assertRaisesRegex(ValueError, "invalid fields"):
            _checkpoint(
                contract.STAGE_RERANK_COMPLETED,
                owner=owner,
                result=missing_visible,
                artifacts=artifacts,
            )

    def test_missing_extra_and_wrong_section_shapes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing required"):
            _checkpoint(contract.STAGE_PAGE_UNDERSTOOD, result={})
        with self.assertRaisesRegex(ValueError, "unsupported sections"):
            _checkpoint(
                contract.STAGE_IMAGE_ROUTED,
                result={contract.SECTION_PAGE_SUMMARY: {}},
            )
        route = dict(_stage_fixture(contract.STAGE_IMAGE_ROUTED)[1][contract.SECTION_ROUTE_DECISION])
        route["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "invalid fields"):
            _checkpoint(
                contract.STAGE_IMAGE_ROUTED,
                result={contract.SECTION_ROUTE_DECISION: route},
            )

    def test_sensitive_unbounded_and_non_json_results_are_rejected(self):
        base = dict(_stage_fixture(contract.STAGE_PAGE_UNDERSTOOD)[1])
        unit = _unit_result()
        for text in (
            r"C:\private\question.jpg",
            "文件在 C:\\private\\question.jpg",
            "https://example.test/question.jpg",
            "请查看 https://example.test/question.jpg",
            "Bearer secret-value",
            "sk-proj-secret",
        ):
            with self.subTest(text=text):
                candidate = dict(unit)
                candidate["recognized_text_excerpt"] = text
                with self.assertRaisesRegex(ValueError, "path, URL, or secret"):
                    _checkpoint(
                        contract.STAGE_PAGE_UNDERSTOOD,
                        result={
                            contract.SECTION_PAGE_SUMMARY: base[contract.SECTION_PAGE_SUMMARY],
                            contract.SECTION_UNIT_RESULTS: [candidate],
                        },
                    )
        route = dict(_stage_fixture(contract.STAGE_IMAGE_ROUTED)[1][contract.SECTION_ROUTE_DECISION])
        route["prompt"] = "do not persist"
        with self.assertRaisesRegex(ValueError, "forbidden key"):
            _checkpoint(
                contract.STAGE_IMAGE_ROUTED,
                result={contract.SECTION_ROUTE_DECISION: route},
            )
        unit["recognized_text_excerpt"] = "x" * (contract.MAX_EVIDENCE_CHARS + 1)
        with self.assertRaisesRegex(ValueError, "unit text"):
            _checkpoint(
                contract.STAGE_PAGE_UNDERSTOOD,
                result={
                    contract.SECTION_PAGE_SUMMARY: base[contract.SECTION_PAGE_SUMMARY],
                    contract.SECTION_UNIT_RESULTS: [unit],
                },
            )

    def test_sensitive_values_are_rejected_across_the_whole_envelope(self):
        producer_values = _producer().to_dict()
        producer_values["data_version"] = "C:/private"
        with self.assertRaisesRegex(ValueError, "path, URL, or secret"):
            contract.ProducerVersionV1(**producer_values)

        for failure_values in (
            {
                "code": "FAILED",
                "kind": "https://internal",
                "retryable": False,
            },
            {
                "code": "FAILED",
                "kind": "runtime",
                "retryable": False,
                "fallback": "C:/private",
            },
        ):
            with self.subTest(failure=failure_values), self.assertRaisesRegex(
                ValueError, "path, URL, or secret"
            ):
                contract.CheckpointFailureV1(**failure_values)

        with self.assertRaisesRegex(ValueError, "path, URL, or secret"):
            _owner(identity_key="sk-proj-secret")

        owner_values = _owner().to_dict()
        del owner_values["identity_key"]
        with self.assertRaises(TypeError):
            contract.CheckpointOwnerV1(**owner_values)
        for identity_key in ("", "C:private"):
            with self.subTest(identity_key=identity_key), self.assertRaises(ValueError):
                _owner(identity_key=identity_key)

        producer_values = _producer().to_dict()
        for data_version in ("private/file", r"private\file", "private:file"):
            with self.subTest(data_version=data_version), self.assertRaises(ValueError):
                contract.ProducerVersionV1(
                    **dict(producer_values, data_version=data_version)
                )

        owner, result, artifacts, _ = _stage_fixture(contract.STAGE_QUESTION_ANALYZED)
        context = dict(result[contract.SECTION_QUESTION_CONTEXT])
        for secret_text in (
            "sessionid=abc1234567890",
            "cookie=abc1234567890",
            "token=abc1234567890",
            "password=abc1234567890",
        ):
            changed = dict(result)
            changed[contract.SECTION_QUESTION_CONTEXT] = dict(
                context,
                recognized_text_excerpt=secret_text,
            )
            with self.subTest(secret_text=secret_text), self.assertRaisesRegex(
                ValueError, "path, URL, or secret"
            ):
                _checkpoint(
                    contract.STAGE_QUESTION_ANALYZED,
                    owner=owner,
                    result=changed,
                    artifacts=artifacts,
                )

        changed = dict(result)
        changed[contract.SECTION_QUESTION_CONTEXT] = dict(
            context,
            recognized_text_excerpt="杆件荷载比 P/L=2",
        )
        _checkpoint(
            contract.STAGE_QUESTION_ANALYZED,
            owner=owner,
            result=changed,
            artifacts=artifacts,
        )

    def test_nested_contract_objects_require_exact_v1_types_before_access(self):
        class PoisonDuck:
            def __getattribute__(self, name):
                if name.startswith("__"):
                    return object.__getattribute__(self, name)
                raise AssertionError(f"unexpected nested object access: {name}")

        def forbidden_to_dict(self):
            raise AssertionError("unexpected nested to_dict call")

        owner_subclass = type(
            "OwnerSubclass",
            (contract.CheckpointOwnerV1,),
            {"to_dict": forbidden_to_dict},
        )(**_owner().to_dict())
        producer_subclass = type(
            "ProducerSubclass",
            (contract.ProducerVersionV1,),
            {"to_dict": forbidden_to_dict},
        )(**_producer().to_dict())
        failure_subclass = type(
            "FailureSubclass",
            (contract.CheckpointFailureV1,),
            {"to_dict": forbidden_to_dict},
        )("RERANK_FAILED", "external_model", False)
        link_subclass = type(
            "LinkSubclass",
            (contract.ArtifactLinkV1,),
            {"to_dict": forbidden_to_dict},
        )("art_" + "9" * 32, contract.ARTIFACT_ROLE_SOURCE_PAGE)

        for value in (False, PoisonDuck(), owner_subclass):
            with self.subTest(field="owner", value_type=type(value).__name__), self.assertRaisesRegex(
                ValueError, "owner must be CheckpointOwnerV1"
            ):
                _checkpoint(contract.STAGE_IMAGE_ROUTED, owner=value)

            with self.subTest(field="artifact_owner", value_type=type(value).__name__), self.assertRaisesRegex(
                ValueError, "owner must be CheckpointOwnerV1"
            ):
                contract.ArtifactDescriptorV1(
                    artifact_id="art_" + "8" * 32,
                    owner=value,
                    sha256=HASH,
                    byte_size=123_456,
                    media_type="image/jpeg",
                    width_px=1920,
                    height_px=1080,
                    created_at=_timestamp(),
                    expires_at=_timestamp(3),
                    retention_class=contract.RETENTION_NORMAL,
                )

        for value in (False, PoisonDuck(), producer_subclass):
            with self.subTest(field="producer", value_type=type(value).__name__), self.assertRaisesRegex(
                ValueError, "producer must be ProducerVersionV1"
            ):
                _checkpoint(contract.STAGE_IMAGE_ROUTED, producer=value)

        for value in (False, PoisonDuck(), failure_subclass):
            with self.subTest(field="failure", value_type=type(value).__name__), self.assertRaisesRegex(
                ValueError, "failure must be CheckpointFailureV1"
            ):
                _checkpoint(
                    contract.STAGE_RERANK_COMPLETED,
                    outcome=contract.OUTCOME_FAILED,
                    retention_class=contract.RETENTION_FAILED,
                    result={},
                    artifacts=(),
                    failure=value,
                )

        for value in (False, PoisonDuck(), link_subclass):
            with self.subTest(field="artifact_link", value_type=type(value).__name__), self.assertRaisesRegex(
                ValueError, "artifact links must be ArtifactLinkV1"
            ):
                _checkpoint(contract.STAGE_IMAGE_ACCEPTED, artifacts=(value,))

        artifact_tuple_subclass = type("ArtifactTupleSubclass", (tuple,), {})
        with self.assertRaisesRegex(ValueError, "artifacts must be a tuple"):
            _checkpoint(
                contract.STAGE_IMAGE_ACCEPTED,
                artifacts=artifact_tuple_subclass(
                    (_artifact(contract.ARTIFACT_ROLE_SOURCE_PAGE),)
                ),
            )

    def test_input_fingerprint_is_required_and_sha256_shaped(self):
        value = _checkpoint(contract.STAGE_IMAGE_ROUTED)
        self.assertEqual(value.to_dict()["input_fingerprint"], HASH)
        for fingerprint in ("", "not-a-hash", "A" * 64, None):
            with self.subTest(fingerprint=fingerprint), self.assertRaisesRegex(
                ValueError, "input fingerprint"
            ):
                _checkpoint(
                    contract.STAGE_IMAGE_ROUTED,
                    input_fingerprint=fingerprint,
                )

    def test_input_fingerprint_helper_is_canonical_and_binds_every_material(self):
        owner = _owner()
        producer = _producer()
        digests = {
            "source_image": "a" * 64,
            "route_decision": "b" * 64,
        }

        baseline = contract.compute_input_fingerprint_v1(
            stage=contract.STAGE_IMAGE_ROUTED,
            owner=owner,
            producer=producer,
            input_digests=digests,
        )
        reordered = contract.compute_input_fingerprint_v1(
            stage=contract.STAGE_IMAGE_ROUTED,
            owner=owner,
            producer=producer,
            input_digests=dict(reversed(tuple(digests.items()))),
        )
        self.assertRegex(baseline, r"^[0-9a-f]{64}$")
        self.assertEqual(reordered, baseline)

        variants = (
            contract.compute_input_fingerprint_v1(
                stage=contract.STAGE_IMAGE_ACCEPTED,
                owner=owner,
                producer=producer,
                input_digests=digests,
            ),
            contract.compute_input_fingerprint_v1(
                stage=contract.STAGE_IMAGE_ROUTED,
                owner=_owner(workflow_task_revision=3, task_revision=3),
                producer=producer,
                input_digests=digests,
            ),
            contract.compute_input_fingerprint_v1(
                stage=contract.STAGE_IMAGE_ROUTED,
                owner=owner,
                producer=contract.ProducerVersionV1(
                    **dict(producer.to_dict(), component_version="v2")
                ),
                input_digests=digests,
            ),
            contract.compute_input_fingerprint_v1(
                stage=contract.STAGE_IMAGE_ROUTED,
                owner=owner,
                producer=producer,
                input_digests=dict(digests, source_image="c" * 64),
            ),
            contract.compute_input_fingerprint_v1(
                stage=contract.STAGE_IMAGE_ROUTED,
                owner=owner,
                producer=producer,
                input_digests={
                    "source_image": "a" * 64,
                    "routing_input": "b" * 64,
                },
            ),
        )
        self.assertEqual(len({baseline, *variants}), len(variants) + 1)

        invalid_inputs = (
            {},
            [],
            {"source/path": "a" * 64},
            {"access_token": "a" * 64},
            {"source_image": ""},
            {"source_image": "A" * 64},
            {"source_image": False},
        )
        for input_digests in invalid_inputs:
            with self.subTest(input_digests=input_digests), self.assertRaises(ValueError):
                contract.compute_input_fingerprint_v1(
                    stage=contract.STAGE_IMAGE_ROUTED,
                    owner=owner,
                    producer=producer,
                    input_digests=input_digests,
                )
        with self.assertRaises(ValueError):
            contract.compute_input_fingerprint_v1(
                stage="unknown",
                owner=owner,
                producer=producer,
                input_digests=digests,
            )
        with self.assertRaisesRegex(ValueError, "owner must be CheckpointOwnerV1"):
            contract.compute_input_fingerprint_v1(
                stage=contract.STAGE_IMAGE_ROUTED,
                owner=False,
                producer=producer,
                input_digests=digests,
            )
        with self.assertRaisesRegex(ValueError, "producer must be ProducerVersionV1"):
            contract.compute_input_fingerprint_v1(
                stage=contract.STAGE_IMAGE_ROUTED,
                owner=owner,
                producer=False,
                input_digests=digests,
            )

    def test_only_normalized_load_types_are_accepted_and_raw_is_required(self):
        owner, result, artifacts, _ = _stage_fixture(contract.STAGE_QUESTION_ANALYZED)
        for load in ({"type": "点荷载", "raw": "P"}, {"type": "集中", "raw": ""}):
            changed = dict(result)
            changed[contract.SECTION_LOAD_OBSERVATIONS] = [load]
            with self.subTest(load=load), self.assertRaises(ValueError):
                _checkpoint(
                    contract.STAGE_QUESTION_ANALYZED,
                    owner=owner,
                    result=changed,
                    artifacts=artifacts,
                )

    def test_crop_geometry_is_bounded_by_source_and_matches_crop_dimensions(self):
        owner, result, artifacts, _ = _stage_fixture(contract.STAGE_CROP_PREPARED)
        geometry = dict(result[contract.SECTION_CROP_GEOMETRY])

        out_of_bounds = dict(geometry)
        out_of_bounds["pixel_bounds"] = dict(
            geometry["pixel_bounds"],
            right=geometry["source_width_px"] + 1,
        )
        out_of_bounds["crop_width_px"] = (
            out_of_bounds["pixel_bounds"]["right"]
            - out_of_bounds["pixel_bounds"]["left"]
        )
        changed = dict(result)
        changed[contract.SECTION_CROP_GEOMETRY] = out_of_bounds
        with self.assertRaisesRegex(ValueError, "bounds exceed source"):
            _checkpoint(
                contract.STAGE_CROP_PREPARED,
                owner=owner,
                result=changed,
                artifacts=artifacts,
            )

        wrong_size = dict(geometry, crop_width_px=geometry["crop_width_px"] + 1)
        changed[contract.SECTION_CROP_GEOMETRY] = wrong_size
        with self.assertRaisesRegex(ValueError, "dimensions do not match"):
            _checkpoint(
                contract.STAGE_CROP_PREPARED,
                owner=owner,
                result=changed,
                artifacts=artifacts,
            )

    def test_crop_validation_outcome_matches_verdict_and_external_load_gate(self):
        owner, result, artifacts, _ = _stage_fixture(contract.STAGE_CROP_VALIDATED)
        validation = dict(result[contract.SECTION_CROP_VALIDATION])

        for status in ("yes", "not_configured"):
            changed = dict(result)
            changed[contract.SECTION_CROP_VALIDATION] = dict(
                validation,
                external_load_status=status,
            )
            _checkpoint(
                contract.STAGE_CROP_VALIDATED,
                owner=owner,
                result=changed,
                artifacts=artifacts,
            )
            _checkpoint(
                contract.STAGE_CROP_VALIDATED,
                owner=owner,
                outcome=contract.OUTCOME_PARTIAL,
                result=changed,
                artifacts=artifacts,
            )

        for status in ("no", "error"):
            changed = dict(result)
            changed[contract.SECTION_CROP_VALIDATION] = dict(
                validation,
                external_load_status=status,
            )
            _checkpoint(
                contract.STAGE_CROP_VALIDATED,
                owner=owner,
                outcome=contract.OUTCOME_NEEDS_INPUT,
                result=changed,
                artifacts=artifacts,
            )
            with self.assertRaisesRegex(ValueError, "not ready"):
                _checkpoint(
                    contract.STAGE_CROP_VALIDATED,
                    owner=owner,
                    result=changed,
                    artifacts=artifacts,
                )

        review_checks = dict(validation["checks"], image_clear=False)
        review_result = dict(result)
        review_result[contract.SECTION_CROP_VALIDATION] = dict(
            validation,
            verdict="review_required",
            checks=review_checks,
            external_load_status="not_run",
        )
        _checkpoint(
            contract.STAGE_CROP_VALIDATED,
            owner=owner,
            outcome=contract.OUTCOME_NEEDS_INPUT,
            result=review_result,
            artifacts=artifacts,
        )
        with self.assertRaisesRegex(ValueError, "not ready"):
            _checkpoint(
                contract.STAGE_CROP_VALIDATED,
                owner=owner,
                result=review_result,
                artifacts=artifacts,
            )

        invalid_status = dict(result)
        invalid_status[contract.SECTION_CROP_VALIDATION] = dict(
            validation,
            external_load_status="complete",
        )
        with self.assertRaisesRegex(ValueError, "external load status"):
            _checkpoint(
                contract.STAGE_CROP_VALIDATED,
                owner=owner,
                result=invalid_status,
                artifacts=artifacts,
            )

        invalid_combination = dict(review_result)
        invalid_combination[contract.SECTION_CROP_VALIDATION] = dict(
            review_result[contract.SECTION_CROP_VALIDATION],
            external_load_status="yes",
        )
        with self.assertRaisesRegex(ValueError, "verdict and external load status"):
            _checkpoint(
                contract.STAGE_CROP_VALIDATED,
                owner=owner,
                outcome=contract.OUTCOME_NEEDS_INPUT,
                result=invalid_combination,
                artifacts=artifacts,
            )

    def test_candidate_counts_require_bounded_explicit_truncation(self):
        owner, result, artifacts, _ = _stage_fixture(contract.STAGE_COARSE_SEARCH_COMPLETED)
        counts = dict(result[contract.SECTION_CANDIDATE_COUNTS])
        changed = dict(result)
        changed[contract.SECTION_CANDIDATE_COUNTS] = counts
        value = _checkpoint(
            contract.STAGE_COARSE_SEARCH_COMPLETED,
            owner=owner,
            result=changed,
            artifacts=artifacts,
        )
        self.assertTrue(
            value.result[contract.SECTION_CANDIDATE_COUNTS]["scores_truncated"]
        )
        for bad_counts in (
            dict(counts, scores_truncated=False),
            dict(counts, stored_score_count=2),
            dict(counts, positive_score=9),
        ):
            changed[contract.SECTION_CANDIDATE_COUNTS] = bad_counts
            with self.subTest(counts=bad_counts), self.assertRaises(ValueError):
                _checkpoint(
                    contract.STAGE_COARSE_SEARCH_COMPLETED,
                    owner=owner,
                    result=changed,
                    artifacts=artifacts,
                )

    def test_coarse_outcome_matches_dimension_filtered_candidates(self):
        owner, result, artifacts, _ = _stage_fixture(
            contract.STAGE_COARSE_SEARCH_COMPLETED
        )
        counts = dict(result[contract.SECTION_CANDIDATE_COUNTS])

        impossible_scores = dict(result)
        impossible_scores[contract.SECTION_CANDIDATE_COUNTS] = dict(
            counts,
            after_dimension_filter=0,
            scores_truncated=False,
        )
        with self.assertRaisesRegex(
            ValueError, "exceeds dimension-filtered candidates"
        ):
            _checkpoint(
                contract.STAGE_COARSE_SEARCH_COMPLETED,
                owner=owner,
                result=impossible_scores,
                artifacts=artifacts,
            )

        with self.assertRaisesRegex(ValueError, "no-match.*remaining candidates"):
            _checkpoint(
                contract.STAGE_COARSE_SEARCH_COMPLETED,
                owner=owner,
                outcome=contract.OUTCOME_NO_MATCH,
                result=result,
                artifacts=artifacts,
            )

        no_match = dict(result)
        no_match[contract.SECTION_CANDIDATE_COUNTS] = dict(
            counts,
            after_dimension_filter=0,
            stored_score_count=0,
            scores_truncated=False,
        )
        no_match[contract.SECTION_CANDIDATE_SCORES] = []
        no_match[contract.SECTION_FILTER_DECISIONS] = [
            dict(result[contract.SECTION_FILTER_DECISIONS][0], after=0)
        ]
        value = _checkpoint(
            contract.STAGE_COARSE_SEARCH_COMPLETED,
            owner=owner,
            outcome=contract.OUTCOME_NO_MATCH,
            result=no_match,
            artifacts=artifacts,
        )
        self.assertEqual(value.outcome, contract.OUTCOME_NO_MATCH)

        with self.assertRaisesRegex(ValueError, "matched.*requires candidates"):
            _checkpoint(
                contract.STAGE_COARSE_SEARCH_COMPLETED,
                owner=owner,
                outcome=contract.OUTCOME_SUCCESS,
                result=no_match,
                artifacts=artifacts,
            )

        partial = _checkpoint(
            contract.STAGE_COARSE_SEARCH_COMPLETED,
            owner=owner,
            outcome=contract.OUTCOME_PARTIAL,
            result=result,
            artifacts=artifacts,
        )
        self.assertEqual(partial.outcome, contract.OUTCOME_PARTIAL)

    def test_rerank_visibility_and_score_truncation_are_explicit(self):
        owner, result, artifacts, _ = _stage_fixture(contract.STAGE_RERANK_COMPLETED)
        policy = dict(result[contract.SECTION_RERANK_POLICY])
        self.assertEqual(policy["visible"], 1)
        self.assertEqual(policy["stored_score_count"], 1)
        self.assertTrue(policy["scores_truncated"])

        for bad_policy in (
            dict(policy, visible=2),
            dict(policy, stored_score_count=2),
            dict(policy, scores_truncated=False),
            dict(policy, visible=9, stored_score_count=9),
        ):
            changed = dict(result)
            changed[contract.SECTION_RERANK_POLICY] = bad_policy
            with self.subTest(policy=bad_policy), self.assertRaises(ValueError):
                _checkpoint(
                    contract.STAGE_RERANK_COMPLETED,
                    owner=owner,
                    result=changed,
                    artifacts=artifacts,
                )

    def test_rerank_outcome_matches_visible_candidates_and_fallback_policy(self):
        owner, result, artifacts, _ = _stage_fixture(contract.STAGE_RERANK_COMPLETED)
        policy = dict(result[contract.SECTION_RERANK_POLICY])
        hidden_scores = [
            dict(item, visible=False)
            for item in result[contract.SECTION_CANDIDATE_SCORES]
        ]

        hidden_result = dict(result)
        hidden_result[contract.SECTION_RERANK_POLICY] = dict(policy, visible=0)
        hidden_result[contract.SECTION_CANDIDATE_SCORES] = hidden_scores
        with self.assertRaisesRegex(ValueError, "successful rerank.*invalid"):
            _checkpoint(
                contract.STAGE_RERANK_COMPLETED,
                owner=owner,
                result=hidden_result,
                artifacts=artifacts,
            )

        with self.assertRaisesRegex(ValueError, "no-match.*visible"):
            _checkpoint(
                contract.STAGE_RERANK_COMPLETED,
                owner=owner,
                outcome=contract.OUTCOME_NO_MATCH,
                result=result,
                artifacts=artifacts,
            )

        no_match = _checkpoint(
            contract.STAGE_RERANK_COMPLETED,
            owner=owner,
            outcome=contract.OUTCOME_NO_MATCH,
            result=hidden_result,
            artifacts=artifacts,
        )
        self.assertEqual(no_match.result[contract.SECTION_RERANK_POLICY]["visible"], 0)

        fallback_result = dict(result)
        fallback_result[contract.SECTION_RERANK_POLICY] = dict(
            policy,
            reranked=False,
            completed_count=5,
            failed_count=3,
            fallback_used=True,
            reason_code="RERANK_INCOMPLETE_COARSE_FALLBACK",
        )
        partial = _checkpoint(
            contract.STAGE_RERANK_COMPLETED,
            owner=owner,
            outcome=contract.OUTCOME_PARTIAL,
            result=fallback_result,
            artifacts=artifacts,
        )
        self.assertTrue(
            partial.result[contract.SECTION_RERANK_POLICY]["fallback_used"]
        )

        skipped_result = dict(result)
        skipped_result[contract.SECTION_RERANK_POLICY] = dict(
            policy,
            reranked=False,
            completed_count=0,
            failed_count=0,
            fallback_used=True,
            reason_code="RERANK_SKIPPED_NO_IMAGE",
        )
        skipped = _checkpoint(
            contract.STAGE_RERANK_COMPLETED,
            owner=owner,
            outcome=contract.OUTCOME_SKIPPED,
            result=skipped_result,
            artifacts=artifacts,
        )
        self.assertEqual(skipped.outcome, contract.OUTCOME_SKIPPED)

        invalid_fallback = dict(fallback_result)
        invalid_fallback[contract.SECTION_RERANK_POLICY] = dict(
            fallback_result[contract.SECTION_RERANK_POLICY],
            fallback_used=False,
        )
        with self.assertRaisesRegex(ValueError, "fallback.*invalid"):
            _checkpoint(
                contract.STAGE_RERANK_COMPLETED,
                owner=owner,
                outcome=contract.OUTCOME_PARTIAL,
                result=invalid_fallback,
                artifacts=artifacts,
            )

        missing_marker = dict(result)
        missing_marker[contract.SECTION_CANDIDATE_SCORES] = [_candidate_score()]
        with self.assertRaisesRegex(ValueError, "require visibility"):
            _checkpoint(
                contract.STAGE_RERANK_COMPLETED,
                owner=owner,
                result=missing_marker,
                artifacts=artifacts,
            )

        wrong_marker = dict(result)
        wrong_marker[contract.SECTION_CANDIDATE_SCORES] = [
            dict(_candidate_score(), visible=False)
        ]
        with self.assertRaisesRegex(ValueError, "visible count does not match"):
            _checkpoint(
                contract.STAGE_RERANK_COMPLETED,
                owner=owner,
                result=wrong_marker,
                artifacts=artifacts,
            )

        coarse_owner, coarse_result, coarse_artifacts, _ = _stage_fixture(
            contract.STAGE_COARSE_SEARCH_COMPLETED
        )
        coarse_with_marker = dict(coarse_result)
        coarse_with_marker[contract.SECTION_CANDIDATE_SCORES] = [
            dict(_candidate_score(), visible=False)
        ]
        with self.assertRaisesRegex(ValueError, "cannot contain visibility"):
            _checkpoint(
                contract.STAGE_COARSE_SEARCH_COMPLETED,
                owner=coarse_owner,
                result=coarse_with_marker,
                artifacts=coarse_artifacts,
            )

    def test_candidate_records_are_unique_bounded_and_path_free(self):
        owner, result, artifacts, _ = _stage_fixture(contract.STAGE_COARSE_SEARCH_COMPLETED)
        duplicate = dict(result)
        duplicate[contract.SECTION_CANDIDATE_SCORES] = [_candidate_score(), _candidate_score()]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            _checkpoint(
                contract.STAGE_COARSE_SEARCH_COMPLETED,
                owner=owner,
                result=duplicate,
                artifacts=artifacts,
            )
        with_path = dict(_candidate_score())
        with_path["path"] = r"D:\bank\q1.jpg"
        duplicate[contract.SECTION_CANDIDATE_SCORES] = [with_path]
        with self.assertRaisesRegex(ValueError, "forbidden key"):
            _checkpoint(
                contract.STAGE_COARSE_SEARCH_COMPLETED,
                owner=owner,
                result=duplicate,
                artifacts=artifacts,
            )

    def test_failure_is_code_only_and_does_not_require_stage_payload(self):
        failure = contract.CheckpointFailureV1(
            code="RERANK_PROVIDER_FAILED",
            kind="external_model",
            retryable=True,
            fallback="coarse_order",
            last_successful_checkpoint_id="ckpt_" + "8" * 32,
        )
        value = _checkpoint(
            contract.STAGE_RERANK_COMPLETED,
            outcome=contract.OUTCOME_FAILED,
            retention_class=contract.RETENTION_FAILED,
            result={},
            artifacts=(),
            failure=failure,
        )
        self.assertEqual(value.failure.code, "RERANK_PROVIDER_FAILED")
        with self.assertRaisesRegex(ValueError, "requires failure"):
            _checkpoint(
                contract.STAGE_RERANK_COMPLETED,
                outcome=contract.OUTCOME_FAILED,
                retention_class=contract.RETENTION_FAILED,
                result={},
                artifacts=(),
            )
        with self.assertRaisesRegex(ValueError, "normal retention"):
            _checkpoint(
                contract.STAGE_RERANK_COMPLETED,
                outcome=contract.OUTCOME_FAILED,
                result={},
                artifacts=(),
                failure=failure,
            )
        with self.assertRaises(ValueError):
            contract.CheckpointFailureV1(
                code="FAILED",
                kind=r"ValueError: C:\secret",
                retryable=True,
            )

    def test_artifact_roles_are_stage_bounded_and_required_only_for_complete_success(self):
        with self.assertRaisesRegex(ValueError, "missing required artifact"):
            _checkpoint(contract.STAGE_IMAGE_ACCEPTED, artifacts=())
        partial = _checkpoint(
            contract.STAGE_IMAGE_ACCEPTED,
            outcome=contract.OUTCOME_PARTIAL,
            artifacts=(),
        )
        self.assertEqual(partial.artifacts, ())
        with self.assertRaisesRegex(ValueError, "not allowed"):
            _checkpoint(
                contract.STAGE_IMAGE_ROUTED,
                artifacts=(_artifact(contract.ARTIFACT_ROLE_ANSWER_IMAGE),),
            )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            _checkpoint(
                contract.STAGE_IMAGE_ACCEPTED,
                artifacts=(
                    _artifact(contract.ARTIFACT_ROLE_SOURCE_PAGE),
                    _artifact(contract.ARTIFACT_ROLE_SOURCE_PAGE),
                ),
            )

    def test_answer_no_match_is_representable_without_selection_or_artifact(self):
        owner, _, _, _ = _stage_fixture(contract.STAGE_ANSWER_PREPARED)
        result = {
            contract.SECTION_DELIVERY: {
                "answer_artifact_count": 0,
                "media_status": "not_available",
                "delivery_code": "NO_MATCH",
            }
        }
        value = _checkpoint(
            contract.STAGE_ANSWER_PREPARED,
            owner=owner,
            outcome=contract.OUTCOME_NO_MATCH,
            result=result,
            artifacts=(),
        )
        self.assertEqual(value.outcome, contract.OUTCOME_NO_MATCH)

        selected = dict(result)
        selected[contract.SECTION_SELECTION] = {
            "candidate_id": "candidate_1",
            "selected_rank": 1,
            "candidate_generation": "2:1",
            "selection_source": "user",
        }
        with self.assertRaisesRegex(ValueError, "cannot contain selection"):
            _checkpoint(
                contract.STAGE_ANSWER_PREPARED,
                owner=owner,
                outcome=contract.OUTCOME_NO_MATCH,
                result=selected,
                artifacts=(),
            )

        invalid_deliveries = (
            (
                {
                    "answer_artifact_count": 1,
                    "media_status": "not_available",
                    "delivery_code": "NO_MATCH",
                },
                (_artifact(contract.ARTIFACT_ROLE_ANSWER_IMAGE),),
            ),
            (
                {
                    "answer_artifact_count": 0,
                    "media_status": "complete",
                    "delivery_code": "NO_MATCH",
                },
                (),
            ),
            (
                {
                    "answer_artifact_count": 0,
                    "media_status": "not_available",
                    "delivery_code": "ANSWER_READY",
                },
                (),
            ),
        )
        for delivery, linked_artifacts in invalid_deliveries:
            with self.subTest(delivery=delivery), self.assertRaisesRegex(
                ValueError, "invalid delivery"
            ):
                _checkpoint(
                    contract.STAGE_ANSWER_PREPARED,
                    owner=owner,
                    outcome=contract.OUTCOME_NO_MATCH,
                    result={contract.SECTION_DELIVERY: delivery},
                    artifacts=linked_artifacts,
                )

    def test_successful_answer_still_requires_selection(self):
        owner, result, artifacts, _ = _stage_fixture(contract.STAGE_ANSWER_PREPARED)
        missing_selection = dict(result)
        del missing_selection[contract.SECTION_SELECTION]
        with self.assertRaisesRegex(ValueError, "missing selection"):
            _checkpoint(
                contract.STAGE_ANSWER_PREPARED,
                owner=owner,
                result=missing_selection,
                artifacts=artifacts,
            )

    def test_retention_matrix_is_finite_and_capacity_requires_explicit_values(self):
        self.assertEqual(
            {
                name: (
                    value.checkpoint_default_days,
                    value.checkpoint_max_days,
                    value.artifact_default_days,
                    value.artifact_max_days,
                )
                for name, value in contract.RETENTION_POLICIES.items()
            },
            {
                "normal": (30, 30, 3, 3),
                "failed": (30, 30, 7, 7),
                "feedback": (30, 365, 30, 365),
                "investigation": (30, 90, 30, 90),
            },
        )
        capacity = contract.EvidenceCapacityPolicyV1(
            max_checkpoint_rows=10_000,
            max_artifact_rows=20_000,
            max_audit_rows=50_000,
            max_trace_rows=100_000,
            max_artifact_bytes=5_000_000_000,
            min_free_bytes=1_000_000_000,
            max_artifacts_per_checkpoint=50,
        )
        self.assertEqual(
            capacity.to_dict(),
            {
                "max_checkpoint_rows": 10_000,
                "max_artifact_rows": 20_000,
                "max_audit_rows": 50_000,
                "max_trace_rows": 100_000,
                "max_artifact_bytes": 5_000_000_000,
                "min_free_bytes": 1_000_000_000,
                "max_artifacts_per_checkpoint": 50,
            },
        )
        for field in capacity.to_dict():
            values = capacity.to_dict()
            values[field] = 0
            with self.subTest(capacity_field=field), self.assertRaises(ValueError):
                contract.EvidenceCapacityPolicyV1(**values)
        with self.assertRaises(TypeError):
            contract.EvidenceCapacityPolicyV1(
                max_checkpoint_rows=10_000,
                max_artifact_rows=20_000,
                max_audit_rows=50_000,
                max_artifact_bytes=5_000_000_000,
                min_free_bytes=1_000_000_000,
                max_artifacts_per_checkpoint=50,
            )
        with self.assertRaisesRegex(ValueError, "retention exceeds"):
            _checkpoint(contract.STAGE_IMAGE_ROUTED, expires_at=_timestamp(31))
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            _checkpoint(
                contract.STAGE_IMAGE_ROUTED,
                occurred_at="2026-09-04T08:00:00",
            )

    def test_artifact_descriptor_has_owner_hash_dimensions_expiry_and_tombstone(self):
        descriptor = contract.ArtifactDescriptorV1(
            artifact_id="art_" + "9" * 32,
            owner=_owner(),
            sha256=HASH,
            byte_size=123_456,
            media_type="image/jpeg",
            width_px=1920,
            height_px=1080,
            created_at=_timestamp(),
            expires_at=_timestamp(3),
            retention_class=contract.RETENTION_NORMAL,
        )
        self.assertNotIn("path", descriptor.to_dict())
        with self.assertRaisesRegex(ValueError, "retention exceeds"):
            contract.ArtifactDescriptorV1(
                **dict(descriptor.to_dict(), expires_at=_timestamp(4), owner=_owner())
            )
        purged = contract.ArtifactDescriptorV1(
            **dict(
                descriptor.to_dict(),
                owner=_owner(),
                status=contract.ARTIFACT_PURGED,
                purged_at=_timestamp(2),
                purge_reason="expired",
            )
        )
        self.assertEqual(purged.status, "purged")
        with self.assertRaisesRegex(ValueError, "requires purge metadata"):
            contract.ArtifactDescriptorV1(
                **dict(descriptor.to_dict(), owner=_owner(), status=contract.ARTIFACT_PURGED)
            )

    def test_checkpoint_and_artifact_accept_every_upload_media_type(self):
        self.assertEqual(
            contract.IMAGE_MEDIA_TYPES,
            {
                "image/jpeg",
                "image/png",
                "image/webp",
                "image/gif",
                "image/bmp",
            },
        )
        for index, media_type in enumerate(sorted(contract.IMAGE_MEDIA_TYPES), start=1):
            metadata = _image_metadata()
            metadata["mime_type"] = media_type
            checkpoint = _checkpoint(
                contract.STAGE_IMAGE_ACCEPTED,
                result={contract.SECTION_IMAGE_METADATA: metadata},
            )
            self.assertEqual(
                checkpoint.result[contract.SECTION_IMAGE_METADATA]["mime_type"],
                media_type,
            )
            descriptor = contract.ArtifactDescriptorV1(
                artifact_id=f"art_{index:032x}",
                owner=_owner(),
                sha256=HASH,
                byte_size=123_456,
                media_type=media_type,
                width_px=1920,
                height_px=1080,
                created_at=_timestamp(),
                expires_at=_timestamp(3),
                retention_class=contract.RETENTION_NORMAL,
            )
            self.assertEqual(descriptor.media_type, media_type)

    def test_producer_requires_release_and_pairs_model_identity(self):
        with self.assertRaisesRegex(ValueError, "code revision"):
            contract.ProducerVersionV1("main", "component", "v1")
        with self.assertRaisesRegex(ValueError, "must be paired"):
            contract.ProducerVersionV1(
                REVISION,
                "component",
                "v1",
                model_provider="qwen",
            )
        with self.assertRaisesRegex(ValueError, "prompt hash"):
            contract.ProducerVersionV1(
                REVISION,
                "component",
                "v1",
                model_provider="qwen",
                model_name="qwen-vl-max",
                prompt_sha256="not-a-hash",
            )

    def test_contract_values_are_frozen(self):
        value = _checkpoint(contract.STAGE_IMAGE_ROUTED)
        with self.assertRaises(FrozenInstanceError):
            value.stage = contract.STAGE_PAGE_UNDERSTOOD
        with self.assertRaises(TypeError):
            contract.STAGE_CONTRACTS["new"] = value


if __name__ == "__main__":
    unittest.main()
