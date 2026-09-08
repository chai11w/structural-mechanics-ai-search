"""Bounded, best-effort A2 evidence capture behind explicit admission."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import stat
from threading import Lock
from typing import Any

from PIL import Image

from tiku_agent.checkpoint_capture_gate import A2CaptureAdmissionV1, A2CheckpointCaptureGateV1
from tiku_agent.checkpoint_capture import A2CheckpointContextV1, build_a2_checkpoint
from tiku_agent.checkpoint_contract import (
    ARTIFACT_ROLE_SOURCE_PAGE, MAX_ARTIFACT_BYTES,
    RETENTION_NORMAL, RETENTION_FAILED, SCOPE_WORKFLOW, ArtifactLinkV1, IntermediateCheckpointV1,
    ProducerVersionV1,
)
from tiku_agent.checkpoint_store import CheckpointStoreError
from tiku_agent.checkpoint_bank_reference import CheckpointBankCatalog
from tiku_agent.checkpoint_stage_input import freeze_a2_stage_input, materialize_a2_stage_input
from tiku_agent.a2_checkpoint_stages import (
    build_answer_prepared_checkpoint, build_coarse_search_checkpoint,
    build_question_analyzed_checkpoint, build_rerank_checkpoint,
)
from tiku_agent.tool_result import ToolOutcome, ToolResult
from tiku_shared.trace_events import record_trace_event
from tiku_shared.trace_context import is_valid_trace_id


@dataclass(frozen=True)
class A2CaptureRecordResultV1:
    attempted: bool
    stored: bool
    reason_code: str
    checkpoint_id: str = ""
    outcome: str = ""


class A2CheckpointRecorderV1:
    def __init__(
        self, store: Any, *, producer: ProducerVersionV1,
        media_root: str | Path | None = None,
        bank_root: str | Path | None = None,
        gate: A2CheckpointCaptureGateV1 | None = None, clock: Any | None = None,
    ) -> None:
        if not callable(getattr(store, "put_checkpoint", None)):
            raise TypeError("checkpoint recorder requires a checkpoint store")
        if type(producer) is not ProducerVersionV1 or producer.code_revision == "0" * 40:
            raise ValueError("checkpoint recorder requires an explicit producer revision")
        self.store = store
        self.producer = producer
        self.media_root = Path(media_root).resolve() if media_root is not None else None
        if bank_root is None:
            from search import ROOT
            bank_root = ROOT
        self.bank_catalog = CheckpointBankCatalog({"main": bank_root})
        self.gate = gate or A2CheckpointCaptureGateV1()
        self.clock = clock or (lambda: datetime.now(UTC))
        self._lock = Lock()
        self._counts = {"stored": 0, "rejected": 0}
        self._last_failure = ""

    def _result(self, stored: bool, code: str, checkpoint_id: str = "", outcome: str = "") -> A2CaptureRecordResultV1:
        with self._lock:
            key = "stored" if stored else "rejected"
            self._counts[key] = min(2_147_483_647, self._counts[key] + 1)
            if not stored:
                self._last_failure = code
        return A2CaptureRecordResultV1(True, stored, code, checkpoint_id, outcome)

    def health(self) -> dict[str, object]:
        with self._lock:
            return {
                "status": "disabled" if not self.gate.enabled else "degraded" if self._last_failure else "ok",
                "current_reasons": [self._last_failure] if self._last_failure else [],
                "counters": dict(self._counts), "last_failure_code": self._last_failure,
            }

    def input_unavailable(self) -> None:
        self._result(False, "CAPTURE_INPUT_UNAVAILABLE")

    def last_successful(self, context: A2CheckpointContextV1) -> str:
        try:
            checkpoint = self.store.latest_successful_checkpoint(context.owner(), actor_key="a2_capture")
            return checkpoint.checkpoint_id if checkpoint is not None else ""
        except Exception:
            self._result(False, "PREDECESSOR_UNAVAILABLE")
            return ""

    @staticmethod
    def _digest(value: object) -> str:
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False, separators=(",", ":"))
        return sha256(encoded.encode("utf-8")).hexdigest()

    def read_image(self, raw_path: object) -> bytes:
        if self.media_root is None:
            raise ValueError("capture media root is required")
        path = Path(str(raw_path))
        if not path.is_absolute() or not path.is_relative_to(self.media_root):
            raise ValueError("capture image is outside the media root")
        for part in (path, *path.parents):
            details = part.lstat()
            if part.is_symlink() or getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise ValueError("capture image path contains a link")
        if not path.resolve().is_relative_to(self.media_root):
            raise ValueError("capture image is outside the media root")
        with path.open("rb") as stream:
            content = stream.read(MAX_ARTIFACT_BYTES + 1)
        if not content or len(content) > MAX_ARTIFACT_BYTES:
            raise ValueError("capture image exceeds the evidence bound")
        return content

    def capture_stage(
        self, context: A2CheckpointContextV1, *, admission: A2CaptureAdmissionV1,
        stage: str, tool_result: ToolResult, payload: dict[str, Any] | None = None,
        predecessor_checkpoint_id: str = "", last_successful_checkpoint_id: str = "",
    ) -> A2CaptureRecordResultV1:
        decision = self.gate.decide(admission)
        if not decision.permitted:
            return A2CaptureRecordResultV1(False, False, decision.reason_code)
        common = None
        try:
            # Validate identity before any filesystem or Store access.
            if not is_valid_trace_id(context.trace_id):
                return self._result(False, "CAPTURE_TRACE_REQUIRED")
            context.owner()
            tool_result, payload = materialize_a2_stage_input(freeze_a2_stage_input(tool_result, payload or {}))
            metadata = tool_result.data.get("checkpoint_producer") or tool_result.data.get("checkpoint_rerank", {}).get("producer", {})
            if metadata:
                context = replace(context, producer=replace(context.producer, **{
                    key: metadata[key] for key in ("model_provider", "model_name", "prompt_sha256") if key in metadata
                }))
            now = self.clock()
            if not isinstance(now, datetime) or now.tzinfo is None:
                return self._result(False, "CAPTURE_CLOCK_INVALID")
            payload = dict(payload or {})
            digests = dict(payload.get("input_digests") or {"stage_input": self._digest(payload.get("inputs", {}))})
            common = {
                "occurred_at": now.astimezone(UTC).isoformat(),
                "expires_at": (now.astimezone(UTC) + timedelta(days=30)).isoformat(),
                "input_digests": digests,
            }
            if stage == "image_accepted":
                context = replace(context, scope=SCOPE_WORKFLOW, search_id="", candidate_generation="")
                content = self.read_image(payload["image_path"])
                with Image.open(BytesIO(content)) as image:
                    metadata = {
                        "width_px": image.width, "height_px": image.height,
                        "byte_size": len(content), "mime_type": Image.MIME[image.format],
                        "sha256": sha256(content).hexdigest(),
                        "orientation_status": "not_run", "applied_rotation_degrees": 0,
                    }
                descriptor = self.store.put_artifact(context.owner(), content, retention_class=RETENTION_NORMAL)
                common["input_digests"] = {"source_image": metadata["sha256"]}
                checkpoint = build_a2_checkpoint(
                    context, stage=stage, result={"image_metadata": metadata}, tool_result=tool_result,
                    artifacts=(ArtifactLinkV1(descriptor.artifact_id, ARTIFACT_ROLE_SOURCE_PAGE, 1),), **common,
                )
            elif stage == "image_routed":
                context = replace(context, scope=SCOPE_WORKFLOW, search_id="", candidate_generation="")
                checkpoint = build_a2_checkpoint(
                    context, stage=stage, result={"route_decision": payload["route_decision"]},
                    tool_result=tool_result, **common,
                )
            elif stage == "question_analyzed":
                checkpoint = build_question_analyzed_checkpoint(context, tool_result, **common)
            elif stage == "coarse_search_completed":
                checkpoint = build_coarse_search_checkpoint(context, tool_result, bank_catalog=self.bank_catalog,
                    bank_chapter=payload.get("inputs", {}).get("chapter", ""), **common)
            elif stage == "rerank_completed":
                checkpoint = build_rerank_checkpoint(context, tool_result, candidates=payload.get("candidates", []),
                    bank_catalog=self.bank_catalog, bank_chapter=payload.get("inputs", {}).get("chapter", ""), **common)
            elif stage == "answer_prepared":
                references = []
                paths = payload.get("answer_source_paths") or []
                if tool_result.outcome not in {ToolOutcome.NO_MATCH, ToolOutcome.ERROR}:
                    for path in paths[:50]:
                        try:
                            references.append(self.bank_catalog.reference(path,
                                chapter=(payload.get("selected_candidate") or {}).get("chapter")
                                or payload.get("inputs", {}).get("chapter", "")).to_dict())
                        except Exception:
                            self._result(False, "ANSWER_REFERENCE_UNAVAILABLE")
                    if not references or len(references) != len(paths):
                        tool_result = ToolResult(outcome=ToolOutcome.PARTIAL, code="ANSWER_REFERENCE_UNAVAILABLE", error_category="evidence")
                checkpoint = build_answer_prepared_checkpoint(
                    context, tool_result, selected_rank=int(payload.get("selected_rank") or 1),
                    selected_candidate=payload.get("selected_candidate"),
                    candidate_generation=context.candidate_generation,
                    answer_refs=references, **common,
                )
            else:
                return self._result(False, "CAPTURE_STAGE_UNSUPPORTED")
            failure = checkpoint.failure
            if failure is not None:
                failure = replace(failure, last_successful_checkpoint_id=last_successful_checkpoint_id)
            if checkpoint.outcome == "failed" and payload.get("source_image_path"):
                try:
                    source_context = replace(context, scope=SCOPE_WORKFLOW, search_id="", unit_id="",
                        candidate_generation="", task_revision=context.workflow_task_revision)
                    descriptor = self.store.put_artifact(
                        source_context.owner(), self.read_image(payload["source_image_path"]), retention_class=RETENTION_FAILED,
                    )
                    checkpoint = replace(checkpoint, artifacts=(ArtifactLinkV1(descriptor.artifact_id, ARTIFACT_ROLE_SOURCE_PAGE, 1),))
                except Exception:
                    self._result(False, "FAILED_ARTIFACT_UNAVAILABLE")
            checkpoint = replace(checkpoint, predecessor_checkpoint_id=predecessor_checkpoint_id, failure=failure)
        except CheckpointStoreError:
            return self._result(False, "STORE_REJECTED")
        except Exception:
            rejected = self._result(False, "CAPTURE_INVALID")
            if common is not None and stage not in {"image_accepted", "image_routed"}:
                try:
                    checkpoint = build_a2_checkpoint(
                        context, stage=stage, result={}, **common,
                        tool_result=ToolResult(outcome=ToolOutcome.ERROR, code="CAPTURE_INVALID", error_category="evidence"),
                        predecessor_checkpoint_id=predecessor_checkpoint_id,
                        last_successful_checkpoint_id=last_successful_checkpoint_id,
                    )
                    return self.record(checkpoint, admission=admission)
                except Exception:
                    pass
            return rejected
        return self.record(checkpoint, admission=admission)

    def record(self, checkpoint: IntermediateCheckpointV1, *, admission: A2CaptureAdmissionV1) -> A2CaptureRecordResultV1:
        decision = self.gate.decide(admission)
        if not decision.permitted:
            return A2CaptureRecordResultV1(False, False, decision.reason_code)
        if type(checkpoint) is not IntermediateCheckpointV1:
            return self._result(False, "CAPTURE_INVALID")
        try:
            stored = self.store.put_checkpoint(checkpoint)
        except CheckpointStoreError:
            return self._result(False, "STORE_REJECTED")
        except Exception:
            return self._result(False, "STORE_UNAVAILABLE")
        if type(stored) is not IntermediateCheckpointV1:
            return self._result(False, "STORE_INVALID")
        try:
            record_trace_event(
                "stage_finished", stage=stored.stage,
                outcome="error" if stored.outcome == "failed" else stored.outcome,
                session_key=stored.owner.session_key,
                identity_key=stored.owner.identity_key,
                workflow_search_id=stored.owner.workflow_search_id,
                search_id=stored.owner.search_id,
                unit_id=stored.owner.unit_id,
                safe_attributes={"completed": True, "checkpoint_id": stored.checkpoint_id},
            )
        except Exception:
            pass
        return self._result(True, "CAPTURE_STORED", stored.checkpoint_id, stored.outcome)


__all__ = ["A2CaptureRecordResultV1", "A2CheckpointRecorderV1"]
