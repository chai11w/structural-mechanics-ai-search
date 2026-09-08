"""A3 evidence capture sharing the A2 lifecycle and audited Artifact Store."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from tiku_agent.a2_checkpoint_recorder import A2CheckpointRecorderV1, A2CaptureRecordResultV1
from tiku_agent.a3_checkpoint_stages import page_result, crop_result, validation_result
from tiku_agent.a3_auto_crop import GlmA3AutoCropper
from tiku_agent.a3_models import QwenA3PageObserver, QwenA3CropVerifier
from tiku_agent.checkpoint_capture import A2CheckpointContextV1, build_a2_checkpoint
from tiku_agent.checkpoint_capture_gate import A2CaptureAdmissionV1
from tiku_agent.checkpoint_contract import ArtifactLinkV1, SCOPE_WORKFLOW, RETENTION_NORMAL, RETENTION_FAILED
from tiku_agent.session_artifacts import session_key
from tiku_agent.tool_result import ToolResult, ToolOutcome
from tiku_agent.checkpoint_stage_input import FrozenCheckpointInput
from tiku_shared.trace_context import current_trace_id, current_request_id, is_valid_trace_id


class A3CheckpointRecorderV1(A2CheckpointRecorderV1):
    def __init__(self, *args, a3_media_root, **kwargs):
        super().__init__(*args, **kwargs)
        self._page_reader = A2CheckpointRecorderV1(
            self.store, producer=self.producer, media_root=a3_media_root, gate=self.gate,
        )

    def read_image(self, raw_path):
        path = Path(str(raw_path))
        if path.is_absolute() and path.is_relative_to(self._page_reader.media_root):
            return self._page_reader.read_image(path)
        return super().read_image(raw_path)

    def parent_checkpoint(self, owner):
        parent = replace(owner, scope=SCOPE_WORKFLOW, search_id="", candidate_generation="",
                         task_revision=owner.workflow_task_revision)
        checkpoint = self.store.latest_successful_checkpoint(parent, actor_key="a3_capture")
        if checkpoint is None and parent.unit_id:
            checkpoint = self.store.latest_successful_checkpoint(replace(parent, unit_id=""), actor_key="a3_capture")
        return checkpoint

    def last_successful(self, context):
        try:
            checkpoint = self.store.latest_successful_checkpoint(context.owner(), actor_key="a3_capture")
            checkpoint = checkpoint or self.parent_checkpoint(context.owner())
            return checkpoint.checkpoint_id if checkpoint else ""
        except Exception:
            self._result(False, "PREDECESSOR_UNAVAILABLE")
            return ""

    def record(self, checkpoint, *, admission):
        if not self.gate.decide(admission).permitted:
            return super().record(checkpoint, admission=admission)
        if checkpoint.owner.scope != SCOPE_WORKFLOW:
            try:
                parent = self.parent_checkpoint(checkpoint.owner)
                links = list(checkpoint.artifacts)
                for link in parent.artifacts if parent else ():
                    if link.role in {"question_crop", "source_page"} and not any(item.role == link.role for item in links):
                        if len(links) < self.store.capacity.max_artifacts_per_checkpoint:
                            try:
                                self.store.read_artifact(link.artifact_id, checkpoint_id=parent.checkpoint_id,
                                    actor_key="a3_capture", expected_checkpoint_owner=parent.owner)
                                links.append(link)
                            except Exception:
                                self._result(False, "PARENT_ARTIFACT_UNAVAILABLE")
                checkpoint = replace(checkpoint, artifacts=tuple(links))
            except Exception:
                self._result(False, "PARENT_EVIDENCE_UNAVAILABLE")
        return super().record(checkpoint, admission=admission)

    def capture_parent(self, state, *, identity_key, stage, unit_id="", record=None,
                       method="automatic", failure_code="", producer_client=None):
        admission = A2CaptureAdmissionV1("search", bool(identity_key), True, True, True, True)
        decision = self.gate.decide(admission)
        if not decision.permitted:
            return A2CaptureRecordResultV1(False, False, decision.reason_code)
        context = common = None
        predecessor = ""
        try:
            if not is_valid_trace_id(current_trace_id()):
                return self._result(False, "CAPTURE_TRACE_REQUIRED")
            context = A2CheckpointContextV1(
                trace_id=current_trace_id(), request_id=current_request_id(),
                session_key=session_key(state.session_id), identity_key=identity_key,
                workflow_search_id=state.workflow_search_id, search_id="", unit_id=unit_id,
                workflow_task_revision=state.task_revision, task_revision=state.task_revision,
                producer=replace(self.producer, component="a3_runtime", policy_version="a3-capture-v1"),
                scope=SCOPE_WORKFLOW,
            )
            context.owner()
            record_keys = {"path", "bounds", "model_bbox", "bbox", "grounding_status", "reason_codes",
                           "binding_evidence", "verification_checks", "external_load_status", "validation_status"}
            snapshot = FrozenCheckpointInput.capture({
                "page": page_result(state.page_understanding, state.units) if state.page_understanding else {},
                "selected": page_result(state.page_understanding, [state.unit(unit_id)]) if unit_id else {},
                "source_page": str(state.source_page_path), "entry_route": state.entry_route,
                "crop_page": {key: state.auto_crop_page[key] for key in ("schema_version", "page_status") if key in state.auto_crop_page},
                "record": {key: value for key, value in (record or {}).items() if key in record_keys},
            }).materialize()
            record = snapshot["record"]
            try:
                model = getattr(producer_client, "model", "")
                prompt = getattr(producer_client, "prompt_path", None)
                if isinstance(model, str) and model:
                    provider = ("dashscope" if isinstance(producer_client, (QwenA3PageObserver, QwenA3CropVerifier))
                                else "zhipu" if isinstance(producer_client, GlmA3AutoCropper) else "")
                    context = replace(context, producer=replace(context.producer, model_name=model, model_provider=provider))
                if isinstance(prompt, Path):
                    context = replace(context, producer=replace(context.producer,
                        prompt_sha256=sha256(prompt.read_text(encoding="utf-8").strip().encode("utf-8")).hexdigest()))
            except Exception:
                self._result(False, "A3_PRODUCER_UNAVAILABLE")
            predecessor = self.last_successful(context)
            now = self.clock()
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                return self._result(False, "CAPTURE_CLOCK_INVALID")
            now = now.astimezone(UTC)
            common = dict(occurred_at=now.isoformat(), expires_at=(now + timedelta(days=30)).isoformat(),
                          input_digests={"stage_input": self._digest({"stage": stage, "unit": unit_id})})
            result = {}
            links = []
            source = ToolResult.success(code="A3_STAGE_COMPLETED")
            record = record or {}
            if failure_code:
                source = ToolResult(outcome=ToolOutcome.ERROR, code=failure_code, error_category="a3", retryable=True)
            elif stage == "image_routed":
                result = {"route_decision": {"route": snapshot["entry_route"], "decision_source": "a3_router", "reason_code": "IMAGE_ROUTED"}}
            elif stage == "page_understood":
                result = snapshot["page"]
                if not result["page_summary"]["searchable_unit_count"]:
                    source = ToolResult.no_match(code="NO_SEARCHABLE_UNITS")
            elif stage == "crop_validated":
                record = dict(record)
                if record.get("external_load_status") not in {"not_run", "not_configured", "yes", "no", "error"}:
                    record["external_load_status"] = "error"
                result = validation_result(record)
                if record["validation_status"] != "auto_ready":
                    source = ToolResult(outcome=ToolOutcome.NEEDS_INPUT, code="CROP_REVIEW_REQUIRED")

            source_content = None
            try:
                source_content = self.read_image(snapshot["source_page"])
            except Exception:
                self._result(False, "A3_SOURCE_UNAVAILABLE")
                if source.outcome is ToolOutcome.SUCCESS:
                    source = ToolResult(outcome=ToolOutcome.PARTIAL, code="A3_SOURCE_UNAVAILABLE", error_category="evidence")
            input_digests = {"source_image": sha256(source_content).hexdigest()} if source_content else dict(common["input_digests"])
            if stage == "image_accepted":
                with Image.open(BytesIO(source_content)) as image:
                    result = {"image_metadata": {
                        "width_px": image.width, "height_px": image.height, "byte_size": len(source_content),
                        "mime_type": Image.MIME[image.format], "sha256": input_digests["source_image"],
                        "orientation_status": "not_run", "applied_rotation_degrees": 0,
                    }}
            crop_content = None
            if unit_id and record.get("path"):
                crop_content = self.read_image(record["path"])
                input_digests["question_crop"] = sha256(crop_content).hexdigest()
                if stage == "crop_prepared" and not failure_code:
                    with Image.open(BytesIO(source_content)) as image, Image.open(BytesIO(crop_content)) as crop:
                        result = crop_result(unit_id, record, snapshot["crop_page"] if method == "automatic" else {},
                                             source_size=ImageOps.exif_transpose(image).size, crop_size=crop.size, method=method)
            if unit_id:
                input_digests["selected_unit"] = self._digest(snapshot["selected"])
                input_digests["page_context"] = self._digest(snapshot["page"])
            if stage == "crop_prepared":
                input_digests["geometry"] = self._digest(result.get("crop_geometry", {}))
            common["input_digests"] = input_digests
            # Required crop first; an optional source link must not exhaust its slot.
            assets = [(context.owner(), crop_content, "question_crop")] if crop_content else []
            if source_content:
                assets.append((replace(context.owner(), unit_id=""), source_content, "source_page"))
            for owner, content, role in assets[:self.store.capacity.max_artifacts_per_checkpoint]:
                try:
                    artifact = self.store.put_artifact(owner, content,
                        retention_class=RETENTION_FAILED if failure_code else RETENTION_NORMAL)
                    links.append(ArtifactLinkV1(artifact.artifact_id, role, 1))
                except Exception:
                    self._result(False, "A3_ARTIFACT_UNAVAILABLE")
                    if source.outcome in {ToolOutcome.SUCCESS, ToolOutcome.PARTIAL}:
                        source = ToolResult(outcome=ToolOutcome.PARTIAL, code="A3_ARTIFACT_UNAVAILABLE", error_category="evidence")
            checkpoint = build_a2_checkpoint(context, stage=stage, result=result, tool_result=source,
                artifacts=links, predecessor_checkpoint_id=predecessor,
                last_successful_checkpoint_id=predecessor, **common)
        except Exception:
            rejected = self._result(False, "A3_CAPTURE_INVALID")
            if context is None or common is None:
                return rejected
            try:
                checkpoint = build_a2_checkpoint(context, stage=stage, result={}, **common,
                    tool_result=ToolResult(outcome=ToolOutcome.ERROR, code="A3_CAPTURE_INVALID", error_category="evidence"),
                    predecessor_checkpoint_id=predecessor, last_successful_checkpoint_id=predecessor)
            except Exception:
                return rejected
        return self.record(checkpoint, admission=admission)
