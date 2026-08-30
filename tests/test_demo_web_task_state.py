from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from tiku_agent import task_state_contract as contract


ROOT = Path(__file__).resolve().parents[1]
TASK_STATE_SCRIPT = ROOT / "tiku_agent" / "demo_web" / "task_state.js"


def _a2_snapshot() -> contract.TaskStateSnapshotV1:
    return contract.TaskStateSnapshotV1(
        workflow=contract.empty_task_state_snapshot().workflow,
        active_child_task=contract.ChildTaskStateView(
            task_id="search_frontend_child_12345678",
            kind=contract.CHILD_KIND_A2_QUESTION,
            unit_id="",
            task_revision=7,
            phase="WAIT_CANDIDATE_CHOICE",
            status=contract.STATUS_WAITING_USER,
            completed_steps=(
                contract.CHILD_STEP_QUESTION_ACCEPTED,
                contract.CHILD_STEP_QUESTION_ANALYZED,
                contract.CHILD_STEP_CHAPTER_RESOLVED,
                contract.CHILD_STEP_ROUTE_SELECTED,
                contract.CHILD_STEP_SEARCH_COMPLETED,
                contract.CHILD_STEP_CANDIDATES_READY,
            ),
            allowed_actions=(contract.ACTION_SELECT_CANDIDATE,),
            next_stage=contract.NEXT_SELECT_CANDIDATE,
            chapter="2静定结构",
            candidate_count=2,
            candidate_generation="7:1",
        ),
    )


def _a3_snapshot() -> contract.TaskStateSnapshotV1:
    unit = contract.UnitStateView(
        unit_id="g1-u1",
        page_index=1,
        display_label="四-1",
        status=contract.UNIT_ACTIVE,
    )
    next_unit = contract.UnitStateView(
        unit_id="g1-u2",
        page_index=2,
        display_label="四-2",
        status=contract.UNIT_AVAILABLE,
    )
    return contract.TaskStateSnapshotV1(
        workflow=contract.WorkflowStateView(
            exists=True,
            workflow_id="search_frontend_workflow_12345678",
            kind=contract.WORKFLOW_KIND_IMAGE_SEARCH,
            route="A3",
            task_revision=9,
            phase="A2_ACTIVE",
            status=contract.STATUS_RUNNING,
            completed_steps=(
                contract.WORKFLOW_STEP_IMAGE_ACCEPTED,
                contract.WORKFLOW_STEP_ROUTE_DECIDED,
                contract.WORKFLOW_STEP_PAGE_UNDERSTOOD,
                contract.WORKFLOW_STEP_UNIT_CATALOG_READY,
                contract.WORKFLOW_STEP_UNIT_SELECTED,
                contract.WORKFLOW_STEP_CHILD_STARTED,
            ),
            allowed_actions=(contract.ACTION_CANCEL_CURRENT_UNIT,),
            next_stage=contract.NEXT_FOLLOW_CHILD,
        ),
        active_child_task=contract.ChildTaskStateView(
            task_id="search_frontend_child_12345678",
            kind=contract.CHILD_KIND_A2_QUESTION,
            unit_id=unit.unit_id,
            task_revision=7,
            phase="WAIT_CANDIDATE_CHOICE",
            status=contract.STATUS_WAITING_USER,
            completed_steps=(
                contract.CHILD_STEP_QUESTION_ACCEPTED,
                contract.CHILD_STEP_QUESTION_ANALYZED,
                contract.CHILD_STEP_CHAPTER_RESOLVED,
                contract.CHILD_STEP_ROUTE_SELECTED,
                contract.CHILD_STEP_SEARCH_COMPLETED,
                contract.CHILD_STEP_CANDIDATES_READY,
            ),
            allowed_actions=(contract.ACTION_SELECT_CANDIDATE,),
            next_stage=contract.NEXT_SELECT_CANDIDATE,
            chapter="2静定结构",
            candidate_count=2,
            candidate_generation="7:1",
        ),
        current_unit=unit,
        units=(unit, next_unit),
    )


def _inconsistent_snapshot() -> contract.TaskStateSnapshotV1:
    return contract.TaskStateSnapshotV1(
        workflow=contract.empty_task_state_snapshot().workflow,
        active_child_task=contract.ChildTaskStateView(
            task_id="",
            kind=contract.CHILD_KIND_A2_QUESTION,
            unit_id="",
            task_revision=0,
            phase=contract.PHASE_UNKNOWN,
            status=contract.STATUS_INCONSISTENT,
            allowed_actions=(),
            next_stage=contract.NEXT_RETRY,
        ),
        consistency=contract.ConsistencyView(
            status=contract.CONSISTENCY_INCONSISTENT,
            codes=(contract.CONSISTENCY_CHILD_STATE_UNREADABLE,),
        ),
    )


def _inconsistent_workflow_snapshot() -> contract.TaskStateSnapshotV1:
    return contract.TaskStateSnapshotV1(
        workflow=contract.WorkflowStateView(
            exists=True,
            workflow_id="",
            kind=contract.WORKFLOW_KIND_IMAGE_SEARCH,
            route="A3",
            task_revision=0,
            phase=contract.PHASE_UNKNOWN,
            status=contract.STATUS_INCONSISTENT,
            completed_steps=(),
            allowed_actions=(),
            next_stage=contract.NEXT_RETRY,
        ),
        consistency=contract.ConsistencyView(
            status=contract.CONSISTENCY_INCONSISTENT,
            codes=(contract.CONSISTENCY_WORKFLOW_STATE_UNREADABLE,),
        ),
    )


def _phase_matrix_snapshots() -> list[dict[str, object]]:
    snapshots: list[contract.TaskStateSnapshotV1] = []
    for phase_name, spec in contract.CHILD_PHASE_CONTRACTS.items():
        if phase_name in {"IDLE", contract.PHASE_UNKNOWN}:
            continue
        snapshots.append(
            contract.TaskStateSnapshotV1(
                workflow=contract.empty_task_state_snapshot().workflow,
                active_child_task=contract.ChildTaskStateView(
                    task_id=f"search_frontend_{phase_name.lower()}_12345678",
                    kind=contract.CHILD_KIND_A2_QUESTION,
                    unit_id="",
                    task_revision=1,
                    phase=phase_name,
                    status=spec.status,
                    completed_steps=(),
                    allowed_actions=spec.action_candidates,
                    next_stage=spec.next_stage,
                    chapter="",
                    candidate_count=0,
                    candidate_generation="",
                ),
            )
        )

    for route, phase_names in contract.WORKFLOW_PHASES_BY_ROUTE.items():
        if route == contract.WORKFLOW_ROUTE_NONE:
            continue
        for phase_name in sorted(phase_names):
            spec = contract.WORKFLOW_PHASE_CONTRACTS[phase_name]
            unit = None
            units: tuple[contract.UnitStateView, ...] = ()
            if route == "A3" and phase_name in contract.WORKFLOW_CURRENT_UNIT_PHASES:
                unit = contract.UnitStateView(
                    unit_id="g1-u1",
                    page_index=1,
                    display_label="一-1",
                    status=contract.UNIT_ACTIVE,
                )
                units = (unit,)
            child = None
            if phase_name == "A2_ACTIVE":
                child = contract.ChildTaskStateView(
                    task_id=f"search_frontend_{route.lower()}_child_12345678",
                    kind=contract.CHILD_KIND_A2_QUESTION,
                    unit_id=unit.unit_id if unit is not None else "",
                    task_revision=1,
                    phase="PROCESSING",
                    status=contract.STATUS_RUNNING,
                    completed_steps=(),
                    allowed_actions=(),
                    next_stage=contract.NEXT_SYSTEM_CONTINUE,
                    chapter="",
                    candidate_count=0,
                    candidate_generation="",
                )
            snapshots.append(
                contract.TaskStateSnapshotV1(
                    workflow=contract.WorkflowStateView(
                        exists=True,
                        workflow_id=f"search_frontend_{route.lower()}_{phase_name.lower()}_12345678",
                        kind=contract.WORKFLOW_KIND_IMAGE_SEARCH,
                        route=route,
                        task_revision=1,
                        phase=phase_name,
                        status=spec.status,
                        completed_steps=(),
                        allowed_actions=spec.action_candidates,
                        next_stage=spec.next_stage,
                    ),
                    active_child_task=child,
                    current_unit=unit,
                    units=units,
                )
            )
    return [snapshot.to_dict() for snapshot in snapshots]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend task-state tests")
class DemoWebTaskStateTests(unittest.TestCase):
    def test_parser_matches_v1_and_fails_closed(self):
        fixtures = {
            "empty": contract.empty_task_state_snapshot().to_dict(),
            "a2": _a2_snapshot().to_dict(),
            "a3": _a3_snapshot().to_dict(),
            "inconsistent": _inconsistent_snapshot().to_dict(),
            "inconsistent_workflow": _inconsistent_workflow_snapshot().to_dict(),
            "phase_matrix": _phase_matrix_snapshots(),
        }
        unicode_snapshot = _a2_snapshot()
        fixtures["unicode"] = replace(
            unicode_snapshot,
            active_child_task=replace(
                unicode_snapshot.active_child_task,
                chapter="𠀀" * 64,
            ),
        ).to_dict()
        fixtures["python_public_text"] = [
            replace(
                unicode_snapshot,
                active_child_task=replace(unicode_snapshot.active_child_task, chapter=chapter),
            ).to_dict()
            for chapter in ("中password中", "\ufeff/etc")
        ]
        self.assertTrue(contract.is_public_task_state_text("中password中", 64))
        self.assertTrue(contract.is_public_task_state_text("\ufeff/etc", 64))
        self.assertFalse(contract.is_public_task_state_text("\u0085/etc", 64))
        self.assertFalse(contract.is_public_task_state_text("K://x", 64))
        node_test = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const api = require('./tiku_agent/demo_web/task_state.js');
const fixtures = JSON.parse(fs.readFileSync(0, 'utf8'));
const clone = (value) => JSON.parse(JSON.stringify(value));

const browserContext = vm.createContext({ fixtureJson: JSON.stringify(fixtures.a3) });
vm.runInContext(
  fs.readFileSync('./tiku_agent/demo_web/task_state.js', 'utf8'),
  browserContext,
  { filename: 'task_state.js' },
);
assert.equal(typeof browserContext.TikuTaskStateV1.createTaskStateModel, 'function');
assert.equal(browserContext.TikuTaskStateV1.createTaskStateModel().reason, 'MISSING');
assert.equal(browserContext.TikuTaskStateV1.createTaskStateModel().actions_enabled, false);
const browserModel = vm.runInContext(
  'TikuTaskStateV1.createTaskStateModel(JSON.parse(fixtureJson))',
  browserContext,
);
assert.equal(browserModel.reason, 'OK');
assert.equal(
  browserContext.TikuTaskStateV1.allowsWorkflowAction(browserModel, 'cancel_current_unit'),
  true,
);

function assertDeepFrozen(value) {
  if (!value || typeof value !== 'object') return;
  assert.equal(Object.isFrozen(value), true);
  Object.values(value).forEach(assertDeepFrozen);
}

for (const fixture of [
  fixtures.empty,
  fixtures.a2,
  fixtures.a3,
  fixtures.inconsistent,
  fixtures.inconsistent_workflow,
  fixtures.unicode,
  ...fixtures.python_public_text,
  ...fixtures.phase_matrix,
]) {
  const parsed = api.parseTaskStateSnapshotV1(fixture);
  assert.deepEqual(parsed, fixture);
  assert.notStrictEqual(parsed, fixture);
  assertDeepFrozen(parsed);
}

const emptyModel = api.createTaskStateModel(fixtures.empty);
assert.equal(emptyModel.available, true);
assert.equal(emptyModel.consistent, true);
assert.equal(emptyModel.workflow_next_stage, 'UPLOAD_IMAGE');
assert.equal(api.allowsWorkflowAction(emptyModel, 'upload_image'), false);

const a2Model = api.createTaskStateModel(fixtures.a2);
assert.equal(api.allowsChildAction(a2Model, 'select_candidate'), true);
assert.equal(api.allowsChildAction(a2Model, 'resend_answer'), false);
assert.equal(api.allowsWorkflowAction(a2Model, 'select_candidate'), false);

const a3Model = api.createTaskStateModel(fixtures.a3);
assert.equal(api.allowsWorkflowAction(a3Model, 'cancel_current_unit'), true);
assert.equal(api.allowsChildAction(a3Model, 'select_candidate'), true);

const inconsistentModel = api.createTaskStateModel(fixtures.inconsistent);
assert.equal(inconsistentModel.available, true);
assert.equal(inconsistentModel.consistent, false);
assert.equal(inconsistentModel.actions_enabled, false);
assert.equal(inconsistentModel.reason, 'SERVER_INCONSISTENT');
assert.deepEqual(inconsistentModel.workflow_actions, []);
assert.deepEqual(inconsistentModel.child_actions, []);
assert.equal(inconsistentModel.child_next_stage, 'RETRY');
assert.equal(api.allowsChildAction(inconsistentModel, 'retry_search'), false);

const missingModel = api.createTaskStateModel();
assert.equal(missingModel.reason, 'MISSING');
assert.equal(missingModel.snapshot, null);
assert.equal(missingModel.actions_enabled, false);
assertDeepFrozen(missingModel);

const envelopeModel = api.createTaskStateModelFromEnvelope({ task_state: fixtures.a2 });
assert.equal(envelopeModel.reason, 'OK');
assert.equal(api.allowsChildAction(envelopeModel, 'select_candidate'), true);
assert.equal(api.createTaskStateModelFromEnvelope({}).reason, 'MISSING');
assert.equal(api.createTaskStateModelFromEnvelope().reason, 'MISSING');
assert.equal(api.createTaskStateModelFromEnvelope(null).reason, 'INVALID');
assert.equal(
  api.createTaskStateModelFromEnvelope({ task_state: { ...fixtures.a2, schema_version: 2 } }).reason,
  'UNSUPPORTED_SCHEMA',
);
let envelopeGetterReads = 0;
const getterEnvelope = {};
Object.defineProperty(getterEnvelope, 'task_state', {
  enumerable: true,
  get() {
    envelopeGetterReads += 1;
    return fixtures.a2;
  },
});
assert.equal(api.createTaskStateModelFromEnvelope(getterEnvelope).reason, 'INVALID');
assert.equal(envelopeGetterReads, 0);
const hiddenEnvelope = {};
Object.defineProperty(hiddenEnvelope, 'task_state', { value: fixtures.a2, enumerable: false });
assert.equal(api.createTaskStateModelFromEnvelope(hiddenEnvelope).reason, 'INVALID');

const consumer = api.createTaskStateConsumer();
assert.equal(Object.isFrozen(consumer), true);
assert.equal(consumer.current().reason, 'MISSING');
assert.equal(consumer.consume(null, { task_state: fixtures.a2 }).reason, 'MISSING');
const firstRequest = consumer.begin();
assert.equal(consumer.current().reason, 'MISSING');
assert.equal(consumer.consume(firstRequest, { task_state: fixtures.a2 }).reason, 'OK');
assert.equal(api.allowsChildAction(consumer.current(), 'select_candidate'), true);
assert.equal(consumer.consume(firstRequest, { task_state: fixtures.a3 }).snapshot.active_child_task.task_revision, 7);
const secondRequest = consumer.begin();
assert.equal(consumer.current().reason, 'MISSING');
let staleEnvelopeReads = 0;
const staleEnvelope = {};
Object.defineProperty(staleEnvelope, 'task_state', {
  enumerable: true,
  get() {
    staleEnvelopeReads += 1;
    return fixtures.a2;
  },
});
assert.strictEqual(consumer.consume(firstRequest, staleEnvelope), consumer.current());
assert.equal(consumer.current().reason, 'MISSING');
assert.equal(staleEnvelopeReads, 0);
assert.equal(consumer.consume(secondRequest, { task_state: fixtures.a3 }).reason, 'OK');
assert.equal(api.allowsWorkflowAction(consumer.current(), 'cancel_current_unit'), true);
const thirdRequest = consumer.begin();
assert.equal(consumer.consume(thirdRequest, {}).reason, 'MISSING');
const fourthRequest = consumer.begin();
assert.equal(consumer.consume(fourthRequest, { task_state: null }).reason, 'INVALID');
const noUpdateRequest = consumer.begin();
const noUpdateModel = consumer.current();
assert.strictEqual(consumer.finish(noUpdateRequest), noUpdateModel);
let retiredEnvelopeReads = 0;
const retiredEnvelope = {};
Object.defineProperty(retiredEnvelope, 'task_state', {
  enumerable: true,
  get() {
    retiredEnvelopeReads += 1;
    return fixtures.a2;
  },
});
assert.strictEqual(consumer.consume(noUpdateRequest, retiredEnvelope), noUpdateModel);
assert.equal(retiredEnvelopeReads, 0);
const finalRequest = consumer.begin();
consumer.finish(noUpdateRequest);
assert.equal(consumer.consume(finalRequest, { task_state: fixtures.a2 }).reason, 'OK');

function assertInvalid(mutator, reason = 'INVALID') {
  const raw = clone(fixtures.a3);
  mutator(raw);
  assert.throws(
    () => api.parseTaskStateSnapshotV1(raw),
    (error) => error instanceof api.TaskStateValidationError,
  );
  const model = api.createTaskStateModel(raw);
  assert.equal(model.available, false);
  assert.equal(model.reason, reason);
  assert.equal(model.snapshot, null);
  assert.equal(model.actions_enabled, false);
  assert.deepEqual(model.workflow_actions, []);
  assert.deepEqual(model.child_actions, []);
}

assertInvalid((raw) => { delete raw.workflow; });
assertInvalid((raw) => { raw.extra = true; });
assertInvalid((raw) => { raw.workflow.extra = true; });
assertInvalid((raw) => { raw.active_child_task.extra = true; });
assertInvalid((raw) => { raw.current_unit.extra = true; });
assertInvalid((raw) => { raw.units[0].extra = true; });
assertInvalid((raw) => { raw.consistency.extra = true; });
assertInvalid((raw) => {
  raw.workflow.status = 'COMPLETED';
  raw.workflow.allowed_actions = ['upload_image'];
  raw.workflow.next_stage = 'DONE';
  let reads = 0;
  Object.defineProperty(raw.workflow, 'phase', {
    enumerable: true,
    configurable: true,
    get() {
      reads += 1;
      return reads === 3 ? 'COMPLETE' : 'A2_ACTIVE';
    },
  });
});
assertInvalid((raw) => {
  Object.defineProperty(raw.workflow, 'phase', {
    value: raw.workflow.phase,
    enumerable: false,
  });
});
assertInvalid((raw) => { raw.schema_version = 2; }, 'UNSUPPORTED_SCHEMA');
assertInvalid((raw) => { raw.workflow.allowed_actions = ['not_registered']; });
assertInvalid((raw) => { raw.active_child_task.allowed_actions = ['resend_answer']; });
assertInvalid((raw) => { raw.units[Symbol('extra')] = true; });
assertInvalid((raw) => { Object.defineProperty(raw.units, 'hidden', { value: true }); });
assertInvalid((raw) => {
  Object.defineProperty(raw.units, 0, {
    value: raw.units[0],
    enumerable: false,
  });
});
assertInvalid((raw) => {
  Object.defineProperty(raw.units, 0, {
    enumerable: true,
    get() { return fixtures.a3.units[0]; },
  });
});
assertInvalid((raw) => { delete raw.units[0]; });
assertInvalid((raw) => { Object.setPrototypeOf(raw.units, null); });
for (const inheritedName of ['__proto__', 'constructor', 'toString']) {
  assertInvalid((raw) => { raw.workflow.phase = inheritedName; });
  assertInvalid((raw) => { raw.active_child_task.phase = inheritedName; });
}
assertInvalid((raw) => { raw.workflow.status = 'WAITING_USER'; });
assertInvalid((raw) => { raw.workflow.next_stage = 'SELECT_UNIT'; });
assertInvalid((raw) => { raw.active_child_task.task_revision = 7.5; });
assertInvalid((raw) => { raw.active_child_task.task_revision = 0; });
assertInvalid((raw) => { raw.active_child_task.candidate_generation = '6:1'; });
assertInvalid((raw) => { raw.active_child_task.candidate_count = 0; });
assertInvalid((raw) => {
  raw.active_child_task.candidate_count = 1;
  raw.active_child_task.candidate_generation = '';
});
assertInvalid((raw) => { raw.active_child_task.chapter = '𠀀'.repeat(65); });
assertInvalid((raw) => { raw.active_child_task.unit_id = 'g1-u2'; });
assertInvalid((raw) => { raw.current_unit.display_label = 'changed'; });
assertInvalid((raw) => { raw.units.push({ ...raw.units[0] }); });
assertInvalid((raw) => { raw.units.reverse(); });
assertInvalid((raw) => { raw.units[1].page_index = 1; });
assertInvalid((raw) => {
  raw.units[1].status = 'ACTIVE';
});
assertInvalid((raw) => { raw.active_child_task.completed_steps.push('ANSWER_PREPARED'); });
assertInvalid((raw) => { raw.active_child_task = null; });
assertInvalid((raw) => {
  raw.consistency = { status: 'INCONSISTENT', codes: ['CHILD_STATE_UNREADABLE'] };
});
assertInvalid((raw) => { raw.consistency.codes = ['CHILD_STATE_UNREADABLE']; });
assertInvalid((raw) => { raw.consistency = { status: 'INCONSISTENT', codes: [] }; });

const badInconsistent = clone(fixtures.inconsistent);
badInconsistent.active_child_task.allowed_actions = ['cancel'];
const badModel = api.createTaskStateModel(badInconsistent);
assert.equal(badModel.available, false);
assert.equal(badModel.actions_enabled, false);

const forgedWorkflowModel = { actions_enabled: true, workflow_actions: ['finish_page'] };
const forgedChildModel = { actions_enabled: true, child_actions: ['select_candidate'] };
assert.equal(api.allowsWorkflowAction(forgedWorkflowModel, 'finish_page'), false);
assert.equal(api.allowsChildAction(forgedChildModel, 'select_candidate'), false);
assert.equal(api.allowsWorkflowAction({ actions_enabled: true }, 'finish_page'), false);
assert.equal(api.allowsChildAction({ actions_enabled: true }, 'select_candidate'), false);
assert.equal(api.allowsWorkflowAction(Object.create(a3Model), 'cancel_current_unit'), false);
assert.equal(api.allowsChildAction(Object.create(a3Model), 'select_candidate'), false);

const mutable = clone(fixtures.a2);
const detached = api.parseTaskStateSnapshotV1(mutable);
mutable.active_child_task.allowed_actions.length = 0;
assert.deepEqual(detached.active_child_task.allowed_actions, ['select_candidate']);
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", node_test],
            cwd=ROOT,
            input=json.dumps(fixtures, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_browser_loads_model_before_demo_and_initializes_closed_context(self):
        page = (ROOT / "tiku_agent" / "demo_web" / "index.html").read_text(encoding="utf-8")
        demo = (ROOT / "tiku_agent" / "demo_web" / "demo.js").read_text(encoding="utf-8")

        task_state_asset = 'src="/assets/task_state.js?v=20260830-task-state-3-4-2"'
        demo_asset = 'src="/assets/demo.js?v=20260830-task-state-3-4-2"'
        self.assertIn(task_state_asset, page)
        self.assertIn(demo_asset, page)
        self.assertLess(page.index(task_state_asset), page.index(demo_asset))
        self.assertIn("const taskStateV1 = globalThis.TikuTaskStateV1", demo)
        self.assertIn("const taskStateConsumer = taskStateV1.createTaskStateConsumer()", demo)
        self.assertIn("let taskStateContext = taskStateConsumer.current()", demo)

    def test_demo_consumes_only_authoritative_task_state_envelopes(self):
        demo = (ROOT / "tiku_agent" / "demo_web" / "demo.js").read_text(encoding="utf-8")

        for path in (
            "/api/session", "/api/message", "/api/image", "/api/a3/select", "/api/reset",
            "/api/message/stream", "/api/image/stream", "/api/a3/select/stream",
            "/api/a3/prepare/stream", "/api/a3/crop/stream",
        ):
            self.assertIn(f"'{path}'", demo)
        json_paths = demo.split("const TASK_STATE_JSON_PATHS", 1)[1].split("]);", 1)[0]
        stream_paths = demo.split("const TASK_STATE_STREAM_PATHS", 1)[1].split("]);", 1)[0]
        for non_task_path in ("/api/feedback", "/health"):
            self.assertNotIn(non_task_path, json_paths)
            self.assertNotIn(non_task_path, stream_paths)

        request_block = demo.split("async function request(", 1)[1].split(
            "async function requestStream(", 1
        )[0]
        self.assertLess(
            request_block.index("beginTaskStateRequest(url, 'json')"),
            request_block.index("await fetch(url"),
        )
        error_consume = "consumeTaskStateResponse(taskStateRequest, data, { error: true })"
        self.assertLess(request_block.index(error_consume), request_block.index("throw safeHttpError"))
        success_consume = "consumeTaskStateResponse(taskStateRequest, data);"
        self.assertLess(request_block.index(success_consume), request_block.index("return data;"))

        stream_block = demo.split("async function requestStream(", 1)[1].split(
            "function responseItem(", 1
        )[0]
        self.assertLess(
            stream_block.index("beginTaskStateRequest(url, 'stream')"),
            stream_block.index("await fetch(url"),
        )
        progress_branch = stream_block.split("if (event.type === 'progress')", 1)[1].split(
            "if (event.type === 'result')", 1
        )[0]
        self.assertNotIn("consumeTaskStateResponse", progress_branch)
        result_branch = stream_block.split("if (event.type === 'result')", 1)[1].split(
            "if (event.type === 'error')", 1
        )[0]
        self.assertLess(
            result_branch.index("consumeTaskStateResponse(taskStateRequest, terminalResult)"),
            result_branch.index("await reader.cancel()"),
        )
        error_branch = stream_block.split("if (event.type === 'error')", 1)[1].split(
            "if (done) break;", 1
        )[0]
        self.assertIn("consumeTaskStateResponse(taskStateRequest, event, { error: true })", error_branch)
        self.assertIn("TASK_STATE_QUEUE_CODES", demo)
        self.assertIn("envelope?.layer === 'queue'", demo)
        self.assertEqual(demo.count("finishTaskStateRequest(taskStateRequest);"), 2)

    def test_response_wiring_preserves_no_update_and_latest_request(self):
        fixtures = {
            "empty": contract.empty_task_state_snapshot().to_dict(),
            "a2": _a2_snapshot().to_dict(),
            "a3": _a3_snapshot().to_dict(),
        }
        node_test = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const taskStateV1 = require('./tiku_agent/demo_web/task_state.js');
const fixtures = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync('./tiku_agent/demo_web/demo.js', 'utf8');

const constants = source.slice(
  source.indexOf('const TASK_STATE_JSON_PATHS'),
  source.indexOf('const a3PrepareSelection'),
);
const initialization = source.slice(
  source.indexOf('const taskStateConsumer ='),
  source.indexOf('let a3SourceUrl ='),
);
const helpers = source.slice(
  source.indexOf('function taskStateApiPath'),
  source.indexOf('function protocolFields'),
);
const createHarness = new Function('taskStateV1', `
  ${constants}
  ${initialization}
  ${helpers}
  return Object.freeze({
    begin: beginTaskStateRequest,
    consume: consumeTaskStateResponse,
    finish: finishTaskStateRequest,
    current: () => taskStateContext,
  });
`);
const wiring = createHarness(taskStateV1);

for (const path of ['/api/session', '/api/message', '/api/image', '/api/a3/select', '/api/reset']) {
  const request = wiring.begin(`${path}?request=1`, 'json');
  assert.equal(typeof request, 'symbol');
  assert.equal(wiring.current().reason, 'MISSING');
  wiring.finish(request);
}
for (const path of [
  '/api/message/stream', '/api/image/stream', '/api/a3/select/stream',
  '/api/a3/prepare/stream', '/api/a3/crop/stream',
]) {
  const request = wiring.begin(path, 'stream');
  assert.equal(typeof request, 'symbol');
  assert.equal(wiring.current().reason, 'MISSING');
  wiring.finish(request);
}

let request = wiring.begin('/api/session', 'json');
wiring.consume(request, { task_state: fixtures.a2 });
assert.equal(wiring.current().reason, 'OK');
assert.equal(wiring.current().snapshot.active_child_task.task_revision, 7);
const sessionModel = wiring.current();
for (const path of ['/api/feedback', '/api/feedback/resp_1', '/health', '/api/media/image.jpg']) {
  const nonTask = wiring.begin(path, 'json');
  assert.equal(nonTask, null);
  wiring.consume(nonTask, { task_state: fixtures.a3 });
  wiring.finish(nonTask);
  assert.strictEqual(wiring.current(), sessionModel);
}

for (const code of ['QUEUE_FULL', 'QUEUE_TIMEOUT']) {
  request = wiring.begin('/api/message/stream', 'stream');
  const closed = wiring.current();
  wiring.consume(request, {
    type: 'error', layer: 'queue', code, task_state: fixtures.a3,
  }, { error: true });
  assert.strictEqual(wiring.current(), closed);
  wiring.finish(request);
  wiring.consume(request, { task_state: fixtures.a3 });
  assert.strictEqual(wiring.current(), closed);
}

const slowSession = wiring.begin('/api/session', 'json');
const latestStream = wiring.begin('/api/image/stream', 'stream');
wiring.consume(latestStream, { task_state: fixtures.a3 });
const latestModel = wiring.current();
wiring.consume(slowSession, { task_state: fixtures.a2 });
assert.strictEqual(wiring.current(), latestModel);
assert.equal(wiring.current().snapshot.workflow.task_revision, 9);

request = wiring.begin('/api/message/stream', 'stream');
wiring.consume(request, { data: { task_state: fixtures.a2 } }, { error: true });
assert.equal(wiring.current().reason, 'MISSING');
request = wiring.begin('/api/reset', 'json');
wiring.consume(request, { task_state: fixtures.empty });
assert.equal(wiring.current().reason, 'OK');
assert.equal(wiring.current().snapshot.workflow.exists, false);

const retryConnectionSource = source.slice(
  source.indexOf('async function retryConnection()'),
  source.indexOf("form.addEventListener('submit'"),
);
const createRetryHarness = new Function(`
  let isBusy = false;
  let becomeBusyDuringHealth = false;
  let healthChecks = 0;
  let sessionRepairs = 0;
  async function checkHealth() {
    healthChecks += 1;
    if (becomeBusyDuringHealth) isBusy = true;
    return true;
  }
  async function repairUploadedImageHistory() { sessionRepairs += 1; }
  ${retryConnectionSource}
  return Object.freeze({
    retryConnection,
    setBusy: (value) => { isBusy = value; },
    setBecomeBusyDuringHealth: (value) => { becomeBusyDuringHealth = value; },
    counts: () => ({ healthChecks, sessionRepairs }),
  });
`);
const retryHarness = createRetryHarness();
(async () => {
  retryHarness.setBusy(true);
  await retryHarness.retryConnection();
  assert.deepEqual(retryHarness.counts(), { healthChecks: 0, sessionRepairs: 0 });
  retryHarness.setBusy(false);
  retryHarness.setBecomeBusyDuringHealth(true);
  await retryHarness.retryConnection();
  assert.deepEqual(retryHarness.counts(), { healthChecks: 1, sessionRepairs: 0 });
  retryHarness.setBusy(false);
  retryHarness.setBecomeBusyDuringHealth(false);
  await retryHarness.retryConnection();
  assert.deepEqual(retryHarness.counts(), { healthChecks: 2, sessionRepairs: 1 });
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", node_test],
            cwd=ROOT,
            input=json.dumps(fixtures, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_task_state_module_has_valid_syntax(self):
        result = subprocess.run(
            [shutil.which("node"), "--check", str(TASK_STATE_SCRIPT)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
