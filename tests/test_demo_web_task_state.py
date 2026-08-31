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


def _a3_action_snapshot(
    *,
    workflow_id: str = "search_frontend_actions_workflow_12345678",
    task_revision: int = 9,
    phase: str = "WAIT_UNIT_SELECTION",
    allowed_actions: tuple[str, ...] = (
        contract.ACTION_SELECT_UNIT,
        contract.ACTION_PREPARE_UNITS,
    ),
    unit_statuses: tuple[str, ...] = (
        contract.UNIT_AVAILABLE,
        contract.UNIT_PREPARED,
        contract.UNIT_COMPLETED,
        contract.UNIT_CLOSED,
    ),
) -> contract.TaskStateSnapshotV1:
    units = tuple(
        contract.UnitStateView(
            unit_id=f"g1-u{index}",
            page_index=index,
            display_label=f"Q{index}",
            status=status,
        )
        for index, status in enumerate(unit_statuses, start=1)
    )
    current_unit = next((unit for unit in units if unit.status == contract.UNIT_ACTIVE), None)
    child = None
    if phase == "A2_ACTIVE":
        if current_unit is None:
            raise ValueError("A2_ACTIVE fixture requires an ACTIVE unit")
        child = contract.ChildTaskStateView(
            task_id="search_frontend_actions_child_12345678",
            kind=contract.CHILD_KIND_A2_QUESTION,
            unit_id=current_unit.unit_id,
            task_revision=3,
            phase="PROCESSING",
            status=contract.STATUS_RUNNING,
            completed_steps=(),
            allowed_actions=(),
            next_stage=contract.NEXT_SYSTEM_CONTINUE,
            chapter="",
            candidate_count=0,
            candidate_generation="",
        )
    phase_contract = contract.WORKFLOW_PHASE_CONTRACTS[phase]
    return contract.TaskStateSnapshotV1(
        workflow=contract.WorkflowStateView(
            exists=True,
            workflow_id=workflow_id,
            kind=contract.WORKFLOW_KIND_IMAGE_SEARCH,
            route="A3",
            task_revision=task_revision,
            phase=phase,
            status=phase_contract.status,
            completed_steps=(),
            allowed_actions=allowed_actions,
            next_stage=phase_contract.next_stage,
        ),
        active_child_task=child,
        current_unit=current_unit,
        units=units,
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
const fixtures = JSON.parse(process.argv[2]);
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
            [shutil.which("node"), "-", json.dumps(fixtures, ensure_ascii=False)],
            cwd=ROOT,
            input=node_test,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_browser_loads_model_before_demo_and_initializes_closed_context(self):
        page = (ROOT / "tiku_agent" / "demo_web" / "index.html").read_text(encoding="utf-8")
        demo = (ROOT / "tiku_agent" / "demo_web" / "demo.js").read_text(encoding="utf-8")

        task_state_asset = 'src="/assets/task_state.js?v=20260830-task-state-3-4-5"'
        demo_asset = 'src="/assets/demo.js?v=20260831-session-recovery-v1"'
        self.assertIn(task_state_asset, page)
        self.assertIn(demo_asset, page)
        self.assertLess(page.index(task_state_asset), page.index(demo_asset))
        self.assertIn("const taskStateV1 = globalThis.TikuTaskStateV1", demo)
        self.assertIn("const taskStateConsumer = taskStateV1.createTaskStateConsumer()", demo)
        self.assertIn("let taskStateContext = taskStateConsumer.current()", demo)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for bootstrap validation")
    def test_demo_bootstraps_missing_task_state_asset_before_start(self):
        demo = (ROOT / "tiku_agent" / "demo_web" / "demo.js").read_text(encoding="utf-8")
        bootstrap = demo.split("function startDemo(taskStateV1) {", 1)[0]
        node_test = r"""
const assert = require('node:assert/strict');
const bootstrap = globalThis.__bootstrapSource;

function harness(initialModel = null) {
  const scripts = [];
  const starts = [];
  const globalObject = { TikuTaskStateV1: initialModel };
  const document = {
    querySelector: () => null,
    createElement: () => {
      const listeners = {};
      return {
        src: '',
        setAttribute(name, value) { this[name] = value; },
        addEventListener(name, callback) { listeners[name] = callback; },
        dispatch(name) { listeners[name]?.(); },
      };
    },
    head: { appendChild(script) { scripts.push(script); } },
  };
  const execute = new Function('globalThis', 'document', 'startDemo', bootstrap);
  execute(globalObject, document, (model) => starts.push(model));
  return { globalObject, scripts, starts };
}

const readyModel = { createTaskStateConsumer() {} };
const ready = harness(readyModel);
assert.deepEqual(ready.starts, [readyModel]);
assert.equal(ready.scripts.length, 0);

const missing = harness();
assert.equal(missing.starts.length, 0);
assert.equal(missing.scripts.length, 1);
assert.equal(
  missing.scripts[0].src,
  '/assets/task_state.js?v=20260830-task-state-3-4-5',
);
missing.globalObject.TikuTaskStateV1 = readyModel;
missing.scripts[0].dispatch('load');
missing.scripts[0].dispatch('load');
assert.deepEqual(missing.starts, [readyModel]);
"""
        result = subprocess.run(
            [shutil.which("node"), "-"],
            cwd=ROOT,
            input=(
                f"globalThis.__bootstrapSource = {json.dumps(bootstrap)};\n"
                f"{node_test}"
            ),
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

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

        reset_block = demo.split("async function resetConversation()", 1)[1].split(
            "async function checkHealth()", 1
        )[0]
        self.assertIn("const data = await request('/api/reset'", reset_block)
        self.assertLess(
            reset_block.index("applyResetSessionContext(data)"),
            reset_block.index("clearHistory();"),
        )

    def test_explicit_reset_text_uses_the_authoritative_reset_endpoint(self):
        demo = (ROOT / "tiku_agent" / "demo_web" / "demo.js").read_text(
            encoding="utf-8"
        )
        reset_parser = demo.split("function isExplicitSessionResetText", 1)[1].split(
            "function sessionRequestLockAvailable", 1
        )[0]
        node_test = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('./tiku_agent/demo_web/demo.js', 'utf8');
const start = source.indexOf('function isExplicitSessionResetText');
const end = source.indexOf('function sessionRequestLockAvailable', start);
const parseReset = new Function(`${source.slice(start, end)}; return isExplicitSessionResetText;`)();
for (const value of [
  '开始新对话', '清空当前会话。', '删除全部聊天记录', '全部清空',
  '开始\u0085新对话', '清空\u001c当前会话',
]) {
  assert.equal(parseReset(value), true, value);
}
for (const value of ['结束这张图', '清空候选', '开始新题', '']) {
  assert.equal(parseReset(value), false, value);
}
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", node_test],
            cwd=ROOT,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("publishSessionReset(sessionRequestFence)", demo)
        self.assertIn("浏览器无法安全确认当前会话", demo)
        request_block = demo.split("async function request(", 1)[1].split(
            "async function requestStream(", 1
        )[0]
        self.assertLess(
            request_block.index("const response = await fetch"),
            request_block.index("publishAuthoritativeReset(url, data"),
        )
        send_block = demo.split("async function sendTextValue", 1)[1].split(
            "async function sendText()", 1
        )[0]
        self.assertLess(
            send_block.index("isExplicitSessionResetText(clean)"),
            send_block.index("sessionTaskStartAllowed()"),
        )
        self.assertIn("await resetConversation();", send_block)
        self.assertIn("/^(?:(?:开始|创建|开个|开启)?新对话", reset_parser)

    def test_a2_buttons_use_branded_child_actions_and_recheck_on_click(self):
        demo = (ROOT / "tiku_agent" / "demo_web" / "demo.js").read_text(encoding="utf-8")

        candidate_block = demo.split("function createMediaCard(", 1)[1].split(
            "function addMessage(", 1
        )[0]
        self.assertIn("taskStateAllowsChildAction('select_candidate', actionTarget)", candidate_block)
        self.assertIn("bindChildActionButton(choose, 'select_candidate', actionTarget)", candidate_block)
        self.assertNotIn("sessionContext.phase", candidate_block)
        self.assertNotIn("workflow_next_stage", candidate_block)
        click_guard = "if (!taskStateAllowsChildAction('select_candidate', actionTarget)) return;"
        self.assertLess(candidate_block.index(click_guard), candidate_block.index("sendTextValue("))

        recovery_block = demo.split("function createRecoveryActions(", 1)[1].split(
            "function normalizeAuthorContact(", 1
        )[0]
        self.assertIn("recoveryChildActionBinding(action, retryAction, item)", recovery_block)
        self.assertIn("const childAction = childBinding?.action || ''", recovery_block)
        self.assertIn("taskStateAllowsChildAction(childAction, childActionTarget)", recovery_block)
        self.assertIn("taskStateAllowsChildAction(action, childActionTarget)", recovery_block)
        self.assertNotIn("child_next_stage", recovery_block)

        send_block = demo.split("async function sendTextValue(", 1)[1].split(
            "async function sendText()", 1
        )[0]
        self.assertIn("['select_candidate', 'retry_search'].includes(actionContext?.type)", send_block)
        self.assertIn("taskStateAllowsChildAction(childAction, childActionTarget)", send_block)
        self.assertIn("syncA2ActionButtons();", demo)

    def test_response_wiring_preserves_no_update_and_latest_request(self):
        fixtures = {
            "empty": contract.empty_task_state_snapshot().to_dict(),
            "a2": _a2_snapshot().to_dict(),
            "a3": _a3_snapshot().to_dict(),
            "inconsistent": _inconsistent_snapshot().to_dict(),
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
const normalizeA3Source = source.slice(
  source.indexOf('function normalizeA3Snapshot'),
  source.indexOf('function openLightbox'),
);
const actionButtons = [];
const fakeDocument = {
  querySelectorAll: () => actionButtons,
};
const createHarness = new Function('taskStateV1', 'document', `
  ${constants}
  ${initialization}
  ${helpers}
  ${normalizeA3Source}
  return Object.freeze({
    begin: beginTaskStateRequest,
    consume: consumeTaskStateResponse,
    finish: finishTaskStateRequest,
    current: () => taskStateContext,
    allowsChild: taskStateAllowsChildAction,
    childTarget: currentChildActionTarget,
    candidateBinding: candidateChildActionBinding,
    recoveryBinding: recoveryChildActionBinding,
    syncA2: syncA2ActionButtons,
    bindingFor: (envelope) => taskStateEnvelopeBindings.get(envelope) || null,
  });
`);
const wiring = createHarness(taskStateV1, fakeDocument);

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
const candidateTarget = {
  childTaskId: 'search_frontend_child_12345678',
  childTaskRevision: 7,
  childCandidateGeneration: '7:1',
  candidateRank: 1,
};
assert.equal(wiring.allowsChild('select_candidate', candidateTarget), true);
assert.equal(wiring.allowsChild('select_candidate', { ...candidateTarget, childTaskId: 'search_stale_child_12345678' }), false);
assert.equal(wiring.allowsChild('select_candidate', { ...candidateTarget, childTaskRevision: 8 }), false);
assert.equal(wiring.allowsChild('select_candidate', { ...candidateTarget, childCandidateGeneration: '7:2' }), false);
assert.equal(wiring.allowsChild('select_candidate', { ...candidateTarget, candidateRank: 3 }), false);
assert.equal(wiring.allowsChild('retry_search', candidateTarget), false);
assert.equal(wiring.allowsChild('select_candidate', null), false);

const contradictoryItem = {
  taskRevision: 99,
  candidateGeneration: '99:9',
  childTaskId: candidateTarget.childTaskId,
  childTaskRevision: candidateTarget.childTaskRevision,
  childCandidateGeneration: candidateTarget.childCandidateGeneration,
};
const candidateBinding = wiring.candidateBinding(contradictoryItem, 0);
assert.deepEqual(candidateBinding.actionContext, {
  type: 'select_candidate', task_id: candidateTarget.childTaskId,
  rank: 1, task_revision: 7, candidate_generation: '7:1',
});
assert.deepEqual(candidateBinding.actionTarget, candidateTarget);
const retryBinding = wiring.recoveryBinding('retry_search', null, contradictoryItem);
assert.deepEqual(retryBinding, {
  action: 'retry_search',
  target: { childTaskId: candidateTarget.childTaskId, childTaskRevision: 7 },
  actionContext: {
    type: 'retry_search', task_id: candidateTarget.childTaskId, task_revision: 7,
  },
});
const candidateRetryBinding = wiring.recoveryBinding('retry_request', {
  actionContext: {
    type: 'select_candidate', task_id: candidateTarget.childTaskId,
    rank: 2, task_revision: 7, candidate_generation: '7:1',
  },
}, contradictoryItem);
assert.deepEqual(candidateRetryBinding, {
  action: 'select_candidate',
  target: { ...candidateTarget, candidateRank: 2 },
  actionContext: {
    type: 'select_candidate', task_id: candidateTarget.childTaskId,
    rank: 2, task_revision: 7, candidate_generation: '7:1',
  },
});

const candidateButton = {
  dataset: {
    childAction: 'select_candidate', childTaskId: candidateTarget.childTaskId,
    childTaskRevision: '7', childCandidateGeneration: '7:1', candidateRank: '1',
    mediaAvailable: 'true',
  },
  classList: { contains: (name) => name === 'select-candidate' },
  disabled: true, hidden: false, textContent: '',
};
const retryButton = {
  dataset: {
    childAction: 'retry_search', childTaskId: candidateTarget.childTaskId,
    childTaskRevision: '7',
  },
  classList: { contains: (name) => name === 'message-recovery' },
  disabled: false, hidden: false, textContent: '重试搜索',
};
const legacyButton = {
  dataset: {
    childAction: 'select_candidate', childTaskId: '', childTaskRevision: '0',
    childCandidateGeneration: '', candidateRank: '1', mediaAvailable: 'true',
  },
  classList: { contains: (name) => name === 'select-candidate' },
  disabled: false, hidden: false, textContent: '选择',
};
actionButtons.push(candidateButton, retryButton, legacyButton);
wiring.syncA2();
assert.equal(candidateButton.disabled, false);
assert.equal(candidateButton.textContent, '选择');
assert.equal(retryButton.disabled, true);
assert.equal(retryButton.hidden, true);
assert.equal(legacyButton.disabled, true);
candidateButton.dataset.mediaAvailable = 'false';
wiring.syncA2();
assert.equal(candidateButton.disabled, true);
candidateButton.dataset.mediaAvailable = 'true';
wiring.syncA2();
assert.equal(candidateButton.disabled, false);
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
  assert.equal(wiring.allowsChild('select_candidate', candidateTarget), false);
  wiring.consume(request, {
    type: 'error', layer: 'queue', code, task_state: fixtures.a3,
  }, { error: true });
  assert.strictEqual(wiring.current(), closed);
  wiring.finish(request);
  wiring.consume(request, { task_state: fixtures.a3 });
  assert.strictEqual(wiring.current(), closed);
}

request = wiring.begin('/api/session', 'json');
const nextStageOnly = structuredClone(fixtures.a2);
nextStageOnly.active_child_task.allowed_actions = [];
wiring.consume(request, { task_state: nextStageOnly });
assert.equal(wiring.current().child_next_stage, 'SELECT_CANDIDATE');
assert.equal(wiring.allowsChild('select_candidate', candidateTarget), false);

for (const [phase, status, nextStage] of [
  ['ANSWERED', 'COMPLETED', 'DONE'],
  ['ERROR', 'FAILED', 'RETRY'],
]) {
  request = wiring.begin('/api/session', 'json');
  const actionDriven = structuredClone(fixtures.a2);
  actionDriven.active_child_task.phase = phase;
  actionDriven.active_child_task.status = status;
  actionDriven.active_child_task.next_stage = nextStage;
  actionDriven.active_child_task.allowed_actions = ['select_candidate'];
  wiring.consume(request, { task_state: actionDriven });
  assert.equal(wiring.current().reason, 'OK');
  assert.equal(wiring.allowsChild('select_candidate', candidateTarget), true);
}

request = wiring.begin('/api/session', 'json');
const retryable = structuredClone(fixtures.a2);
retryable.active_child_task.phase = 'ERROR';
retryable.active_child_task.status = 'FAILED';
retryable.active_child_task.next_stage = 'RETRY';
retryable.active_child_task.allowed_actions = ['retry_search'];
wiring.consume(request, { task_state: retryable });
const retryTarget = {
  childTaskId: candidateTarget.childTaskId,
  childTaskRevision: candidateTarget.childTaskRevision,
};
assert.equal(wiring.allowsChild('retry_search', retryTarget), true);
assert.equal(wiring.allowsChild('retry_search', { ...retryTarget, childTaskRevision: 8 }), false);
assert.equal(retryButton.disabled, false);
assert.equal(retryButton.hidden, false);
assert.equal(candidateButton.disabled, true);

request = wiring.begin('/api/session', 'json');
wiring.consume(request, { task_state: fixtures.inconsistent });
assert.equal(wiring.current().reason, 'SERVER_INCONSISTENT');
assert.equal(wiring.allowsChild('select_candidate', candidateTarget), false);

request = wiring.begin('/api/session', 'json');
wiring.consume(request, { task_state: { schema_version: 1 } });
assert.equal(wiring.current().reason, 'INVALID');
assert.equal(wiring.allowsChild('select_candidate', candidateTarget), false);

request = wiring.begin('/api/session', 'json');
const unsupported = structuredClone(fixtures.a2);
unsupported.schema_version = 2;
wiring.consume(request, { task_state: unsupported });
assert.equal(wiring.current().reason, 'UNSUPPORTED_SCHEMA');
assert.equal(wiring.allowsChild('select_candidate', candidateTarget), false);

const slowSession = wiring.begin('/api/session', 'json');
const latestStream = wiring.begin('/api/image/stream', 'stream');
const latestEnvelope = {
  task_state: fixtures.a3,
  session: {
    a3: {
      enabled: true,
      phase: fixtures.a3.workflow.phase,
      task_revision: fixtures.a3.workflow.task_revision,
      units: fixtures.a3.units.map((unit) => ({
        unit_id: unit.unit_id,
        page_index: unit.page_index,
        display_label: unit.display_label,
        selected: unit.unit_id === fixtures.a3.current_unit.unit_id,
      })),
      selected_unit: {
        unit_id: fixtures.a3.current_unit.unit_id,
        display_label: fixtures.a3.current_unit.display_label,
      },
    },
  },
};
wiring.consume(latestStream, latestEnvelope);
const latestModel = wiring.current();
assert.deepEqual(wiring.bindingFor(latestEnvelope), {
  workflowId: fixtures.a3.workflow.workflow_id,
  workflowRevision: fixtures.a3.workflow.task_revision,
});
const staleEnvelope = { task_state: fixtures.a2 };
wiring.consume(slowSession, staleEnvelope);
assert.strictEqual(wiring.current(), latestModel);
assert.equal(wiring.current().snapshot.workflow.task_revision, 9);
assert.equal(wiring.bindingFor(staleEnvelope), null);
const duplicateEnvelope = { task_state: fixtures.a3 };
wiring.consume(latestStream, duplicateEnvelope);
assert.strictEqual(wiring.current(), latestModel);
assert.equal(wiring.bindingFor(duplicateEnvelope), null);

request = wiring.begin('/api/session', 'json');
const mismatchedLabelEnvelope = structuredClone(latestEnvelope);
mismatchedLabelEnvelope.session.a3.selected_unit.display_label = 'poison';
wiring.consume(request, mismatchedLabelEnvelope);
assert.equal(wiring.current().reason, 'MISSING');
assert.equal(wiring.current().actions_enabled, false);
assert.equal(wiring.bindingFor(mismatchedLabelEnvelope), null);

request = wiring.begin('/api/session', 'json');
const mismatchedUnitStateEnvelope = structuredClone(latestEnvelope);
mismatchedUnitStateEnvelope.session.a3.units[0].completed = true;
wiring.consume(request, mismatchedUnitStateEnvelope);
assert.equal(wiring.current().reason, 'MISSING');
assert.equal(wiring.current().actions_enabled, false);
assert.equal(wiring.bindingFor(mismatchedUnitStateEnvelope), null);

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
  async function runSessionBootstrap() { sessionRepairs += 1; }
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

    def test_real_transport_lifecycle_updates_session_and_does_not_replay_a3(self):
        fixtures = {
            "empty": contract.empty_task_state_snapshot().to_dict(),
            "a3": _a3_action_snapshot().to_dict(),
            "inconsistent": _inconsistent_workflow_snapshot().to_dict(),
        }
        demo_source = (ROOT / "tiku_agent" / "demo_web" / "demo.js").read_text(
            encoding="utf-8"
        )
        send_text_source = demo_source.split("async function sendTextValue", 1)[1].split(
            "async function sendText()", 1
        )[0]
        upload_source = demo_source.split("async function uploadImage", 1)[1].split(
            "function openDrawer", 1
        )[0]
        self.assertLess(
            send_text_source.index("await sessionTaskStartAllowed()"),
            send_text_source.index("taskStateAllowsChildAction"),
        )
        self.assertLess(
            upload_source.index("await sessionTaskStartAllowed()"),
            upload_source.index("validateImage(selected)"),
        )
        self.assertIn("window.addEventListener('storage'", demo_source)
        self.assertIn("event.key === SESSION_RESET_EVENT_KEY", demo_source)
        self.assertIn("retireSessionForExternalReset();", demo_source)
        self.assertEqual(demo_source.count("localStorage.getItem("), 1)
        self.assertEqual(demo_source.count("localStorage.setItem("), 1)
        self.assertEqual(demo_source.count("localStorage.removeItem("), 1)
        node_test = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const taskStateV1 = require('./tiku_agent/demo_web/task_state.js');
const fixtures = globalThis.__a3Fixtures;
const source = fs.readFileSync('./tiku_agent/demo_web/demo.js', 'utf8');
let sessionLockGate = null;
let sessionLockTail = Promise.resolve();
const sessionLockManager = {
  request: (_name, _options, callback) => {
    const previous = sessionLockTail;
    let release;
    sessionLockTail = new Promise((resolve) => { release = resolve; });
    return (async () => {
      await previous;
      if (sessionLockGate) await sessionLockGate;
      try {
        return await callback();
      } finally {
        release();
      }
    })();
  },
};
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  value: { locks: sessionLockManager },
});

function block(start, end) {
  const startIndex = source.indexOf(start);
  const endIndex = source.indexOf(end, startIndex);
  assert.notEqual(startIndex, -1, `missing source start: ${start}`);
  assert.notEqual(endIndex, -1, `missing source end: ${end}`);
  return source.slice(startIndex, endIndex);
}

const constants = block('const TASK_STATE_JSON_PATHS', 'const a3PrepareSelection');
const errorClass = block('class UserVisibleError', 'let history =');
const requestIdSource = block('function createRequestId', 'function taskStateApiPath');
const consumerSource = block('function taskStateApiPath', 'function currentChildActionTarget');
const workflowSource = block(
  'function currentWorkflowActionTarget',
  'function workflowActionTargetFromControl',
);
const identitySource = block('function workflowIdentityKey', 'function validA3Bounds');
const protocolSource = block('function protocolFields', 'function saveHistory');
const recoverySource = block('function normalizeRecoveryActions', 'function normalizeA3Snapshot');
const normalizeA3Source = block('function normalizeA3Snapshot', 'function openLightbox');
const transportSource = block('function safeHttpError', 'function responseItem');
const responseSource = block('function responseItem', 'function setResponseStatus');
const updateSessionSource = block('function updateSessionContext', 'function invalidateCandidateActions');
const repairSessionSource = block(
  'async function repairUploadedImageHistory()',
  'function clearA3WorkflowState()',
);
const retireSessionSource = block('function retireSessionForExternalReset()', 'function expireHistoryIfNeeded()');
const restoreHistorySource = block('function restoreHistory()', 'function flushStartupNotices()');
const expirySource = block('function expireHistoryIfNeeded()', 'function showSessionExpiredNotice()');

const createHarness = new Function('taskStateV1', 'sharedSessionStorage', `
  ${constants}
  const RECOVERY_ACTION_LABELS = {
    relogin: '重新登录', reupload: '重新上传题图', new_chat: '开始新对话',
    retry_connection: '重新连接', retry_request: '重试上一条', retry_search: '重试搜索',
  };
  ${errorClass}
  let sessionContext = {
    session_valid: false, search_id: '', a3: null,
    a3WorkflowId: '', a3WorkflowRevision: 0,
  };
  let a3SourceUrl = '';
  let a3SourceWorkflowKey = '';
  let sessionResetRequired = false;
  let sessionResetActivityAt = 0;
  let sessionResetEpoch = 0;
  let lastHandledSessionResetEventId = '';
  let lastHandledSessionRequestFenceId = '';
  let sessionBootstrap = null;
  let sessionBootstrapPending = false;
  let pendingSessionExpiredNotice = false;
  let pendingHistoryStorageNotice = '';
  let activeController = null;
  let history = [{ message: 'existing history' }];
  let historyLastActivityAt = 0;
  let historyExpiryTimer = null;
  let operationVersion = 0;
  let isBusy = false;
  const HISTORY_TTL_MS = 2 * 60 * 60 * 1000;
  const HISTORY_KEY = 'history';
  const LEGACY_HISTORY_KEY = 'legacy-history';
  const SESSION_ACTIVITY_KEY = 'session-activity';
  const SESSION_RESET_EVENT_KEY = 'session-reset-event';
  const SESSION_STORAGE_PROBE_KEY = 'session-storage-probe';
  const SESSION_REQUEST_FENCE_KEY = 'session-request-fence';
  const SESSION_REQUEST_LOCK_NAME = 'session-request-lock';
  const taskStateEnvelopeBindings = new WeakMap();
  const taskStateAcceptedEnvelopes = new WeakMap();
  const lifecycle = [];
  const delegate = taskStateV1.createTaskStateConsumer();
  let lastTaskStateRequest = null;
  const taskStateConsumer = {
    current: () => delegate.current(),
    begin: () => {
      lifecycle.push('begin');
      lastTaskStateRequest = delegate.begin();
      return lastTaskStateRequest;
    },
    consume: (...args) => { lifecycle.push('consume'); return delegate.consume(...args); },
    finish: (...args) => { lifecycle.push('finish'); return delegate.finish(...args); },
  };
  let taskStateContext = taskStateConsumer.current();
  let activeTaskStateRequest = null;
  let taskStateRequestGeneration = 0;
  let activeTaskStateRequestGeneration = 0;
  let syncA3Count = 0;
  let immediateTimeout = false;
  let timerId = 0;
  let clearHistoryCount = 0;
  let clearA3WorkflowCount = 0;
  let storedHistory = null;
  const sharedStorage = sharedSessionStorage || {
    activityAt: 0, resetEventId: '', probe: '', requestFenceId: '',
  };
  let storageFailures = { get: false, set: false, remove: false, resetSet: false };
  const resetEvents = [];
  const fetchPlans = [];
  const fetchUrls = [];
  const failureNotices = [];
  const statusUpdates = [];

  function setTimeout(callback) {
    const id = ++timerId;
    if (immediateTimeout) callback();
    return id;
  }
  function clearTimeout() {}
  function syncTaskStateActionButtons() {}
  function setBusy(value) { isBusy = Boolean(value); }
  function setStatus(state, message) { statusUpdates.push({ state, message }); }
  function syncA3Interface() { syncA3Count += 1; }
  function resolveFailureNotice() {}
  function renderHistory() {}
  function clearHistory() {
    clearHistoryCount += 1;
    history = [];
    safeLocalStorageRemove(HISTORY_KEY);
    safeLocalStorageRemove(LEGACY_HISTORY_KEY);
  }
  function clearA3WorkflowState() {
    clearA3WorkflowCount += 1;
    a3SourceUrl = '';
    a3SourceWorkflowKey = '';
    closeLightbox();
  }
  function restoreA3CropHistoryState() {}
  function scheduleHistoryExpiry() {}
  function closeLightbox() {}
  function showSessionExpiredNotice() {}
  function flushStartupNotices() {}
  function showFailureNotice(key, message, recoveryActions = [], protocol = {}) {
    failureNotices.push({ key, message, recoveryActions, protocol });
  }
  function saveHistory() {}
  function isLegacyInlineOnlyMessage() { return false; }
  function currentChildActionTarget() { return null; }
  function normalizeAuthorContact() { return null; }
  function normalizeFeedbackImages() { return []; }
  function isPersistentImage(url) {
    return typeof url === 'string' && url.startsWith('/api/media/');
  }
  const localStorage = {
    getItem(key) {
      if (storageFailures.get) {
        const error = new Error('storage get blocked');
        error.name = 'SecurityError';
        throw error;
      }
      if (key === SESSION_ACTIVITY_KEY) return sharedStorage.activityAt ? String(sharedStorage.activityAt) : null;
      if (key === SESSION_RESET_EVENT_KEY) return sharedStorage.resetEventId || null;
      if (key === SESSION_STORAGE_PROBE_KEY) return sharedStorage.probe || null;
      if (key === SESSION_REQUEST_FENCE_KEY) return sharedStorage.requestFenceId || null;
      if (key === HISTORY_KEY || key === LEGACY_HISTORY_KEY) {
        return storedHistory === null ? null : JSON.stringify(storedHistory);
      }
      return null;
    },
    setItem(key, value) {
      if (
        storageFailures.set
        || (storageFailures.resetSet && key === SESSION_RESET_EVENT_KEY)
      ) {
        const error = new Error('storage set blocked');
        error.name = 'SecurityError';
        throw error;
      }
      if (key === SESSION_ACTIVITY_KEY) sharedStorage.activityAt = Number(value || 0);
      if (key === SESSION_RESET_EVENT_KEY) {
        sharedStorage.resetEventId = String(value || '');
        resetEvents.push(sharedStorage.resetEventId);
      }
      if (key === SESSION_STORAGE_PROBE_KEY) sharedStorage.probe = String(value || '');
      if (key === SESSION_REQUEST_FENCE_KEY) sharedStorage.requestFenceId = String(value || '');
    },
    removeItem(key) {
      if (storageFailures.remove) {
        const error = new Error('storage remove blocked');
        error.name = 'SecurityError';
        throw error;
      }
      if (key === SESSION_ACTIVITY_KEY) sharedStorage.activityAt = 0;
      if (key === SESSION_STORAGE_PROBE_KEY) sharedStorage.probe = '';
      if (key === SESSION_REQUEST_FENCE_KEY) sharedStorage.requestFenceId = '';
    },
  };
  function createMessageId() { return 'transport-lifecycle'; }

  function abortError() {
    const error = new Error('aborted');
    error.name = 'AbortError';
    return error;
  }

  function jsonResponse(data, { ok = true, status = 200 } = {}) {
    return {
      ok,
      status,
      headers: { get: () => 'application/json' },
      json: async () => data,
      text: async () => '',
    };
  }

  function queueDeferredJson(data) {
    let release;
    const response = new Promise((resolve) => {
      release = () => resolve(jsonResponse(data));
    });
    fetchPlans.push({ kind: 'deferred-json', response });
    return release;
  }

  function streamResponse(event) {
    const bytes = new TextEncoder().encode(JSON.stringify(event) + '\\n');
    let delivered = false;
    return {
      ok: true,
      status: 200,
      body: {
        getReader: () => ({
          read: async () => {
            if (delivered) return { value: undefined, done: true };
            delivered = true;
            return { value: bytes, done: false };
          },
          cancel: async () => {},
        }),
      },
    };
  }

  function queueDeferredHttpError(data, status = 409) {
    let release;
    const response = new Promise((resolve) => {
      release = () => resolve(jsonResponse(data, { ok: false, status }));
    });
    fetchPlans.push({ kind: 'deferred-response', response });
    return release;
  }

  function queueDeferredEvent(event) {
    let release;
    const response = new Promise((resolve) => {
      release = () => resolve(streamResponse(event));
    });
    fetchPlans.push({ kind: 'deferred-response', response });
    return release;
  }

  async function fetch(url, options = {}) {
    fetchUrls.push(String(url));
    const plan = fetchPlans.shift();
    assert.ok(plan, 'missing fetch plan for ' + url);
    if (options.signal?.aborted) throw abortError();
    if (plan.kind === 'timeout') {
      return new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(abortError()), { once: true });
      });
    }
    if (plan.kind === 'deferred-json') return plan.response;
    if (plan.kind === 'deferred-response') return plan.response;
    if (plan.kind === 'json') return jsonResponse(plan.data);
    if (plan.kind === 'http-error') {
      return jsonResponse(plan.data, { ok: false, status: plan.status });
    }
    return streamResponse(plan.event);
  }

  ${requestIdSource}
  ${workflowSource}
  ${identitySource}
  ${consumerSource}
  ${protocolSource}
  ${recoverySource}
  ${normalizeA3Source}
  ${transportSource}
  ${updateSessionSource}
  ${responseSource}
  ${repairSessionSource}
  ${retireSessionSource}
  ${restoreHistorySource}
  ${expirySource}

  function legacyA3(raw) {
    return {
      enabled: true,
      auto_crop_enabled: false,
      auto_prepare_all_enabled: true,
      auto_prepare_all_units: false,
      phase: raw.workflow.phase,
      page_finished: false,
      units: raw.units.map((unit) => ({
        unit_id: unit.unit_id,
        page_index: unit.page_index,
        display_label: unit.display_label,
        title_text: unit.display_label,
        completed: unit.status === 'COMPLETED',
        searched: unit.status === 'CLOSED',
        selected: unit.status === 'ACTIVE',
        requested: unit.status === 'PREPARED',
        crop_available: unit.status === 'PREPARED',
        preparation_status: unit.status === 'PREPARED' ? 'ready' : 'pending',
      })),
      selected_unit: { unit_id: '', display_label: '', context_text: '' },
      crop_draft: {},
      task_revision: raw.workflow.task_revision,
    };
  }

  function envelope(raw) {
    return {
      task_state: raw,
      text: '请选择一道题继续。',
      intent: 'a3_units_prepared',
      session: {
        session_valid: true,
        search_id: 'search_transport_lifecycle',
        task_revision: raw.workflow.task_revision,
        a3: legacyA3(raw),
      },
    };
  }

  return Object.freeze({
    request,
    requestStream,
    responseItem,
    applyResetSessionContext,
    repairUploadedImageHistory,
    runSessionBootstrap,
    sessionTaskStartAllowed,
    restoreHistory,
    duplicateLastEnvelope: (data) => consumeTaskStateResponse(lastTaskStateRequest, data),
    envelope,
    queueJson: (data) => fetchPlans.push({ kind: 'json', data }),
    queueHttpError: (data, status = 409) => fetchPlans.push({
      kind: 'http-error', data, status,
    }),
    queueDeferredJson,
    queueDeferredHttpError,
    queueDeferredEvent,
    queueEvent: (event) => fetchPlans.push({ kind: 'stream', event }),
    queueTimeout: () => fetchPlans.push({ kind: 'timeout' }),
    setImmediateTimeout: (value) => { immediateTimeout = Boolean(value); },
    lifecycle: () => [...lifecycle],
    model: () => taskStateContext,
    session: () => structuredClone(sessionContext),
    fetchUrls: () => [...fetchUrls],
    syncA3Count: () => syncA3Count,
    clearHistoryCount: () => clearHistoryCount,
    clearA3WorkflowCount: () => clearA3WorkflowCount,
    historyLength: () => history.length,
    setStoredHistory: (value) => { storedHistory = structuredClone(value); },
    resetRequired: () => sessionResetRequired,
    sourceState: () => ({ url: a3SourceUrl, workflowKey: a3SourceWorkflowKey }),
    resetEvents: () => [...resetEvents],
    requestFenceId: () => sharedStorage.requestFenceId,
    failureNotices: () => structuredClone(failureNotices),
    statusUpdates: () => structuredClone(statusUpdates),
    commitExternalReset: (eventId) => {
      localStorage.setItem(SESSION_RESET_EVENT_KEY, eventId);
      localStorage.removeItem(SESSION_ACTIVITY_KEY);
    },
    deliverExternalReset: (eventId) => retireUnhandledSessionReset(eventId),
    expireDuringRequest: () => {
      history = [{ message: 'expiring request' }];
      historyLastActivityAt = Date.now() - HISTORY_TTL_MS - 1;
      isBusy = true;
      const beforeOperation = operationVersion;
      return { expired: expireHistoryIfNeeded(), beforeOperation };
    },
    expiryState: () => ({
      operationVersion, isBusy, activeController: activeController !== null,
      modelReason: taskStateContext.reason,
      session: structuredClone(sessionContext),
    }),
    historyActivityAt: () => historyLastActivityAt,
    failureRecovery: (actions) => taskStateFailureRecoveryActions(actions),
    setStorageFailures: (value) => { storageFailures = { ...storageFailures, ...value }; },
    clearHistory,
  });
`);

const harness = createHarness(taskStateV1);

(async () => {
  const successEnvelope = harness.envelope(fixtures.a3);
  harness.queueJson(successEnvelope);
  const data = await harness.request('/api/session', {}, 1000, 'session timeout');
  assert.strictEqual(data, successEnvelope);
  assert.deepEqual(harness.lifecycle(), ['begin', 'consume', 'finish']);
  assert.equal(harness.model().reason, 'OK');

  harness.duplicateLastEnvelope(data);
  assert.deepEqual(harness.lifecycle().slice(-2), ['finish', 'consume']);
  const response = harness.responseItem(data);
  assert.equal(response.workflowId, fixtures.a3.workflow.workflow_id);
  assert.equal(response.workflowRevision, fixtures.a3.workflow.task_revision);
  assert.equal(harness.session().a3WorkflowId, fixtures.a3.workflow.workflow_id);
  assert.equal(harness.session().a3WorkflowRevision, fixtures.a3.workflow.task_revision);
  assert.equal(harness.syncA3Count(), 1);

  const projectionA = harness.envelope(fixtures.a3);
  projectionA.session.search_id = 'search_projection_a';
  harness.queueJson(projectionA);
  await harness.request('/api/session', {}, 1000, 'session timeout');

  const projectionB = harness.envelope(fixtures.a3);
  projectionB.session.search_id = 'search_projection_b';
  harness.queueJson(projectionB);
  await harness.request('/api/session', {}, 1000, 'session timeout');
  const projectedB = harness.responseItem(projectionB);
  assert.equal(projectedB.workflowId, fixtures.a3.workflow.workflow_id);
  const sessionAfterB = harness.session();
  assert.equal(sessionAfterB.search_id, 'search_projection_b');

  const staleProjectionA = harness.responseItem(projectionA);
  assert.equal(Object.hasOwn(staleProjectionA, 'workflowId'), false);
  assert.deepEqual(harness.session(), sessionAfterB);

  const staleSessionEnvelope = harness.envelope(fixtures.a3);
  staleSessionEnvelope.session.session_valid = false;
  staleSessionEnvelope.session.search_id = 'search_stale_session';
  const releaseStaleSession = harness.queueDeferredJson(staleSessionEnvelope);
  const repair = harness.repairUploadedImageHistory();

  const latestEnvelope = harness.envelope(fixtures.a3);
  harness.queueEvent({ type: 'result', data: latestEnvelope });
  const latestRequest = harness.requestStream(
    '/api/a3/select/stream', { method: 'POST' }, 1000, 'select timeout',
  );
  releaseStaleSession();
  await repair;
  const latestData = await latestRequest;
  harness.responseItem(latestData);
  const latestSession = harness.session();

  assert.equal(harness.model().reason, 'OK');
  assert.deepEqual(harness.session(), latestSession);
  assert.equal(harness.clearHistoryCount(), 0, 'stale /api/session must not clear history');
  assert.equal(harness.historyLength(), 1);
  assert.deepEqual(
    harness.lifecycle().slice(-6),
    ['begin', 'consume', 'finish', 'begin', 'consume', 'finish'],
  );

  const authoritativeSession = harness.session();
  const authoritativeSyncA3Count = harness.syncA3Count();
  const failClosedCases = [
    { name: 'missing', reason: 'MISSING' },
    { name: 'invalid', reason: 'INVALID', taskState: { schema_version: 1 } },
    { name: 'inconsistent', reason: 'SERVER_INCONSISTENT', taskState: fixtures.inconsistent },
    { name: 'pair_mismatch', reason: 'MISSING', taskState: fixtures.a3 },
  ];
  for (const testCase of failClosedCases) {
    for (const mode of ['json', 'stream']) {
      const poison = harness.envelope(fixtures.a3);
      poison.session.search_id = `search_poison_${testCase.name}_${mode}`;
      poison.session.a3.selected_unit = {
        unit_id: 'g1-poison', display_label: 'poison', context_text: 'poison',
      };
      if (testCase.name === 'missing') delete poison.task_state;
      else poison.task_state = structuredClone(testCase.taskState);

      let poisonError = null;
      if (mode === 'json') {
        harness.queueJson(poison);
        poisonError = await harness.request(
          '/api/session', {}, 1000, 'session timeout',
        ).then(() => null, (error) => error);
      } else {
        harness.queueEvent({ type: 'result', data: poison });
        poisonError = await harness.requestStream(
          '/api/a3/select/stream', { method: 'POST' }, 1000, 'select timeout',
        ).then(() => null, (error) => error);
      }
      assert.equal(poisonError?.code, 'RESPONSE_INVALID', `${testCase.name}/${mode}`);
      assert.ok(harness.requestFenceId(), `${testCase.name}/${mode}: fence must remain pending`);
      assert.equal(harness.model().reason, testCase.reason, `${testCase.name}/${mode}`);
      assert.equal(harness.model().actions_enabled, false, `${testCase.name}/${mode}`);
      assert.deepEqual(harness.session(), authoritativeSession, `${testCase.name}/${mode}`);
      assert.equal(harness.syncA3Count(), authoritativeSyncA3Count, `${testCase.name}/${mode}`);
      const reconciliation = harness.envelope(fixtures.a3);
      harness.queueJson(reconciliation);
      await harness.request('/api/session', {}, 1000, 'session timeout');
      assert.equal(harness.requestFenceId(), '', `${testCase.name}/${mode}: session reconciles`);
    }
  }

  for (const [unitIndex, mutate] of [
    [0, (unit) => { unit.crop_available = true; unit.preparation_status = 'ready'; }],
    [1, (unit) => { unit.crop_available = false; unit.preparation_status = 'pending'; }],
  ]) {
    const mismatch = harness.envelope(fixtures.a3);
    const unit = mismatch.session.a3.units[unitIndex];
    mutate(unit);
    harness.queueJson(mismatch);
    const mismatchError = await harness.request(
      '/api/session', {}, 1000, 'session timeout',
    ).then(() => null, (error) => error);
    assert.equal(mismatchError?.code, 'RESPONSE_INVALID');
    assert.ok(harness.requestFenceId());
    assert.equal(harness.model().reason, 'MISSING');
    assert.deepEqual(harness.session(), authoritativeSession);
    const reconciliation = harness.envelope(fixtures.a3);
    harness.queueJson(reconciliation);
    await harness.request('/api/session', {}, 1000, 'session timeout');
    assert.equal(harness.requestFenceId(), '');
  }

  const beforeQueue = harness.fetchUrls().length;
  harness.queueEvent({
    type: 'error', status: 'ERROR', layer: 'queue', code: 'QUEUE_FULL',
    retryable: true, action: 'retry_request', message: 'queue full',
    task_state: fixtures.a3,
  });
  let queueError = null;
  try {
    await harness.requestStream(
      '/api/a3/select/stream', { method: 'POST' }, 1000, 'select timeout',
    );
  } catch (error) {
    queueError = error;
  }
  assert.equal(queueError?.code, 'QUEUE_FULL');
  assert.equal(harness.model().reason, 'MISSING');
  assert.deepEqual(harness.session(), authoritativeSession);
  assert.equal(harness.failureRecovery(queueError.recoveryActions).includes('retry_connection'), true);
  assert.deepEqual(
    harness.fetchUrls().slice(beforeQueue),
    ['/api/a3/select/stream'],
    'queue failure must not replay the A3 action',
  );
  assert.equal(harness.requestFenceId(), '', 'queue no-update must resolve its fence');
  assert.deepEqual(harness.lifecycle().slice(-2), ['begin', 'finish']);

  for (const [layer, code] of [
    ['session', 'QUEUE_FULL'],
    ['queue', 'QUEUE_UNKNOWN'],
  ]) {
    const nonAdmissionQueueHarness = createHarness(taskStateV1);
    const active = nonAdmissionQueueHarness.envelope(fixtures.a3);
    nonAdmissionQueueHarness.queueJson(active);
    nonAdmissionQueueHarness.responseItem(
      await nonAdmissionQueueHarness.request('/api/session', {}, 1000, 'session timeout'),
    );
    nonAdmissionQueueHarness.queueEvent({
      type: 'error', status: 'ERROR', layer, code,
      retryable: true, action: 'retry_connection', message: 'not a queue admission no-update',
      task_state: fixtures.a3,
    });
    const nonAdmissionError = await nonAdmissionQueueHarness.requestStream(
      '/api/a3/select/stream', { method: 'POST' }, 1000, 'select timeout',
    ).then(() => null, (error) => error);
    assert.equal(nonAdmissionError?.code, code, `${layer}/${code}`);
    assert.ok(
      nonAdmissionQueueHarness.requestFenceId(),
      `${layer}/${code}: only registered queue admission no-update may resolve`,
    );
  }

  const beforeTimeout = harness.fetchUrls().length;
  harness.queueTimeout();
  harness.setImmediateTimeout(true);
  let timeoutError = null;
  try {
    await harness.requestStream(
      '/api/a3/select/stream', { method: 'POST' }, 1, 'select timeout',
    );
  } catch (error) {
    timeoutError = error;
  } finally {
    harness.setImmediateTimeout(false);
  }
  assert.equal(timeoutError?.code, 'REQUEST_TIMEOUT');
  assert.equal(harness.model().reason, 'MISSING');
  assert.deepEqual(harness.session(), authoritativeSession);
  assert.equal(harness.failureRecovery(timeoutError.recoveryActions).includes('retry_connection'), true);
  assert.deepEqual(
    harness.fetchUrls().slice(beforeTimeout),
    ['/api/a3/select/stream'],
    'timeout must not replay the A3 action',
  );
  assert.ok(harness.requestFenceId(), 'unknown task timeout must preserve its fence');
  assert.deepEqual(harness.lifecycle().slice(-2), ['begin', 'finish']);

  const resetSeed = harness.envelope(fixtures.a3);
  harness.queueJson(resetSeed);
  harness.responseItem(await harness.request('/api/session', {}, 1000, 'session timeout'));
  assert.equal(harness.requestFenceId(), '', 'authoritative session read must reconcile pending');
  assert.equal(harness.session().a3WorkflowId, fixtures.a3.workflow.workflow_id);
  const resetEnvelope = { ok: true, task_state: fixtures.empty };
  harness.queueJson(resetEnvelope);
  const resetData = await harness.request('/api/reset', { method: 'POST' }, 1000, 'reset timeout');
  assert.equal(harness.applyResetSessionContext(resetData), true);
  assert.deepEqual(harness.session(), {
    session_valid: false,
    phase: 'IDLE',
    has_active_image: false,
    task_revision: 0,
    candidate_generation: '',
    candidate_count: 0,
    search_id: '',
    a3: null,
    a3WorkflowId: '',
    a3WorkflowRevision: 0,
  });
  assert.equal(harness.model().reason, 'OK');
  assert.equal(harness.model().snapshot.workflow.exists, false);
  assert.equal(harness.resetEvents().length, 1);

  const emptyHttpHarness = createHarness(taskStateV1);
  const emptyHttpSeed = emptyHttpHarness.envelope(fixtures.a3);
  emptyHttpSeed.uploaded_image = '/api/media/empty-http-source.jpg';
  emptyHttpHarness.queueJson(emptyHttpSeed);
  emptyHttpHarness.responseItem(
    await emptyHttpHarness.request('/api/session', {}, 1000, 'session timeout'),
  );
  const emptyHttpClears = emptyHttpHarness.clearA3WorkflowCount();
  emptyHttpHarness.queueHttpError({
    status: 'ERROR', layer: 'session', code: 'STALE_ACTION', retryable: false,
    action: 'new_chat', message: 'session is empty', task_state: fixtures.empty,
  });
  let emptyHttpError = null;
  try {
    await emptyHttpHarness.request(
      '/api/reset', { method: 'POST' }, 1000, 'reset timeout',
    );
  } catch (error) {
    emptyHttpError = error;
  }
  assert.ok(emptyHttpError);
  assert.equal(emptyHttpHarness.historyLength(), 0);
  assert.equal(emptyHttpHarness.session().a3, null);
  assert.deepEqual(emptyHttpHarness.sourceState(), { url: '', workflowKey: '' });
  assert.equal(emptyHttpHarness.model().snapshot.workflow.exists, false);
  assert.equal(emptyHttpHarness.resetEvents().length, 1);
  assert.ok(emptyHttpHarness.clearA3WorkflowCount() > emptyHttpClears);

  const emptyStreamHarness = createHarness(taskStateV1);
  const emptyStreamSeed = emptyStreamHarness.envelope(fixtures.a3);
  emptyStreamSeed.uploaded_image = '/api/upload/empty-stream-source.jpg';
  emptyStreamHarness.queueJson(emptyStreamSeed);
  emptyStreamHarness.responseItem(
    await emptyStreamHarness.request('/api/session', {}, 1000, 'session timeout'),
  );
  const emptyStreamClears = emptyStreamHarness.clearA3WorkflowCount();
  emptyStreamHarness.queueEvent({
    type: 'error', status: 'ERROR', layer: 'session', code: 'STALE_ACTION',
    retryable: false, action: 'new_chat', message: 'session is empty',
    task_state: fixtures.empty,
  });
  let emptyStreamError = null;
  try {
    await emptyStreamHarness.requestStream(
      '/api/message/stream', { method: 'POST' }, 1000, 'message timeout',
    );
  } catch (error) {
    emptyStreamError = error;
  }
  assert.equal(emptyStreamError?.code, 'STALE_ACTION');
  assert.equal(emptyStreamHarness.historyLength(), 0);
  assert.equal(emptyStreamHarness.session().a3, null);
  assert.deepEqual(emptyStreamHarness.sourceState(), { url: '', workflowKey: '' });
  assert.equal(emptyStreamHarness.model().snapshot.workflow.exists, false);
  assert.equal(emptyStreamHarness.resetEvents().length, 1);
  assert.ok(emptyStreamHarness.clearA3WorkflowCount() > emptyStreamClears);

  for (const [name, taskState] of [
    ['missing', undefined],
    ['invalid', { schema_version: 1 }],
  ]) {
    const invalidResetHarness = createHarness(taskStateV1);
    const activeEnvelope = invalidResetHarness.envelope(fixtures.a3);
    activeEnvelope.uploaded_image = '/api/media/invalid-reset-source.jpg';
    invalidResetHarness.queueJson(activeEnvelope);
    invalidResetHarness.responseItem(
      await invalidResetHarness.request('/api/session', {}, 1000, 'session timeout'),
    );
    const activeSession = invalidResetHarness.session();
    const invalidReset = invalidResetHarness.envelope(fixtures.a3);
    invalidReset.intent = 'a3_session_reset';
    if (typeof taskState === 'undefined') delete invalidReset.task_state;
    else invalidReset.task_state = taskState;
    invalidResetHarness.queueEvent({ type: 'result', data: invalidReset });
    const invalidResetError = await invalidResetHarness.requestStream(
      '/api/message/stream', { method: 'POST' }, 1000, 'message timeout',
    ).then(() => null, (error) => error);
    assert.equal(invalidResetError?.code, 'RESPONSE_INVALID', name);
    assert.ok(invalidResetHarness.requestFenceId(), name);
    assert.deepEqual(invalidResetHarness.session(), activeSession, name);
    assert.equal(invalidResetHarness.historyLength(), 1, name);
    assert.equal(invalidResetHarness.resetEvents().length, 0, name);
    assert.equal(invalidResetHarness.sourceState().url, '/api/media/invalid-reset-source.jpg', name);
  }

  const sourceHarness = createHarness(taskStateV1);
  const sourceA = sourceHarness.envelope(fixtures.a3);
  sourceA.uploaded_image = '/api/media/source-a.jpg';
  sourceHarness.queueJson(sourceA);
  sourceHarness.responseItem(await sourceHarness.request('/api/session', {}, 1000, 'session timeout'));
  assert.equal(sourceHarness.sourceState().url, '/api/media/source-a.jpg');
  const workflowB = structuredClone(fixtures.a3);
  workflowB.workflow.workflow_id = 'search_frontend_actions_workflow_b_12345678';
  const sourceB = sourceHarness.envelope(workflowB);
  sourceHarness.queueJson(sourceB);
  sourceHarness.responseItem(await sourceHarness.request('/api/session', {}, 1000, 'session timeout'));
  assert.deepEqual(sourceHarness.sourceState(), { url: '', workflowKey: '' });

  const lateEnvelope = harness.envelope(fixtures.a3);
  const releaseLateResponse = harness.queueDeferredJson(lateEnvelope);
  const lateRequest = harness.request('/api/session', {}, 1000, 'session timeout');
  await Promise.resolve();
  const expiry = harness.expireDuringRequest();
  assert.equal(expiry.expired, true);
  assert.deepEqual(harness.expiryState(), {
    operationVersion: expiry.beforeOperation + 1,
    isBusy: false,
    activeController: false,
    modelReason: 'MISSING',
    session: {
      session_valid: false,
      phase: 'IDLE',
      has_active_image: false,
      task_revision: 0,
      candidate_generation: '',
      candidate_count: 0,
      search_id: '',
      a3: null,
      a3WorkflowId: '',
      a3WorkflowRevision: 0,
    },
  });
  releaseLateResponse();
  const lateError = await lateRequest.then(() => null, (error) => error);
  assert.equal(lateError?.code, 'RESPONSE_INVALID');
  assert.ok(harness.requestFenceId());
  assert.equal(harness.model().reason, 'MISSING');
  assert.equal(harness.session().a3, null);
  const lateReconciliation = harness.envelope(fixtures.a3);
  harness.queueJson(lateReconciliation);
  await harness.request('/api/session', {}, 1000, 'session timeout');
  assert.equal(harness.requestFenceId(), '', 'session must reconcile the late-response fence');

  const coldHarness = createHarness(taskStateV1);
  coldHarness.setStoredHistory({
    lastActivityAt: Date.now() - (2 * 60 * 60 * 1000) - 1,
    messages: [{ message: 'expired local A3 history' }],
  });
  coldHarness.restoreHistory();
  assert.equal(coldHarness.resetRequired(), true);
  coldHarness.queueJson({ ok: true, task_state: fixtures.empty });
  await coldHarness.repairUploadedImageHistory();
  assert.deepEqual(coldHarness.fetchUrls(), ['/api/reset']);
  assert.equal(coldHarness.resetRequired(), false);
  assert.equal(coldHarness.model().snapshot.workflow.exists, false);
  assert.equal(coldHarness.session().a3, null);

  const deferredResetHarness = createHarness(taskStateV1);
  deferredResetHarness.setStoredHistory({
    lastActivityAt: Date.now() - (2 * 60 * 60 * 1000) - 1,
    messages: [{ message: 'expired before deferred reset' }],
  });
  deferredResetHarness.restoreHistory();
  const releaseDeferredReset = deferredResetHarness.queueDeferredJson({
    ok: true, task_state: fixtures.empty,
  });
  const deferredBootstrap = deferredResetHarness.runSessionBootstrap();
  let taskGateSettled = false;
  const taskGate = deferredResetHarness.sessionTaskStartAllowed().then((allowed) => {
    taskGateSettled = true;
    return allowed;
  });
  await Promise.resolve();
  assert.equal(taskGateSettled, false, 'task start must wait for the reset response');
  assert.deepEqual(deferredResetHarness.fetchUrls(), ['/api/reset']);
  releaseDeferredReset();
  assert.equal(await deferredBootstrap, true);
  assert.equal(await taskGate, true);
  assert.equal(deferredResetHarness.resetRequired(), false);

  const failedResetHarness = createHarness(taskStateV1);
  failedResetHarness.setStoredHistory({
    lastActivityAt: Date.now() - (2 * 60 * 60 * 1000) - 1,
    messages: [{ message: 'expired before failed reset' }],
  });
  failedResetHarness.restoreHistory();
  failedResetHarness.queueTimeout();
  failedResetHarness.setImmediateTimeout(true);
  assert.equal(await failedResetHarness.runSessionBootstrap(), false);
  failedResetHarness.setImmediateTimeout(false);
  assert.equal(failedResetHarness.resetRequired(), true);
  assert.equal(await failedResetHarness.sessionTaskStartAllowed(), false);
  assert.deepEqual(failedResetHarness.fetchUrls(), ['/api/reset']);
  assert.equal(failedResetHarness.resetEvents().length, 0);
  assert.ok(failedResetHarness.requestFenceId());

  const unreconciledHarness = createHarness(taskStateV1);
  unreconciledHarness.queueJson({ ok: true });
  assert.equal(await unreconciledHarness.runSessionBootstrap(), false);
  assert.deepEqual(unreconciledHarness.fetchUrls(), ['/api/session']);
  assert.ok(unreconciledHarness.requestFenceId());
  const unreconciledNotice = unreconciledHarness.failureNotices().at(-1);
  assert.equal(unreconciledNotice.key, 'session-recovery');
  assert.equal(
    unreconciledNotice.message,
    '服务可以连接，但上次请求结果无法安全确认，当前对话不能继续。请开始新对话后重新上传题图。',
  );
  assert.deepEqual(unreconciledNotice.recoveryActions, ['new_chat']);
  assert.equal(unreconciledNotice.protocol.status, 'ERROR');
  assert.equal(unreconciledNotice.protocol.layer, 'session');
  assert.equal(unreconciledNotice.protocol.code, 'STALE_ACTION');
  assert.equal(unreconciledNotice.protocol.retryable, false);
  assert.equal(unreconciledNotice.protocol.action, 'new_chat');
  assert.match(unreconciledNotice.protocol.request_id, /^req_[A-Za-z0-9_-]+$/);
  assert.deepEqual(unreconciledHarness.statusUpdates().at(-1), {
    state: 'error', message: '需要开始新对话',
  });

  const activeTabHarness = createHarness(taskStateV1);
  const staleActivityAt = Date.now() - (2 * 60 * 60 * 1000) - 1;
  const freshActivityAt = Date.now();
  activeTabHarness.setStoredHistory({
    lastActivityAt: freshActivityAt,
    messages: [{ message: 'newer activity from another tab' }],
  });
  const refreshedExpiry = activeTabHarness.expireDuringRequest();
  assert.equal(refreshedExpiry.expired, false);
  assert.equal(activeTabHarness.resetRequired(), false);
  assert.equal(activeTabHarness.historyLength(), 1);
  assert.equal(activeTabHarness.historyActivityAt(), freshActivityAt);
  assert.ok(activeTabHarness.historyActivityAt() > staleActivityAt);

  const lockRecheckHarness = createHarness(taskStateV1);
  const lockExpiredAt = Date.now() - (2 * 60 * 60 * 1000) - 1;
  lockRecheckHarness.setStoredHistory({
    lastActivityAt: lockExpiredAt,
    messages: [{ message: 'expired before waiting for lock' }],
  });
  lockRecheckHarness.restoreHistory();
  lockRecheckHarness.queueJson(lockRecheckHarness.envelope(fixtures.a3));
  let releaseSessionLock;
  sessionLockGate = new Promise((resolve) => { releaseSessionLock = resolve; });
  const lockRecheckedBootstrap = lockRecheckHarness.runSessionBootstrap();
  await Promise.resolve();
  assert.deepEqual(lockRecheckHarness.fetchUrls(), []);
  const activityWhileWaiting = Date.now();
  lockRecheckHarness.setStoredHistory({
    lastActivityAt: activityWhileWaiting,
    messages: [{ message: 'new activity while reset waits for lock' }],
  });
  releaseSessionLock();
  assert.equal(await lockRecheckedBootstrap, true);
  sessionLockGate = null;
  assert.deepEqual(lockRecheckHarness.fetchUrls(), ['/api/session']);
  assert.equal(lockRecheckHarness.resetRequired(), false);
  assert.equal(lockRecheckHarness.resetEvents().length, 0);

  const queuedAfterResetHarness = createHarness(taskStateV1);
  let releaseQueuedTaskLock;
  sessionLockGate = new Promise((resolve) => { releaseQueuedTaskLock = resolve; });
  const queuedTask = queuedAfterResetHarness.request(
    '/api/message',
    { method: 'POST' },
    1000,
    'message timeout',
  ).then(
    () => null,
    (error) => error,
  );
  await Promise.resolve();
  assert.deepEqual(queuedAfterResetHarness.fetchUrls(), []);
  const delayedResetEventId = 'external-reset-before-lock-grant';
  const beforeQueuedReset = queuedAfterResetHarness.expiryState();
  queuedAfterResetHarness.commitExternalReset(delayedResetEventId);
  releaseQueuedTaskLock();
  const queuedTaskError = await queuedTask;
  sessionLockGate = null;
  assert.equal(queuedTaskError?.code, 'STALE_ACTION');
  assert.deepEqual(
    queuedAfterResetHarness.fetchUrls(),
    [],
    'a task granted the lock before its storage event must not reach fetch',
  );
  const afterQueuedReset = queuedAfterResetHarness.expiryState();
  assert.equal(afterQueuedReset.operationVersion, beforeQueuedReset.operationVersion + 1);
  assert.equal(afterQueuedReset.modelReason, 'MISSING');
  assert.equal(queuedAfterResetHarness.deliverExternalReset(delayedResetEventId), false);
  assert.equal(
    queuedAfterResetHarness.expiryState().operationVersion,
    afterQueuedReset.operationVersion,
    'the delayed storage event must not retire the same reset twice',
  );

  async function seedActiveHarness(target) {
    const active = target.envelope(fixtures.a3);
    target.queueJson(active);
    target.responseItem(await target.request('/api/session', {}, 1000, 'session timeout'));
  }

  async function waitForFetch(target, expectedUrl) {
    for (let index = 0; index < 20; index += 1) {
      if (target.fetchUrls().includes(expectedUrl)) return;
      await Promise.resolve();
    }
    assert.fail(`fetch did not start: ${expectedUrl}`);
  }

  for (const mode of ['json-http-error', 'stream-http-error', 'stream-error']) {
    const sharedStorage = {
      activityAt: 0, resetEventId: '', probe: '', requestFenceId: '',
    };
    const tabA = createHarness(taskStateV1, sharedStorage);
    const tabB = createHarness(taskStateV1, sharedStorage);
    await seedActiveHarness(tabA);
    await seedActiveHarness(tabB);
    const tabBBefore = tabB.expiryState();
    tabA.setStorageFailures({ resetSet: true });
    const emptyError = {
      type: 'error', status: 'ERROR', layer: 'session', code: 'STALE_ACTION',
      retryable: false, action: 'new_chat', message: 'session is empty',
      task_state: fixtures.empty,
    };
    let releaseTerminal;
    let tabARequest;
    let tabBRequest;
    if (mode === 'json-http-error') {
      releaseTerminal = tabA.queueDeferredHttpError(emptyError);
      tabARequest = tabA.request(
        '/api/message', { method: 'POST' }, 1000, 'message timeout',
      ).then(() => null, (error) => error);
      await waitForFetch(tabA, '/api/message');
      tabBRequest = tabB.request(
        '/api/image', { method: 'POST' }, 1000, 'image timeout',
      ).then(() => null, (error) => error);
    } else {
      releaseTerminal = mode === 'stream-http-error'
        ? tabA.queueDeferredHttpError(emptyError)
        : tabA.queueDeferredEvent(emptyError);
      tabARequest = tabA.requestStream(
        '/api/message/stream', { method: 'POST' }, 1000, 'message timeout',
      ).then(() => null, (error) => error);
      await waitForFetch(tabA, '/api/message/stream');
      tabBRequest = tabB.requestStream(
        '/api/image/stream', { method: 'POST' }, 1000, 'image timeout',
      ).then(() => null, (error) => error);
    }
    await Promise.resolve();
    assert.deepEqual(tabB.fetchUrls(), ['/api/session'], `${mode}: tab B must wait for the lock`);
    releaseTerminal();
    const [tabAError, tabBError] = await Promise.all([tabARequest, tabBRequest]);
    assert.equal(tabAError?.code, 'RESPONSE_INVALID', mode);
    assert.equal(tabBError?.code, 'STALE_ACTION', mode);
    assert.deepEqual(tabB.fetchUrls(), ['/api/session'], `${mode}: stale request must not fetch`);
    assert.equal(sharedStorage.resetEventId, '', `${mode}: failed commit is not a reset`);
    assert.ok(sharedStorage.requestFenceId, `${mode}: pending fence must survive`);
    assert.equal(tabA.historyLength(), 0, mode);
    const tabBAfter = tabB.expiryState();
    assert.equal(tabBAfter.operationVersion, tabBBefore.operationVersion + 1, mode);
    assert.equal(tabBAfter.modelReason, 'MISSING', mode);
    assert.equal(tabBAfter.session.session_valid, false, mode);
  }

  const explicitTimeoutStorage = {
    activityAt: 0, resetEventId: '', probe: '', requestFenceId: '',
  };
  const explicitTimeoutTabA = createHarness(taskStateV1, explicitTimeoutStorage);
  const explicitTimeoutTabB = createHarness(taskStateV1, explicitTimeoutStorage);
  await seedActiveHarness(explicitTimeoutTabA);
  await seedActiveHarness(explicitTimeoutTabB);
  const explicitTimeoutTabBSession = explicitTimeoutTabB.session();
  explicitTimeoutTabA.queueTimeout();
  explicitTimeoutTabA.setImmediateTimeout(true);
  const explicitTimeoutError = await explicitTimeoutTabA.request(
    '/api/reset', { method: 'POST' }, 1, 'reset timeout',
  ).then(() => null, (error) => error);
  explicitTimeoutTabA.setImmediateTimeout(false);
  assert.equal(explicitTimeoutError?.code, 'REQUEST_TIMEOUT');
  assert.equal(explicitTimeoutStorage.resetEventId, '');
  assert.ok(explicitTimeoutStorage.requestFenceId);
  assert.deepEqual(explicitTimeoutTabB.session(), explicitTimeoutTabBSession);
  assert.deepEqual(explicitTimeoutTabB.fetchUrls(), ['/api/session']);
  explicitTimeoutTabB.queueJson({ ok: true, task_state: fixtures.empty });
  globalThis.navigator.locks = null;
  const reconciledReset = await explicitTimeoutTabB.request(
    '/api/reset', { method: 'POST' }, 1000, 'reset timeout',
  );
  globalThis.navigator.locks = sessionLockManager;
  assert.equal(reconciledReset.task_state.workflow.exists, false);
  assert.ok(explicitTimeoutStorage.resetEventId);
  assert.equal(explicitTimeoutStorage.requestFenceId, '');
  assert.deepEqual(explicitTimeoutTabB.fetchUrls(), ['/api/session', '/api/reset']);

  const replacedFenceStorage = {
    activityAt: 0, resetEventId: '', probe: '', requestFenceId: 'older-pending-fence',
  };
  const replacedFenceHarness = createHarness(taskStateV1, replacedFenceStorage);
  const releaseReplacedFence = replacedFenceHarness.queueDeferredJson(
    replacedFenceHarness.envelope(fixtures.a3),
  );
  globalThis.navigator.locks = null;
  const replacedFenceRequest = replacedFenceHarness.request(
    '/api/session', {}, 1000, 'session timeout',
  ).then(() => null, (error) => error);
  await waitForFetch(replacedFenceHarness, '/api/session');
  replacedFenceStorage.requestFenceId = 'newer-pending-fence';
  releaseReplacedFence();
  const replacedFenceError = await replacedFenceRequest;
  globalThis.navigator.locks = sessionLockManager;
  assert.equal(replacedFenceError?.code, 'RESPONSE_INVALID');
  assert.equal(
    replacedFenceStorage.requestFenceId,
    'newer-pending-fence',
    'an older reconciliation must not compare-remove a newer fence',
  );

  const nonEmptyResetStorage = {
    activityAt: 0, resetEventId: '', probe: '', requestFenceId: '',
  };
  const nonEmptyResetTabA = createHarness(taskStateV1, nonEmptyResetStorage);
  const nonEmptyResetTabB = createHarness(taskStateV1, nonEmptyResetStorage);
  await seedActiveHarness(nonEmptyResetTabA);
  await seedActiveHarness(nonEmptyResetTabB);
  const nonEmptyResetTabBSession = nonEmptyResetTabB.session();
  const nonEmptyResetEnvelope = nonEmptyResetTabA.envelope(fixtures.a3);
  nonEmptyResetTabA.queueJson(nonEmptyResetEnvelope);
  assert.strictEqual(
    await nonEmptyResetTabA.request(
      '/api/reset', { method: 'POST' }, 1000, 'reset timeout',
    ),
    nonEmptyResetEnvelope,
  );
  assert.equal(nonEmptyResetStorage.resetEventId, '');
  assert.equal(nonEmptyResetStorage.requestFenceId, '');
  assert.deepEqual(nonEmptyResetTabB.session(), nonEmptyResetTabBSession);

  const storageDeniedHarness = createHarness(taskStateV1);
  storageDeniedHarness.setStorageFailures({ get: true, set: true, remove: true });
  storageDeniedHarness.queueJson(storageDeniedHarness.envelope(fixtures.a3));
  const storageDeniedError = await storageDeniedHarness.request('/api/message', {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}',
  }).then(() => null, (error) => error);
  assert.equal(storageDeniedError?.code, 'RESPONSE_INVALID');
  assert.deepEqual(storageDeniedHarness.fetchUrls(), []);
  assert.equal(storageDeniedHarness.model().reason, 'MISSING');
  assert.doesNotThrow(() => storageDeniedHarness.restoreHistory());
  assert.doesNotThrow(() => storageDeniedHarness.clearHistory());

  const storageDeniedStreamHarness = createHarness(taskStateV1);
  storageDeniedStreamHarness.setStorageFailures({ get: true, set: true, remove: true });
  storageDeniedStreamHarness.queueEvent({
    type: 'result', data: storageDeniedStreamHarness.envelope(fixtures.a3),
  });
  const storageDeniedStreamError = await storageDeniedStreamHarness.requestStream(
    '/api/message/stream',
    { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' },
    1000,
    'timeout',
  ).then(() => null, (error) => error);
  assert.equal(storageDeniedStreamError?.code, 'RESPONSE_INVALID');
  assert.deepEqual(storageDeniedStreamHarness.fetchUrls(), []);
  assert.equal(storageDeniedStreamHarness.model().reason, 'MISSING');

  const storageDeniedResetHarness = createHarness(taskStateV1);
  storageDeniedResetHarness.setStoredHistory({
    lastActivityAt: Date.now() - (2 * 60 * 60 * 1000) - 1,
    messages: [{ message: 'expired before storage becomes unavailable' }],
  });
  storageDeniedResetHarness.restoreHistory();
  assert.equal(storageDeniedResetHarness.resetRequired(), true);
  storageDeniedResetHarness.setStorageFailures({ get: true, set: true, remove: true });
  storageDeniedResetHarness.queueJson({ ok: true, task_state: fixtures.empty });
  assert.equal(await storageDeniedResetHarness.runSessionBootstrap(), false);
  assert.deepEqual(storageDeniedResetHarness.fetchUrls(), []);
  assert.equal(storageDeniedResetHarness.resetRequired(), true);

  const resetPublishDeniedHarness = createHarness(taskStateV1);
  resetPublishDeniedHarness.setStoredHistory({
    lastActivityAt: Date.now() - (2 * 60 * 60 * 1000) - 1,
    messages: [{ message: 'expired before reset publication fails' }],
  });
  resetPublishDeniedHarness.restoreHistory();
  assert.equal(resetPublishDeniedHarness.resetRequired(), true);
  resetPublishDeniedHarness.setStorageFailures({ resetSet: true });
  resetPublishDeniedHarness.queueJson({ ok: true, task_state: fixtures.empty });
  assert.equal(await resetPublishDeniedHarness.runSessionBootstrap(), false);
  assert.deepEqual(resetPublishDeniedHarness.fetchUrls(), ['/api/reset']);
  assert.equal(resetPublishDeniedHarness.resetRequired(), false);
  assert.equal(resetPublishDeniedHarness.resetEvents().length, 0);
  assert.ok(resetPublishDeniedHarness.requestFenceId());
  const resetPublishBlockedError = await resetPublishDeniedHarness.request(
    '/api/message', { method: 'POST' }, 1000, 'message timeout',
  ).then(() => null, (error) => error);
  assert.equal(resetPublishBlockedError?.code, 'STALE_ACTION');
  assert.deepEqual(resetPublishDeniedHarness.fetchUrls(), ['/api/reset']);

  const explicitPublishDeniedHarness = createHarness(taskStateV1);
  const explicitActive = explicitPublishDeniedHarness.envelope(fixtures.a3);
  explicitPublishDeniedHarness.queueJson(explicitActive);
  explicitPublishDeniedHarness.responseItem(
    await explicitPublishDeniedHarness.request('/api/session', {}, 1000, 'session timeout'),
  );
  const explicitActiveSession = explicitPublishDeniedHarness.session();
  explicitPublishDeniedHarness.setStorageFailures({ resetSet: true });
  explicitPublishDeniedHarness.queueJson({ ok: true, task_state: fixtures.empty });
  let explicitPublishError = null;
  try {
    await explicitPublishDeniedHarness.request(
      '/api/reset', { method: 'POST' }, 1000, 'reset timeout',
    );
  } catch (error) {
    explicitPublishError = error;
  }
  assert.equal(explicitPublishError?.code, 'RESPONSE_INVALID');
  assert.deepEqual(explicitPublishDeniedHarness.fetchUrls(), ['/api/session', '/api/reset']);
  assert.notDeepEqual(explicitPublishDeniedHarness.session(), explicitActiveSession);
  assert.equal(explicitPublishDeniedHarness.session().session_valid, false);
  assert.equal(explicitPublishDeniedHarness.historyLength(), 0);
  assert.equal(explicitPublishDeniedHarness.resetEvents().length, 0);
  assert.ok(explicitPublishDeniedHarness.requestFenceId());

  const noLockTombstoneHarness = createHarness(taskStateV1);
  await seedActiveHarness(noLockTombstoneHarness);
  const noLockResetEventId = 'committed-before-no-lock-request';
  noLockTombstoneHarness.commitExternalReset(noLockResetEventId);
  globalThis.navigator.locks = null;
  const noLockStaleError = await noLockTombstoneHarness.request(
    '/api/message', { method: 'POST' }, 1000, 'message timeout',
  ).then(() => null, (error) => error);
  globalThis.navigator.locks = sessionLockManager;
  assert.equal(noLockStaleError?.code, 'STALE_ACTION');
  assert.deepEqual(noLockTombstoneHarness.fetchUrls(), ['/api/session']);
  assert.equal(noLockTombstoneHarness.session().session_valid, false);
  assert.equal(noLockTombstoneHarness.deliverExternalReset(noLockResetEventId), false);

  const noLockUnsupportedHarness = createHarness(taskStateV1);
  const noLockSessionEnvelope = noLockUnsupportedHarness.envelope(fixtures.a3);
  noLockUnsupportedHarness.queueJson(noLockSessionEnvelope);
  globalThis.navigator.locks = null;
  noLockUnsupportedHarness.responseItem(
    await noLockUnsupportedHarness.request('/api/session', {}, 1000, 'session timeout'),
  );
  const noLockUnsupportedError = await noLockUnsupportedHarness.request(
    '/api/message', { method: 'POST' }, 1000, 'message timeout',
  ).then(() => null, (error) => error);
  globalThis.navigator.locks = sessionLockManager;
  assert.equal(noLockUnsupportedError?.code, 'RESPONSE_INVALID');
  assert.equal(noLockUnsupportedError?.recoveryActions?.includes('retry_connection'), true);
  assert.deepEqual(noLockUnsupportedHarness.fetchUrls(), ['/api/session']);

  const noLockHarness = createHarness(taskStateV1);
  noLockHarness.setStoredHistory({
    lastActivityAt: Date.now() - (2 * 60 * 60 * 1000) - 1,
    messages: [{ message: 'expired without Web Locks' }],
  });
  noLockHarness.restoreHistory();
  globalThis.navigator.locks = null;
  assert.equal(await noLockHarness.runSessionBootstrap(), false);
  globalThis.navigator.locks = sessionLockManager;
  assert.equal(noLockHarness.resetRequired(), true);
  assert.deepEqual(noLockHarness.fetchUrls(), []);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        result = subprocess.run(
            [shutil.which("node"), "-"],
            cwd=ROOT,
            input=f"globalThis.__a3Fixtures = {json.dumps(fixtures, ensure_ascii=False)};\n{node_test}",
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a3_envelope_binding_survives_response_history_roundtrip(self):
        fixtures = {
            "select": _a3_action_snapshot().to_dict(),
            "select_new_workflow": _a3_action_snapshot(
                workflow_id="search_frontend_history_workflow_new_12345678"
            ).to_dict(),
        }
        node_test = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const taskStateV1 = require('./tiku_agent/demo_web/task_state.js');
const fixtures = globalThis.__a3Fixtures;
const source = fs.readFileSync('./tiku_agent/demo_web/demo.js', 'utf8');

function block(start, end) {
  const startIndex = source.indexOf(start);
  const endIndex = source.indexOf(end, startIndex);
  assert.notEqual(startIndex, -1, `missing source start: ${start}`);
  assert.notEqual(endIndex, -1, `missing source end: ${end}`);
  return source.slice(startIndex, endIndex);
}

const constants = block('const TASK_STATE_JSON_PATHS', 'const a3PrepareSelection');
const consumerSource = block('function taskStateApiPath', 'function currentChildActionTarget');
const workflowSource = block('function currentWorkflowActionTarget', 'function syncA2ActionButtons');
const rememberSource = block('function remember', 'function scrollToLatest');
const normalizeA3Source = block('function normalizeA3Snapshot', 'function openLightbox');
const responseSource = block('function responseItem', 'function setResponseStatus');
const updateSessionSource = block('function updateSessionContext', 'function invalidateCandidateActions');
const currentA3Source = block('function a3Current', 'function currentA3CropActionTarget');
const identitySource = block('function workflowIdentityKey', 'function validA3Bounds');

const createHarness = new Function('taskStateV1', `
  ${constants}
  let sessionContext = {
    a3: null, a3WorkflowId: '', a3WorkflowRevision: 0,
  };
  const taskStateConsumer = taskStateV1.createTaskStateConsumer();
  const taskStateEnvelopeBindings = new WeakMap();
  const taskStateAcceptedEnvelopes = new WeakMap();
  let taskStateContext = taskStateConsumer.current();
  let activeTaskStateRequest = null;
  let taskStateRequestGeneration = 0;
  let activeTaskStateRequestGeneration = 0;
  let a3SourceUrl = '';
  let a3SourceWorkflowKey = '';
  let sessionResetRequired = false;
  let history = [];

  function syncTaskStateActionButtons() {}
  function syncA3Interface() {}
  function currentChildActionTarget() { return null; }
  function protocolFields() { return {}; }
  function protocolRecoveryAction() { return ''; }
  function normalizeAuthorContact() { return null; }
  function normalizeFeedbackImages() { return []; }
  function normalizeRecoveryActions() { return []; }
  function normalizeRetryAction() { return null; }
  function isPersistentImage() { return false; }
  function createMessageId() { return 'message-history-binding'; }
  function saveHistory() {}

  ${normalizeA3Source}
  ${workflowSource}
  ${currentA3Source}
  ${identitySource}
  ${consumerSource}
  ${updateSessionSource}
  ${responseSource}
  ${rememberSource}

  function legacyFromSnapshot(raw) {
    return {
      enabled: true,
      auto_crop_enabled: false,
      auto_prepare_all_enabled: false,
      auto_prepare_all_units: false,
      phase: raw.workflow.phase,
      page_finished: false,
      units: raw.units.map((unit) => ({
        unit_id: unit.unit_id,
        page_index: unit.page_index,
        display_label: unit.display_label,
        title_text: unit.display_label,
        completed: unit.status === 'COMPLETED',
        searched: unit.status === 'CLOSED',
        selected: unit.status === 'ACTIVE',
        requested: unit.status === 'PREPARED',
        crop_available: unit.status === 'PREPARED',
        preparation_status: unit.status === 'PREPARED' ? 'ready' : 'pending',
      })),
      selected_unit: { unit_id: '', display_label: '', context_text: '' },
      crop_draft: {},
      task_revision: raw.workflow.task_revision,
    };
  }

  function envelope(raw) {
    return {
      task_state: raw,
      text: '请选择一道题继续。',
      intent: 'a3_units_prepared',
      session: { a3: legacyFromSnapshot(raw) },
    };
  }

  function consumeAndRemember(raw, path = '/api/session') {
    const data = envelope(raw);
    const request = beginTaskStateRequest(path, 'json');
    consumeTaskStateResponse(request, data);
    const item = responseItem(data);
    remember(item);
    finishTaskStateRequest(request);
    return {
      data,
      item: structuredClone(item),
      stored: JSON.parse(JSON.stringify(history.at(-1))),
    };
  }

  return Object.freeze({
    consumeAndRemember,
    envelope,
    begin: beginTaskStateRequest,
    consume: consumeTaskStateResponse,
    respond: responseItem,
    remember,
    stored: () => JSON.parse(JSON.stringify(history.at(-1))),
    session: () => structuredClone(sessionContext),
    allowsA3: taskStateAllowsA3Action,
  });
`);

const harness = createHarness(taskStateV1);
const first = harness.consumeAndRemember(fixtures.select);
const firstTarget = {
  workflowId: fixtures.select.workflow.workflow_id,
  workflowRevision: fixtures.select.workflow.task_revision,
};
assert.deepEqual(
  { workflowId: first.item.workflowId, workflowRevision: first.item.workflowRevision },
  firstTarget,
);
assert.deepEqual(
  { workflowId: first.stored.workflowId, workflowRevision: first.stored.workflowRevision },
  firstTarget,
);
assert.equal(harness.allowsA3('select_unit', {
  ...first.stored, unitId: 'g1-u1',
}, first.stored.a3), true);

const newer = harness.consumeAndRemember(fixtures.select_new_workflow, '/api/image');
const newerTarget = {
  workflowId: fixtures.select_new_workflow.workflow.workflow_id,
  workflowRevision: fixtures.select_new_workflow.workflow.task_revision,
};
assert.deepEqual(
  { workflowId: newer.stored.workflowId, workflowRevision: newer.stored.workflowRevision },
  newerTarget,
);
assert.equal(harness.allowsA3('select_unit', {
  ...first.stored, unitId: 'g1-u1',
}, first.stored.a3), false);
assert.equal(harness.allowsA3('select_unit', {
  ...newer.stored, unitId: 'g1-u1',
}, newer.stored.a3), true);

const slow = harness.begin('/api/session', 'json');
const latest = harness.begin('/api/image', 'json');
const latestEnvelope = harness.envelope(fixtures.select_new_workflow);
harness.consume(latest, latestEnvelope);
const latestItem = harness.respond(latestEnvelope);
assert.equal(latestItem.workflowId, newerTarget.workflowId);

const staleEnvelope = harness.envelope(fixtures.select);
harness.consume(slow, staleEnvelope);
const staleItem = harness.respond(staleEnvelope);
harness.remember(staleItem);
assert.equal(Object.hasOwn(staleItem, 'workflowId'), false);
assert.equal(Object.hasOwn(staleItem, 'workflowRevision'), false);
assert.equal(harness.stored().workflowId, '');
assert.equal(harness.session().a3WorkflowId, newerTarget.workflowId);
assert.equal(harness.session().a3WorkflowRevision, newerTarget.workflowRevision);

const duplicateEnvelope = harness.envelope(fixtures.select_new_workflow);
harness.consume(latest, duplicateEnvelope);
const duplicateItem = harness.respond(duplicateEnvelope);
assert.equal(Object.hasOwn(duplicateItem, 'workflowId'), false);
assert.equal(Object.hasOwn(duplicateItem, 'workflowRevision'), false);

const missingBindingItem = harness.respond(harness.envelope(fixtures.select_new_workflow));
assert.equal(Object.hasOwn(missingBindingItem, 'workflowId'), false);
assert.equal(Object.hasOwn(missingBindingItem, 'workflowRevision'), false);
"""
        result = subprocess.run(
            [shutil.which("node"), "-"],
            cwd=ROOT,
            input=f"globalThis.__a3Fixtures = {json.dumps(fixtures, ensure_ascii=False)};\n{node_test}",
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a3_handlers_use_bound_workflow_actions_and_fail_closed(self):
        select_snapshot = _a3_action_snapshot()
        fixtures = {
            "select": select_snapshot.to_dict(),
            "select_new_workflow": _a3_action_snapshot(
                workflow_id="search_frontend_actions_workflow_new_12345678"
            ).to_dict(),
            "select_next_stage_only": replace(
                select_snapshot,
                workflow=replace(select_snapshot.workflow, allowed_actions=()),
            ).to_dict(),
            "prepare_two": _a3_action_snapshot(
                workflow_id="search_frontend_prepare_workflow_12345678",
                unit_statuses=(
                    contract.UNIT_AVAILABLE,
                    contract.UNIT_AVAILABLE,
                    contract.UNIT_COMPLETED,
                    contract.UNIT_CLOSED,
                ),
            ).to_dict(),
            "prepare_action_removed": replace(
                select_snapshot,
                workflow=replace(
                    select_snapshot.workflow,
                    allowed_actions=(contract.ACTION_SELECT_UNIT,),
                ),
            ).to_dict(),
            "crop": _a3_action_snapshot(
                phase="CROP_REQUIRED",
                allowed_actions=(
                    contract.ACTION_SUBMIT_CROP,
                    contract.ACTION_SELECT_UNIT,
                    contract.ACTION_PREPARE_UNITS,
                ),
                unit_statuses=(
                    contract.UNIT_ACTIVE,
                    contract.UNIT_AVAILABLE,
                    contract.UNIT_COMPLETED,
                    contract.UNIT_CLOSED,
                ),
            ).to_dict(),
            "crop_action_removed": _a3_action_snapshot(
                phase="CROP_REQUIRED",
                allowed_actions=(
                    contract.ACTION_SELECT_UNIT,
                    contract.ACTION_PREPARE_UNITS,
                ),
                unit_statuses=(
                    contract.UNIT_ACTIVE,
                    contract.UNIT_AVAILABLE,
                    contract.UNIT_COMPLETED,
                    contract.UNIT_CLOSED,
                ),
            ).to_dict(),
            "crop_new_workflow": _a3_action_snapshot(
                workflow_id="search_frontend_crop_workflow_new_12345678",
                phase="CROP_REQUIRED",
                allowed_actions=(
                    contract.ACTION_SUBMIT_CROP,
                    contract.ACTION_SELECT_UNIT,
                    contract.ACTION_PREPARE_UNITS,
                ),
                unit_statuses=(
                    contract.UNIT_ACTIVE,
                    contract.UNIT_AVAILABLE,
                    contract.UNIT_COMPLETED,
                    contract.UNIT_CLOSED,
                ),
            ).to_dict(),
            "a2_active": _a3_action_snapshot(
                phase="A2_ACTIVE",
                allowed_actions=(contract.ACTION_SELECT_UNIT,),
                unit_statuses=(contract.UNIT_ACTIVE, contract.UNIT_AVAILABLE),
            ).to_dict(),
            "inconsistent": _inconsistent_workflow_snapshot().to_dict(),
        }
        node_test = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const taskStateV1 = require('./tiku_agent/demo_web/task_state.js');
const fixtures = JSON.parse(process.argv[2]);
const source = fs.readFileSync('./tiku_agent/demo_web/demo.js', 'utf8');

function block(start, end) {
  const startIndex = source.indexOf(start);
  const endIndex = source.indexOf(end, startIndex);
  assert.notEqual(startIndex, -1, `missing source start: ${start}`);
  assert.notEqual(endIndex, -1, `missing source end: ${end}`);
  return source.slice(startIndex, endIndex);
}

const normalizeA3Source = block('function normalizeA3Snapshot', 'function openLightbox');
const recoverySource = block('function normalizeRecoveryActions', 'function normalizeA3Snapshot');
const workflowSource = block('function currentWorkflowActionTarget', 'function syncA2ActionButtons');
const createA3Source = block('function createA3UnitActions', 'async function submitFeedback');
const currentA3Source = block('function a3Current', 'function a3CropReviewMessage');
const identitySource = block('function workflowIdentityKey', 'function validA3Bounds');
const validA3BoundsSource = block('function validA3Bounds', 'function syncA3ActionButtons');
const syncA3Source = block('function syncA3ActionButtons', 'function syncA3Interface');
const syncA3InterfaceSource = block('function syncA3Interface', 'async function selectA3Unit');
const selectSource = block('async function selectA3Unit', 'function openA3Crop');
const closeCropSource = block('function finishCloseA3Crop', 'function renderA3Selection');
const popstateSource = block("window.addEventListener('popstate'", "document.addEventListener('dragenter'");
const clearA3StateSource = block('function clearA3WorkflowState()', 'function clearHistory(');
const historyExpirySource = block('function clearHistory(', 'function showSessionExpiredNotice()');
const submitSource = block('async function submitA3Crop', 'function renderA3SheetUnits');
const renderSheetSource = block('function renderA3SheetUnits', 'async function prepareA3Units');
const prepareSource = block('async function prepareA3Units', 'function openA3Sheet');

const createHarness = new Function('taskStateV1', `
  let taskStateContext = taskStateV1.createTaskStateModel();
  let sessionContext = {
    a3: null, a3WorkflowId: '', a3WorkflowRevision: 0,
  };
  let history = [];
  let historyLastActivityAt = 0;
  let historyExpiryTimer = null;
  let a3SourceUrl = '';
  let a3SourceWorkflowKey = '';
  let sessionResetRequired = false;
  let isBusy = false;
  let operationVersion = 0;
  let activeController = null;
  let activeAbortReason = '';
  let taskStateInvalidations = 0;
  let a3DismissedKey = '';
  let a3KnownWorkflowKey = '';
  let a3Bounds = { x: 0.1, y: 0.1, width: 0.5, height: 0.5 };
  let a3Pointer = null;
  let a3CropHistoryActive = false;
  let a3CropHistoryKey = '';
  let a3PendingClose = null;
  let a3DismissNextCrop = false;
  const a3LocalDrafts = new Map();
  const calls = [];
  const messages = [];
  const sheetOpens = [];
  const cropOpens = [];
  const historyPushes = [];
  const historyReplacements = [];
  let historyBackCalls = 0;
  let popstateHandler = null;
  const activeFailureNotices = new Set();
  let requestFailure = null;
  const a3PrepareSelection = new Set();
  const createdElements = [];
  class FakeElement {
    constructor(tagName) {
      this.tagName = String(tagName || '').toUpperCase();
      this.dataset = {};
      this.children = [];
      this.listeners = new Map();
      this.className = '';
      this.disabled = false;
      this.hidden = false;
      this.textContent = '';
      this.innerHTML = '';
      this.classList = {
        add: (...names) => { this.className += names.map((name) => ' ' + name).join(''); },
        remove: () => {},
        toggle: () => {},
        contains: (name) => this.className.split(/\\s+/).includes(name),
      };
      createdElements.push(this);
    }
    append(...children) { this.children.push(...children); }
    prepend(...children) { this.children.unshift(...children); }
    replaceChildren(...children) { this.children = [...children]; }
    addEventListener(type, listener) { this.listeners.set(type, listener); }
    invoke(type = 'click') { return this.listeners.get(type)?.({ target: this }); }
    setAttribute(name, value) { this[name] = String(value); }
    removeAttribute(name) { delete this[name]; }
    getAttribute(name) { return Object.hasOwn(this, name) ? this[name] : null; }
    focus() {}
    querySelector(selector) {
      if (selector === 'span') return this.children.find((child) => child.tagName === 'SPAN') || null;
      if (selector === '.a3-continue-crop') {
        return this.children.find((child) => child.classList?.contains('a3-continue-crop')) || null;
      }
      return null;
    }
    querySelectorAll(selector) {
      const matches = [];
      const visit = (child) => {
        if (!child || typeof child !== 'object') return;
        if (selector === 'img' && child.tagName === 'IMG') matches.push(child);
        (child.children || []).forEach(visit);
      };
      this.children.forEach(visit);
      return matches;
    }
  }
  const document = {
    body: { dataset: {} },
    createElement: (tagName) => new FakeElement(tagName),
    querySelectorAll: (selector) => {
      if (selector === '.a3-unit-choice[data-a3-unit-id]') {
        return createdElements.filter((item) => (
          item.classList.contains('a3-unit-choice') && Object.hasOwn(item.dataset, 'a3UnitId')
        ));
      }
      if (selector === '.a3-unit-actions') {
        return createdElements.filter((item) => item.classList.contains('a3-unit-actions'));
      }
      if (selector === '[data-workflow-action]') {
        return createdElements.filter((item) => Object.hasOwn(item.dataset, 'workflowAction'));
      }
      if (selector === '[data-a3-unit-navigation]') {
        return createdElements.filter((item) => Object.hasOwn(item.dataset, 'a3UnitNavigation'));
      }
      return [];
    },
  };
  const window = {
    history: {
      state: {},
      back: () => { historyBackCalls += 1; },
      pushState(state) {
        this.state = structuredClone(state);
        historyPushes.push(structuredClone(state));
      },
      replaceState(state) {
        this.state = structuredClone(state);
        historyReplacements.push(structuredClone(state));
      },
    },
    addEventListener(type, listener) {
      if (type === 'popstate') popstateHandler = listener;
    },
  };
  const a3Submit = new FakeElement('button');
  const a3Prepare = new FakeElement('button');
  const a3CropWorkspace = new FakeElement('section');
  a3CropWorkspace.hidden = true;
  const a3CropStatus = new FakeElement('div');
  const a3SheetUnits = new FakeElement('div');
  const a3SheetSubtitle = new FakeElement('p');
  const a3SheetOverlay = new FakeElement('button');
  const a3SheetOverlayImage = new FakeElement('img');
  const a3SourceImage = new FakeElement('img');
  const lightbox = new FakeElement('section');
  lightbox.hidden = true;
  const lightboxImage = new FakeElement('img');
  const a3SheetFooter = new FakeElement('footer');
  const a3SheetCount = new FakeElement('span');
  const a3SheetBackdrop = { hidden: true };
  const a3ExampleBackdrop = { hidden: true };
  const A3_TIMEOUT_MS = 180000;
  const HISTORY_TTL_MS = 2 * 60 * 60 * 1000;
  const HISTORY_KEY = 'history';
  const LEGACY_HISTORY_KEY = 'legacy-history';
  const A3_INLINE_ONLY_INTENTS = new Set(['inline']);
  const RECOVERY_ACTION_LABELS = {
    retry_request: '重试上一条', retry_connection: '重新连接',
  };

  ${recoverySource}
  ${normalizeA3Source}
  ${workflowSource}
  ${currentA3Source}
  ${identitySource}
  ${createA3Source}
  ${syncA3Source}

  function legacyFromSnapshot(raw, options = {}) {
    const selectedUnitId = Object.hasOwn(options, 'selectedUnitId')
      ? String(options.selectedUnitId || '')
      : String(raw.current_unit?.unit_id || '');
    const requestedIds = new Set(options.requestedIds || []);
    const manualIds = new Set(options.manualIds || []);
    return normalizeA3Snapshot({
      enabled: true,
      auto_crop_enabled: options.autoCropEnabled !== false,
      auto_prepare_all_enabled: true,
      auto_prepare_all_units: false,
      auto_crop_overlay_available: Boolean(options.overlayAvailable),
      phase: String(options.phase || raw.workflow.phase),
      page_finished: false,
      units: raw.units.map((unit) => ({
        unit_id: unit.unit_id,
        page_index: unit.page_index,
        display_label: unit.display_label,
        title_text: unit.display_label,
        completed: unit.status === 'COMPLETED',
        searched: unit.status === 'CLOSED',
        selected: unit.unit_id === selectedUnitId,
        requested: requestedIds.has(unit.unit_id) || unit.status === 'PREPARED',
        crop_available: unit.status === 'PREPARED',
        preparation_status: unit.status === 'PREPARED'
          ? 'ready'
          : manualIds.has(unit.unit_id) ? 'manual' : 'pending',
      })),
      selected_unit: {
        unit_id: selectedUnitId,
        display_label: selectedUnitId,
        context_text: '',
      },
      crop_draft: {},
      task_revision: Object.hasOwn(options, 'taskRevision')
        ? Number(options.taskRevision || 0)
        : Number(raw.workflow.task_revision || 0),
    });
  }

  function setSnapshot(raw, options = {}) {
    taskStateContext = taskStateV1.createTaskStateModel(raw);
    const a3 = legacyFromSnapshot(raw, options);
    sessionContext = {
      a3,
      a3WorkflowId: String(options.workflowId || raw.workflow.workflow_id || ''),
      a3WorkflowRevision: Object.hasOwn(options, 'workflowRevision')
        ? Number(options.workflowRevision || 0)
        : Number(raw.workflow.task_revision || 0),
    };
  }

  function setTaskState(raw) {
    taskStateContext = arguments.length
      ? taskStateV1.createTaskStateModel(raw)
      : taskStateV1.createTaskStateModel();
  }

  function setForgedTaskState(raw) {
    taskStateContext = raw;
  }

  function setBusy(value) { isBusy = Boolean(value); }
  function setStatus() {}
  function closeA3Sheet() { a3SheetBackdrop.hidden = true; }
  function closeA3Example() { a3ExampleBackdrop.hidden = true; }
  function closeLightbox() {
    lightbox.hidden = true;
    lightboxImage.removeAttribute('src');
  }
  function renderHistory() {}
  function showSessionExpiredNotice() {}
  function scheduleHistoryExpiry() {}
  function refreshHistoryActivityFromStorage() { return false; }
  function clearPendingUpload() {}
  function releaseAllObjectUrls() {}
  const localStorage = { removeItem() {} };
  function openA3Sheet(target) { sheetOpens.push(structuredClone(target)); }
  function openA3Crop(target, options = {}) {
    cropOpens.push({ target: structuredClone(target), force: Boolean(options.force) });
  }
  function renderA3Selection() {}
  function setResponseStatus() {}
  function protocolFields() { return {}; }
  function isPersistentImage() { return false; }
  function responseItem(data) { return { intent: data.intent || '', message: '' }; }
  function invalidateTaskStateContext() {
    taskStateInvalidations += 1;
    taskStateContext = taskStateV1.createTaskStateModel();
  }
  function addMessage(item) {
    messages.push(structuredClone(item));
    return { remove() {} };
  }
  ${validA3BoundsSource}
  ${closeCropSource}
  ${clearA3StateSource}
  ${historyExpirySource}
  async function requestStream(url, options) {
    if (requestFailure) {
      const failure = requestFailure;
      requestFailure = null;
      taskStateContext = taskStateV1.createTaskStateModel();
      throw failure;
    }
    calls.push({ url, body: JSON.parse(options.body) });
    return { intent: 'inline' };
  }

  ${selectSource}
  ${submitSource}
  ${renderSheetSource}
  ${syncA3InterfaceSource}
  ${prepareSource}
  ${popstateSource}

  return Object.freeze({
    setSnapshot,
    setTaskState,
    setForgedTaskState,
    model: () => taskStateContext,
    session: () => sessionContext,
    allowsWorkflow: taskStateAllowsWorkflowAction,
    allowsA3: taskStateAllowsA3Action,
    allowsNavigation: taskStateAllowsA3UnitNavigation,
    bind: bindWorkflowActionControl,
    createActions: createA3UnitActions,
    syncA3: syncA3ActionButtons,
    syncA3Interface,
    a3: a3Current,
    draftKey: a3DraftKey,
    workflowKey: () => workflowIdentityKey(currentA3WorkflowTarget()),
    select: selectA3Unit,
    prepare: prepareA3Units,
    submit: submitA3Crop,
    closeCrop: finishCloseA3Crop,
    requestCloseCrop: requestCloseA3Crop,
    restoreCropHistory: restoreA3CropHistoryState,
    showCrop: () => { a3CropWorkspace.hidden = false; },
    cropHidden: () => a3CropWorkspace.hidden,
    boundCropKey: () => a3DraftKey(a3Current(), workflowActionTargetFromControl(a3Submit)),
    setCropHistoryActive: (value) => {
      a3CropHistoryActive = Boolean(value);
      a3CropHistoryKey = a3CropHistoryActive
        ? a3DraftKey(a3Current(), workflowActionTargetFromControl(a3Submit))
        : '';
    },
    setHistoryState: (state) => { window.history.state = structuredClone(state); },
    firePopstate: (state = {}) => {
      window.history.state = structuredClone(state);
      popstateHandler?.({ state: structuredClone(state) });
    },
    cropHistory: () => ({
      active: a3CropHistoryActive,
      key: a3CropHistoryKey,
      dismissNext: a3DismissNextCrop,
      pending: a3PendingClose ? structuredClone(a3PendingClose) : null,
      backCalls: historyBackCalls,
      pushes: structuredClone(historyPushes),
      replacements: structuredClone(historyReplacements),
      state: structuredClone(window.history.state),
    }),
    seedExpiredUi: () => {
      history = [{ message: 'expired' }];
      historyLastActivityAt = Date.now() - HISTORY_TTL_MS - 1;
      a3CropWorkspace.hidden = false;
      a3SheetBackdrop.hidden = false;
      a3ExampleBackdrop.hidden = false;
      a3CropHistoryActive = true;
      a3PendingClose = { dismiss: true, key: 'pending-expired' };
      isBusy = true;
      activeController = {
        abort(reason) { activeAbortReason = String(reason || ''); },
      };
      window.history.state = { keep: 'value', a3Crop: true };
    },
    expireHistory: expireHistoryIfNeeded,
    expiredUi: () => ({
      cropHidden: a3CropWorkspace.hidden,
      sheetHidden: a3SheetBackdrop.hidden,
      exampleHidden: a3ExampleBackdrop.hidden,
      historyLength: history.length,
      historyLastActivityAt,
      operationVersion,
      isBusy,
      activeController: activeController !== null,
      activeAbortReason,
      taskStateInvalidations,
      session: structuredClone(sessionContext),
    }),
    renderSheet: renderA3SheetUnits,
    prepareControl: a3Prepare,
    submitControl: a3Submit,
    setBusyState: setBusy,
    sheetUnits: () => a3SheetUnits.children,
    mediaSources: () => ({
      overlay: String(a3SheetOverlayImage.src || ''),
      source: String(a3SourceImage.src || ''),
      lightbox: String(lightboxImage.src || ''),
      lightboxHidden: lightbox.hidden,
      sheetChildCount: a3SheetUnits.children.length,
      crops: createdElements
        .filter((item) => item.tagName === 'IMG' && String(item.src || '').startsWith('/api/a3/crop/'))
        .map((item) => String(item.src)),
    }),
    seedMediaSources: (lightboxUrl = '/api/a3/overlay?old=true') => {
      a3SheetOverlayImage.src = '/api/a3/overlay?old=true';
      a3SourceImage.src = '/api/upload/old.jpg';
      lightboxImage.src = lightboxUrl;
      lightbox.hidden = false;
    },
    sheetFooter: a3SheetFooter,
    prepareSelection: () => Array.from(a3PrepareSelection),
    seedTransient: ({ draftKey, dismissedKey, unitIds }) => {
      a3LocalDrafts.set(draftKey, { x: 0.2, y: 0.2, width: 0.4, height: 0.4 });
      a3DismissedKey = dismissedKey;
      unitIds.forEach((unitId) => a3PrepareSelection.add(unitId));
      a3Bounds = { x: 0.2, y: 0.2, width: 0.4, height: 0.4 };
    },
    transient: () => ({
      draftCount: a3LocalDrafts.size,
      dismissedKey: a3DismissedKey,
      knownWorkflowKey: a3KnownWorkflowKey,
      unitIds: Array.from(a3PrepareSelection),
      bounds: a3Bounds,
    }),
    setBounds: (value) => { a3Bounds = value; },
    calls: () => structuredClone(calls),
    clearCalls: () => { calls.length = 0; },
    messages: () => structuredClone(messages),
    clearMessages: () => { messages.length = 0; },
    failNextRequest: (failure) => { requestFailure = structuredClone(failure); },
    sheetOpens: () => structuredClone(sheetOpens),
    clearSheetOpens: () => { sheetOpens.length = 0; },
    cropOpens: () => structuredClone(cropOpens),
    clearCropOpens: () => { cropOpens.length = 0; },
    clearDismissal: () => {
      a3DismissedKey = '';
      a3DismissNextCrop = false;
    },
    clearSession: () => {
      sessionContext = { a3: null, a3WorkflowId: '', a3WorkflowRevision: 0 };
      taskStateContext = taskStateV1.createTaskStateModel();
      a3Submit.dataset = {};
    },
    retireExternal: retireSessionForExternalReset,
  });
`);

const harness = createHarness(taskStateV1);
const selectWorkflow = fixtures.select.workflow;
const selectBase = {
  workflowId: selectWorkflow.workflow_id,
  workflowRevision: selectWorkflow.task_revision,
};

const mediaHarness = createHarness(taskStateV1);
mediaHarness.setSnapshot(fixtures.select, { overlayAvailable: true });
mediaHarness.syncA3Interface();
let media = mediaHarness.mediaSources();
assert.equal(
  media.overlay,
  `/api/a3/overlay?workflow_id=${encodeURIComponent(selectBase.workflowId)}&task_revision=9`,
);
assert.equal(
  media.crops.at(-1),
  `/api/a3/crop/g1-u2?workflow_id=${encodeURIComponent(selectBase.workflowId)}&task_revision=9`,
);
mediaHarness.seedMediaSources();
const newMediaWorkflow = fixtures.select_new_workflow.workflow;
mediaHarness.setSnapshot(fixtures.select_new_workflow, { overlayAvailable: true });
mediaHarness.syncA3Interface();
media = mediaHarness.mediaSources();
assert.equal(
  media.overlay,
  `/api/a3/overlay?workflow_id=${encodeURIComponent(newMediaWorkflow.workflow_id)}&task_revision=9`,
);
assert.equal(media.source, '', 'workflow changes must clear the old source image element');
assert.equal(media.lightbox, '', 'workflow changes must clear an open A3 lightbox image');
assert.equal(media.lightboxHidden, true, 'workflow changes must close an open A3 lightbox');
assert.ok(media.sheetChildCount > 0, 'the sheet must be rebuilt only with current workflow units');
assert.equal(
  media.crops.at(-1),
  `/api/a3/crop/g1-u2?workflow_id=${encodeURIComponent(newMediaWorkflow.workflow_id)}&task_revision=9`,
);

harness.setSnapshot(fixtures.select);
assert.equal(harness.allowsWorkflow('select_unit', selectBase), false);
assert.equal(harness.allowsNavigation(selectBase), true);
assert.equal(harness.allowsA3('select_unit', { ...selectBase, unitId: 'g1-u1' }), true);
assert.equal(harness.allowsA3('select_unit', { ...selectBase, unitId: 'g1-u2' }), true);
for (const unitId of ['g1-u3', 'g1-u4', 'g1-unknown', '']) {
  assert.equal(harness.allowsA3('select_unit', { ...selectBase, unitId }), false);
}
assert.equal(harness.allowsA3('prepare_units', { ...selectBase, unitIds: ['g1-u1'] }), true);
for (const unitIds of [
  [], ['g1-u1', 'g1-u1'], ['g1-u2'], ['g1-u3'], ['g1-u4'], ['g1-unknown'],
]) {
  assert.equal(harness.allowsA3('prepare_units', { ...selectBase, unitIds }), false);
}
assert.equal(harness.allowsWorkflow('select_candidate', { ...selectBase, unitId: 'g1-u1' }), false);
assert.equal(taskStateV1.allowsChildAction(harness.model(), 'select_unit'), false);

(async () => {
  harness.setSnapshot(fixtures.select, { autoCropEnabled: false });
  const actionGroup = harness.createActions(harness.a3(), selectBase);
  const unitButtons = actionGroup.children.filter((item) => Object.hasOwn(item.dataset, 'a3UnitId'));
  assert.equal(unitButtons.length, 4);
  assert.deepEqual(unitButtons.map((button) => button.disabled), [false, false, true, true]);
  harness.setBusyState(true);
  harness.syncA3();
  assert.equal(unitButtons[0].disabled, true);
  harness.clearCalls();
  unitButtons[0].invoke('click');
  await new Promise(setImmediate);
  assert.equal(harness.calls().length, 0);
  harness.setBusyState(false);
  harness.syncA3();
  assert.equal(unitButtons[0].disabled, false);
  for (const button of unitButtons.slice(2)) {
    harness.clearCalls();
    button.invoke('click');
    await new Promise(setImmediate);
    assert.equal(harness.calls().length, 0);
  }
  harness.clearCalls();
  unitButtons[0].invoke('click');
  await new Promise(setImmediate);
  assert.deepEqual(harness.calls(), [{
    url: '/api/a3/select/stream',
    body: {
      workflow_id: selectBase.workflowId, unit_id: 'g1-u1', task_revision: 9,
    },
  }]);
  harness.clearCalls();
  unitButtons[1].invoke('click');
  await new Promise(setImmediate);
  assert.deepEqual(harness.calls(), [{
    url: '/api/a3/select/stream',
    body: {
      workflow_id: selectBase.workflowId, unit_id: 'g1-u2', task_revision: 9,
    },
  }]);

  harness.setTaskState(fixtures.select_next_stage_only);
  harness.syncA3();
  assert.equal(unitButtons[0].disabled, true);
  harness.clearCalls();
  unitButtons[0].invoke('click');
  await new Promise(setImmediate);
  assert.equal(harness.calls().length, 0);

  harness.setSnapshot(fixtures.select, { autoCropEnabled: false });
  const unboundGroup = harness.createActions(harness.a3(), {});
  const unboundButton = unboundGroup.children.find((item) => Object.hasOwn(item.dataset, 'a3UnitId'));
  assert.equal(unboundButton.disabled, true);
  harness.clearCalls();
  unboundButton.invoke('click');
  await new Promise(setImmediate);
  assert.equal(harness.calls().length, 0);

  harness.setSnapshot(fixtures.select, { autoCropEnabled: true });
  const navigationGroup = harness.createActions(harness.a3(), selectBase);
  const navigationButton = navigationGroup.children[0];
  assert.equal(navigationButton.disabled, false);
  harness.clearSheetOpens();
  navigationButton.invoke('click');
  assert.equal(harness.sheetOpens().length, 1);
  harness.setTaskState(fixtures.select_next_stage_only);
  harness.syncA3();
  assert.equal(navigationButton.disabled, true);
  assert.equal(navigationButton.hidden, true);
  harness.clearSheetOpens();
  navigationButton.invoke('click');
  assert.equal(harness.sheetOpens().length, 0);

  harness.setSnapshot(fixtures.select, {
    autoCropEnabled: true,
    requestedIds: ['g1-u1'],
    manualIds: ['g1-u1'],
  });
  harness.renderSheet();
  let sheetRows = harness.sheetUnits();
  assert.equal(sheetRows[0].tagName, 'BUTTON');
  assert.equal(sheetRows[0].disabled, false);
  assert.equal(sheetRows[0].children[1].children[1].textContent, '需要人工裁剪');
  assert.equal(sheetRows[1].tagName, 'BUTTON');
  assert.equal(harness.sheetFooter.hidden, true);
  harness.clearCalls();
  sheetRows[0].invoke('click');
  await new Promise(setImmediate);
  assert.deepEqual(harness.calls(), [{
    url: '/api/a3/select/stream',
    body: {
      workflow_id: selectBase.workflowId, unit_id: 'g1-u1', task_revision: 9,
    },
  }]);

  harness.setSnapshot(fixtures.select, {
    autoCropEnabled: true,
    requestedIds: ['g1-u4'],
    manualIds: ['g1-u4'],
  });
  harness.renderSheet();
  sheetRows = harness.sheetUnits();
  assert.equal(sheetRows[3].tagName, 'LABEL');
  assert.equal(sheetRows[3].classList.contains('is-closed'), true);
  const closedCheckbox = sheetRows[3].children[0];
  assert.equal(closedCheckbox.disabled, true);
  closedCheckbox.checked = true;
  closedCheckbox.invoke('change');
  assert.equal(closedCheckbox.checked, false);
  assert.deepEqual(harness.prepareSelection(), []);

  harness.setSnapshot(fixtures.select, { autoCropEnabled: true });
  harness.renderSheet();
  sheetRows = harness.sheetUnits();
  assert.equal(sheetRows[0].tagName, 'LABEL');
  const availableCheckbox = sheetRows[0].children[0];
  assert.equal(availableCheckbox.tagName, 'INPUT');
  assert.equal(availableCheckbox.disabled, false);
  assert.equal(harness.sheetFooter.hidden, false);
  availableCheckbox.checked = true;
  availableCheckbox.invoke('change');
  assert.deepEqual(harness.prepareSelection(), ['g1-u1']);
  assert.equal(harness.prepareControl.disabled, false);
  harness.setTaskState(fixtures.select_next_stage_only);
  harness.syncA3();
  assert.equal(availableCheckbox.disabled, true);
  assert.equal(harness.prepareControl.disabled, true);
  availableCheckbox.checked = true;
  availableCheckbox.invoke('change');
  assert.equal(availableCheckbox.checked, false);
  assert.deepEqual(harness.prepareSelection(), []);

  harness.setSnapshot(fixtures.a2_active, { autoCropEnabled: true });
  harness.renderSheet();
  sheetRows = harness.sheetUnits();
  assert.equal(sheetRows[1].tagName, 'BUTTON');
  assert.equal(sheetRows[1].disabled, false);
  assert.equal(harness.sheetFooter.hidden, true);

  harness.setSnapshot(fixtures.select);
  harness.clearCalls();
  await harness.select({ ...selectBase, unitId: 'g1-u1' });
  assert.deepEqual(harness.calls(), [{
    url: '/api/a3/select/stream',
    body: {
      workflow_id: selectBase.workflowId, unit_id: 'g1-u1', task_revision: 9,
    },
  }]);

  harness.clearCalls();
  harness.bind(harness.prepareControl, 'prepare_units', {
    ...selectBase, unitIds: ['g1-u1'],
  });
  await harness.prepare();
  assert.deepEqual(harness.calls(), [{
    url: '/api/a3/prepare/stream',
    body: {
      workflow_id: selectBase.workflowId, unit_ids: ['g1-u1'], task_revision: 9,
    },
  }]);

  harness.setSnapshot(fixtures.prepare_two);
  const prepareBase = {
    workflowId: fixtures.prepare_two.workflow.workflow_id,
    workflowRevision: fixtures.prepare_two.workflow.task_revision,
  };
  harness.clearCalls();
  harness.bind(harness.prepareControl, 'prepare_units', {
    ...prepareBase, unitIds: ['g1-u1', 'g1-u2'],
  });
  await harness.prepare();
  assert.deepEqual(harness.calls(), [{
    url: '/api/a3/prepare/stream',
    body: {
      workflow_id: prepareBase.workflowId,
      unit_ids: ['g1-u1', 'g1-u2'], task_revision: 9,
    },
  }]);
  for (const badTarget of [
    { ...prepareBase, workflowId: 'search_stale_workflow_12345678', unitIds: ['g1-u1'] },
    { ...prepareBase, workflowRevision: 8, unitIds: ['g1-u1'] },
    { ...prepareBase, unitIds: [] },
    { ...prepareBase, unitIds: ['g1-u1', 'g1-u1'] },
    { ...prepareBase, unitIds: ['g1-u3'] },
    { ...prepareBase, unitIds: ['g1-unknown'] },
  ]) {
    harness.clearCalls();
    harness.bind(harness.prepareControl, 'prepare_units', badTarget);
    await harness.prepare();
    assert.equal(harness.calls().length, 0);
  }
  harness.bind(harness.prepareControl, 'prepare_units', {
    ...prepareBase, unitIds: ['g1-u1'],
  });
  harness.setBusyState(true);
  harness.clearCalls();
  await harness.prepare();
  assert.equal(harness.calls().length, 0);
  harness.setBusyState(false);
  harness.setSnapshot(fixtures.prepare_action_removed);
  harness.bind(harness.prepareControl, 'prepare_units', {
    ...selectBase, unitIds: ['g1-u1'],
  });
  harness.clearCalls();
  await harness.prepare();
  assert.equal(harness.calls().length, 0);

  harness.setSnapshot(fixtures.crop);
  const cropBase = {
    workflowId: fixtures.crop.workflow.workflow_id,
    workflowRevision: fixtures.crop.workflow.task_revision,
  };
  harness.clearCalls();
  harness.bind(harness.submitControl, 'submit_crop', { ...cropBase, unitId: 'g1-u1' });
  await harness.submit();
  assert.deepEqual(harness.calls(), [{
    url: '/api/a3/crop/stream',
    body: {
      workflow_id: cropBase.workflowId,
      bounds: { x: 0.1, y: 0.1, width: 0.5, height: 0.5 },
      unit_id: 'g1-u1', task_revision: 9,
    },
  }]);

  for (const badTarget of [
    { ...cropBase, unitId: 'g1-u2' },
    { ...cropBase, workflowRevision: 8, unitId: 'g1-u1' },
    { ...cropBase, workflowId: 'search_stale_workflow_12345678', unitId: 'g1-u1' },
  ]) {
    harness.clearCalls();
    harness.bind(harness.submitControl, 'submit_crop', badTarget);
    await harness.submit();
    assert.equal(harness.calls().length, 0);
  }
  harness.setSnapshot(fixtures.crop);
  harness.bind(harness.submitControl, 'submit_crop', { ...cropBase, unitId: 'g1-u1' });
  harness.setBusyState(true);
  harness.clearCalls();
  await harness.submit();
  assert.equal(harness.calls().length, 0);
  harness.setBusyState(false);
  for (const invalidBounds of [
    null,
    { x: 0.9, y: 0.1, width: 0.2, height: 0.5 },
    { x: 0.1, y: 0.1, width: 0.01, height: 0.5 },
  ]) {
    harness.setBounds(invalidBounds);
    harness.clearCalls();
    await harness.submit();
    assert.equal(harness.calls().length, 0);
  }
  harness.setBounds({ x: 0.1, y: 0.1, width: 0.5, height: 0.5 });
  harness.setSnapshot(fixtures.crop_action_removed);
  harness.bind(harness.submitControl, 'submit_crop', { ...cropBase, unitId: 'g1-u1' });
  harness.clearCalls();
  await harness.submit();
  assert.equal(harness.calls().length, 0);

  harness.setSnapshot(fixtures.crop);
  harness.bind(harness.submitControl, 'submit_crop', { ...cropBase, unitId: 'g1-u1' });
  harness.setSnapshot(fixtures.crop_new_workflow);
  harness.clearCalls();
  await harness.submit();
  assert.equal(harness.calls().length, 0);

  harness.setSnapshot(fixtures.crop);
  harness.syncA3Interface();
  const oldDraftKey = harness.draftKey();
  const oldWorkflowKey = harness.workflowKey();
  assert.ok(oldDraftKey.includes(fixtures.crop.workflow.workflow_id));
  harness.seedTransient({
    draftKey: oldDraftKey,
    dismissedKey: oldDraftKey,
    unitIds: ['g1-u2'],
  });
  harness.setSnapshot(fixtures.crop_new_workflow);
  const newDraftKey = harness.draftKey();
  assert.notEqual(newDraftKey, oldDraftKey);
  assert.notEqual(harness.workflowKey(), oldWorkflowKey);
  harness.syncA3Interface();
  assert.deepEqual(harness.transient(), {
    draftCount: 0,
    dismissedKey: '',
    knownWorkflowKey: harness.workflowKey(),
    unitIds: [],
    bounds: null,
  });

  harness.setSnapshot(fixtures.crop);
  harness.syncA3Interface();
  harness.clearCropOpens();
  const dismissedCropTarget = { ...cropBase, unitId: 'g1-u1' };
  const dismissedCropKey = harness.draftKey(harness.a3(), dismissedCropTarget);
  assert.ok(dismissedCropKey.includes(fixtures.crop.workflow.workflow_id));
  harness.bind(harness.submitControl, 'submit_crop', dismissedCropTarget);
  harness.showCrop();
  harness.setTaskState();
  assert.equal(harness.draftKey(), '');
  harness.closeCrop({ dismiss: true });
  assert.equal(harness.cropHidden(), true);
  assert.equal(harness.transient().dismissedKey, dismissedCropKey);

  for (const failure of [
    { message: 'timeout', code: 'REQUEST_TIMEOUT', recoveryActions: ['retry_request'] },
    { message: 'network', code: 'NETWORK_UNAVAILABLE', recoveryActions: ['retry_request'] },
    { message: 'queue full', code: 'QUEUE_FULL', recoveryActions: [] },
  ]) {
    harness.setSnapshot(fixtures.select);
    harness.clearCalls();
    harness.clearMessages();
    harness.failNextRequest(failure);
    await harness.select({ ...selectBase, unitId: 'g1-u1' });
    assert.equal(harness.model().reason, 'MISSING');
    assert.equal(harness.calls().length, 0);
    assert.equal(harness.transient().dismissedKey, dismissedCropKey);

    harness.setSnapshot(fixtures.crop);
    harness.clearCropOpens();
    harness.syncA3Interface();
    assert.deepEqual(harness.cropOpens(), []);
    assert.equal(harness.transient().dismissedKey, dismissedCropKey);
  }

  harness.setSnapshot(fixtures.crop, { selectedUnitId: 'g1-u2' });
  assert.equal(harness.a3().selected_unit.unit_id, 'g1-u2');
  const oldBoundCropTarget = { ...cropBase, unitId: 'g1-u1' };
  const oldBoundCropKey = harness.draftKey(harness.a3(), oldBoundCropTarget);
  const mixedSelectedUnitKey = harness.draftKey(harness.a3(), cropBase);
  assert.deepEqual(
    JSON.parse(oldBoundCropKey),
    [cropBase.workflowId, cropBase.workflowRevision, 'g1-u1'],
  );
  assert.deepEqual(
    JSON.parse(mixedSelectedUnitKey),
    [cropBase.workflowId, cropBase.workflowRevision, 'g1-u2'],
  );
  harness.seedTransient({
    draftKey: mixedSelectedUnitKey,
    dismissedKey: mixedSelectedUnitKey,
    unitIds: [],
  });
  harness.bind(harness.submitControl, 'submit_crop', oldBoundCropTarget);
  harness.showCrop();
  harness.closeCrop({ dismiss: true });
  assert.equal(harness.transient().dismissedKey, oldBoundCropKey);
  assert.notEqual(harness.transient().dismissedKey, mixedSelectedUnitKey);

  harness.setSnapshot(fixtures.crop, { selectedUnitId: 'g1-u1' });
  const closeAKey = harness.draftKey(harness.a3(), oldBoundCropTarget);
  harness.seedTransient({ draftKey: closeAKey, dismissedKey: '', unitIds: [] });
  harness.bind(harness.submitControl, 'submit_crop', oldBoundCropTarget);
  harness.showCrop();
  harness.setCropHistoryActive(true);
  const beforeCloseA = harness.cropHistory();
  harness.requestCloseCrop({ dismiss: true });
  assert.equal(harness.cropHidden(), false);
  assert.deepEqual(harness.cropHistory().pending, { dismiss: true, key: closeAKey });
  assert.equal(harness.cropHistory().backCalls, beforeCloseA.backCalls + 1);

  harness.setSnapshot(fixtures.crop, { selectedUnitId: 'g1-u2' });
  const closeBTarget = { ...cropBase, unitId: 'g1-u2' };
  const closeBKey = harness.draftKey(harness.a3(), closeBTarget);
  harness.bind(harness.submitControl, 'submit_crop', closeBTarget);
  harness.showCrop();
  const pushesBeforeStalePop = harness.cropHistory().pushes.length;
  harness.firePopstate({ keep: 'previous' });
  assert.equal(harness.cropHidden(), false);
  assert.equal(harness.boundCropKey(), closeBKey);
  assert.equal(harness.transient().dismissedKey, '');
  assert.notEqual(harness.transient().dismissedKey, closeAKey);
  assert.notEqual(harness.transient().dismissedKey, closeBKey);
  assert.equal(harness.cropHistory().pending, null);
  assert.equal(harness.cropHistory().active, true);
  assert.equal(harness.cropHistory().pushes.length, pushesBeforeStalePop + 1);
  assert.deepEqual(harness.cropHistory().state.a3Crop, {
    workflowId: closeBTarget.workflowId,
    workflowRevision: closeBTarget.workflowRevision,
    unitId: closeBTarget.unitId,
  });

  harness.setCropHistoryActive(false);
  harness.closeCrop({ dismiss: false });
  harness.setSnapshot(fixtures.crop, { selectedUnitId: 'g1-u1' });
  harness.seedTransient({ draftKey: closeAKey, dismissedKey: '', unitIds: [] });
  harness.bind(harness.submitControl, 'submit_crop', oldBoundCropTarget);
  harness.showCrop();
  harness.setCropHistoryActive(true);
  harness.firePopstate({ keep: 'previous' });
  assert.equal(harness.cropHidden(), true);
  assert.equal(harness.transient().dismissedKey, closeAKey);
  assert.equal(harness.cropHistory().active, false);
  assert.equal(harness.cropHistory().pending, null);

  const restoreTarget = { ...cropBase, unitId: 'g1-u1' };
  const restoreKey = harness.draftKey(harness.a3(), restoreTarget);
  harness.bind(harness.submitControl, 'submit_crop', restoreTarget);
  harness.clearSession();
  harness.setHistoryState({
    keep: 'refresh-before-session',
    a3Crop: {
      workflowId: restoreTarget.workflowId,
      workflowRevision: restoreTarget.workflowRevision,
      unitId: restoreTarget.unitId,
    },
  });
  harness.restoreCropHistory();
  assert.equal(harness.cropHistory().key, restoreKey);
  harness.firePopstate({ keep: 'back-before-session' });
  assert.equal(harness.cropHistory().active, false);
  assert.equal(harness.transient().dismissedKey, restoreKey);
  harness.setSnapshot(fixtures.crop, { selectedUnitId: 'g1-u1' });
  harness.clearCropOpens();
  harness.syncA3Interface();
  assert.deepEqual(harness.cropOpens(), []);
  harness.clearDismissal();

  const freshBackHarness = createHarness(taskStateV1);
  const freshRestoreTarget = { ...cropBase, unitId: 'g1-u1' };
  const freshRestoreKey = JSON.stringify([
    freshRestoreTarget.workflowId,
    freshRestoreTarget.workflowRevision,
    freshRestoreTarget.unitId,
  ]);
  freshBackHarness.setHistoryState({
    keep: 'fresh-refresh-before-session',
    a3Crop: {
      workflowId: freshRestoreTarget.workflowId,
      workflowRevision: freshRestoreTarget.workflowRevision,
      unitId: freshRestoreTarget.unitId,
    },
  });
  freshBackHarness.restoreCropHistory();
  freshBackHarness.firePopstate({ keep: 'fresh-back-before-session' });
  assert.equal(freshBackHarness.transient().knownWorkflowKey, '');
  assert.equal(freshBackHarness.transient().dismissedKey, freshRestoreKey);
  freshBackHarness.setSnapshot(fixtures.crop, { selectedUnitId: 'g1-u1' });
  freshBackHarness.clearCropOpens();
  freshBackHarness.syncA3Interface();
  assert.deepEqual(freshBackHarness.cropOpens(), []);
  assert.equal(freshBackHarness.transient().dismissedKey, freshRestoreKey);

  harness.clearSession();
  harness.setHistoryState({ keep: 'legacy-refresh', a3Crop: true });
  harness.restoreCropHistory();
  harness.firePopstate({ keep: 'legacy-back-before-session' });
  assert.equal(harness.cropHistory().dismissNext, true);
  harness.setSnapshot(fixtures.crop, { selectedUnitId: 'g1-u1' });
  harness.clearCropOpens();
  harness.syncA3Interface();
  assert.deepEqual(harness.cropOpens(), []);
  assert.equal(harness.cropHistory().dismissNext, false);
  assert.equal(harness.transient().dismissedKey, restoreKey);
  harness.clearDismissal();

  const replacementsBeforeRefreshRestore = harness.cropHistory().replacements.length;
  harness.setHistoryState({ keep: 'refresh', a3Crop: true });
  harness.restoreCropHistory();
  assert.equal(harness.cropHistory().active, true);
  assert.equal(harness.cropHistory().replacements.length, replacementsBeforeRefreshRestore);
  harness.setCropHistoryActive(false);
  harness.clearCropOpens();
  harness.firePopstate({ keep: 'forward', a3Crop: true });
  assert.equal(harness.cropHistory().active, true);
  assert.equal(harness.cropOpens().length, 1);
  assert.deepEqual(harness.cropOpens()[0], {
    target: oldBoundCropTarget,
    force: true,
  });
  harness.setCropHistoryActive(false);
  harness.setHistoryState({ keep: 'after-forward' });

  harness.setSnapshot(fixtures.select);
  const staleTarget = { ...selectBase, unitId: 'g1-u1' };
  harness.setSnapshot(fixtures.select_new_workflow);
  harness.clearCalls();
  await harness.select(staleTarget);
  assert.equal(harness.calls().length, 0);

  harness.setSnapshot(fixtures.select, { phase: 'CROP_REQUIRED' });
  harness.clearCalls();
  await harness.select(staleTarget);
  assert.equal(harness.calls().length, 0);

  harness.setSnapshot(fixtures.crop, { selectedUnitId: 'g1-u2' });
  harness.bind(harness.submitControl, 'submit_crop', { ...cropBase, unitId: 'g1-u1' });
  harness.clearCalls();
  await harness.submit();
  assert.equal(harness.calls().length, 0);

  harness.setSnapshot(fixtures.select_next_stage_only);
  assert.equal(harness.model().workflow_next_stage, 'SELECT_UNIT');
  assert.equal(harness.allowsNavigation(selectBase), false);
  harness.clearCalls();
  await harness.select(staleTarget);
  assert.equal(harness.calls().length, 0);

  const deniedModels = [
    undefined,
    { schema_version: 1 },
    { ...structuredClone(fixtures.select), schema_version: 2 },
    fixtures.inconsistent,
  ];
  for (const raw of deniedModels) {
    harness.setSnapshot(fixtures.select);
    const favorableSelectSession = harness.session();
    harness.bind(harness.prepareControl, 'prepare_units', {
      ...selectBase, unitIds: ['g1-u1'],
    });
    if (typeof raw === 'undefined') harness.setTaskState();
    else harness.setTaskState(raw);
    assert.strictEqual(harness.session(), favorableSelectSession);
    assert.equal(harness.allowsA3('select_unit', staleTarget), false);
    harness.clearCalls();
    await harness.select(staleTarget);
    await harness.prepare();
    assert.equal(harness.calls().length, 0);

    harness.setSnapshot(fixtures.crop);
    const favorableCropSession = harness.session();
    harness.bind(harness.submitControl, 'submit_crop', { ...cropBase, unitId: 'g1-u1' });
    if (typeof raw === 'undefined') harness.setTaskState();
    else harness.setTaskState(raw);
    assert.strictEqual(harness.session(), favorableCropSession);
    harness.clearCalls();
    await harness.submit();
    assert.equal(harness.calls().length, 0);
  }

  harness.setSnapshot(fixtures.select);
  harness.bind(harness.prepareControl, 'prepare_units', {
    ...selectBase, unitIds: ['g1-u1'],
  });
  harness.setForgedTaskState({
    available: true,
    consistent: true,
    actions_enabled: true,
    workflow_actions: ['select_unit', 'prepare_units', 'submit_crop'],
    snapshot: fixtures.select,
  });
  assert.equal(harness.allowsA3('select_unit', staleTarget), false);
  harness.clearCalls();
  await harness.select(staleTarget);
  await harness.prepare();
  assert.equal(harness.calls().length, 0);
  harness.setSnapshot(fixtures.crop);
  harness.bind(harness.submitControl, 'submit_crop', { ...cropBase, unitId: 'g1-u1' });
  harness.setForgedTaskState({
    available: true,
    consistent: true,
    actions_enabled: true,
    workflow_actions: ['select_unit', 'prepare_units', 'submit_crop'],
    snapshot: fixtures.crop,
  });
  harness.clearCalls();
  await harness.submit();
  assert.equal(harness.calls().length, 0);

  const transportFailures = [
    { message: 'timeout', code: 'REQUEST_TIMEOUT', recoveryActions: ['retry_request'] },
    { message: 'network', code: 'NETWORK_UNAVAILABLE', recoveryActions: ['retry_request'] },
    { message: 'queue full', code: 'QUEUE_FULL', recoveryActions: [] },
    { message: 'queue timeout', code: 'QUEUE_TIMEOUT', recoveryActions: [] },
  ];
  for (const failure of transportFailures) {
    harness.setSnapshot(fixtures.select);
    harness.clearCalls();
    harness.clearMessages();
    harness.failNextRequest(failure);
    await harness.select(staleTarget);
    assert.equal(harness.calls().length, 0);
    assert.equal(harness.messages().at(-1).recoveryActions.includes('retry_connection'), true);

    harness.setSnapshot(fixtures.select);
    harness.bind(harness.prepareControl, 'prepare_units', {
      ...selectBase, unitIds: ['g1-u1'],
    });
    harness.clearCalls();
    harness.clearMessages();
    harness.failNextRequest(failure);
    await harness.prepare();
    assert.equal(harness.calls().length, 0);
    assert.equal(harness.messages().at(-1).recoveryActions.includes('retry_connection'), true);

    harness.setSnapshot(fixtures.crop);
    harness.setBounds({ x: 0.1, y: 0.1, width: 0.5, height: 0.5 });
    harness.bind(harness.submitControl, 'submit_crop', { ...cropBase, unitId: 'g1-u1' });
    harness.clearCalls();
    harness.clearMessages();
    harness.failNextRequest(failure);
    await harness.submit();
    assert.equal(harness.calls().length, 0);
    assert.equal(harness.messages().at(-1).recoveryActions.includes('retry_connection'), true);
  }

  harness.setSnapshot(fixtures.a2_active);
  const activeBase = {
    workflowId: fixtures.a2_active.workflow.workflow_id,
    workflowRevision: fixtures.a2_active.workflow.task_revision,
  };
  assert.equal(harness.allowsA3('select_unit', { ...activeBase, unitId: 'g1-u1' }), false);
  assert.equal(harness.allowsA3('select_unit', { ...activeBase, unitId: 'g1-u2' }), true);
  assert.equal(harness.allowsA3('prepare_units', { ...activeBase, unitIds: ['g1-u2'] }), false);

  const replacementsBeforeExpiry = harness.cropHistory().replacements.length;
  const beforeExpiry = harness.expiredUi();
  harness.seedExpiredUi();
  assert.equal(harness.cropHistory().active, true);
  assert.notEqual(harness.cropHistory().pending, null);
  assert.equal(harness.expireHistory(), true);
  const expiredUi = harness.expiredUi();
  assert.equal(expiredUi.cropHidden, true);
  assert.equal(expiredUi.sheetHidden, true);
  assert.equal(expiredUi.exampleHidden, true);
  assert.equal(expiredUi.historyLength, 0);
  assert.equal(expiredUi.historyLastActivityAt, 0);
  assert.equal(expiredUi.operationVersion, beforeExpiry.operationVersion + 1);
  assert.equal(expiredUi.isBusy, false);
  assert.equal(expiredUi.activeController, false);
  assert.equal(expiredUi.activeAbortReason, 'history-expired');
  assert.equal(expiredUi.taskStateInvalidations, beforeExpiry.taskStateInvalidations + 1);
  assert.equal(harness.model().reason, 'MISSING');
  assert.equal(expiredUi.session.session_valid, false);
  assert.equal(expiredUi.session.phase, 'IDLE');
  assert.equal(expiredUi.session.a3WorkflowId, '');
  assert.equal(expiredUi.session.a3WorkflowRevision, 0);
  assert.equal(harness.cropHistory().active, false);
  assert.equal(harness.cropHistory().pending, null);
  assert.deepEqual(harness.cropHistory().state, { keep: 'value' });
  assert.equal(harness.cropHistory().replacements.length, replacementsBeforeExpiry + 1);
  assert.deepEqual(harness.cropHistory().replacements.at(-1), { keep: 'value' });
  assert.deepEqual(harness.transient(), {
    draftCount: 0,
    dismissedKey: '',
    knownWorkflowKey: '',
    unitIds: [],
    bounds: null,
  });

  const externalResetHarness = createHarness(taskStateV1);
  externalResetHarness.setSnapshot(fixtures.crop, { selectedUnitId: 'g1-u1' });
  externalResetHarness.seedExpiredUi();
  const beforeExternalReset = externalResetHarness.expiredUi();
  externalResetHarness.retireExternal();
  const afterExternalReset = externalResetHarness.expiredUi();
  assert.equal(afterExternalReset.operationVersion, beforeExternalReset.operationVersion + 1);
  assert.equal(afterExternalReset.activeAbortReason, 'session-reset');
  assert.equal(afterExternalReset.activeController, false);
  assert.equal(afterExternalReset.isBusy, false);
  assert.equal(afterExternalReset.cropHidden, true);
  assert.equal(afterExternalReset.historyLength, 0);
  assert.equal(afterExternalReset.session.session_valid, false);
  assert.equal(afterExternalReset.session.a3, null);
  assert.equal(externalResetHarness.model().reason, 'MISSING');

  for (const lightboxUrl of ['/api/upload/old-lightbox.jpg', '/api/media/old-lightbox.jpg']) {
    const resetMediaHarness = createHarness(taskStateV1);
    resetMediaHarness.setSnapshot(fixtures.crop, { selectedUnitId: 'g1-u1' });
    resetMediaHarness.syncA3Interface();
    resetMediaHarness.seedExpiredUi();
    resetMediaHarness.seedMediaSources(lightboxUrl);
    assert.equal(resetMediaHarness.mediaSources().lightboxHidden, false);
    resetMediaHarness.retireExternal();
    const resetMedia = resetMediaHarness.mediaSources();
    assert.equal(resetMedia.overlay, '');
    assert.equal(resetMedia.source, '');
    assert.equal(resetMedia.lightbox, '');
    assert.equal(resetMedia.lightboxHidden, true);
    assert.equal(resetMediaHarness.expiredUi().cropHidden, true);
    assert.equal(resetMediaHarness.expiredUi().sheetHidden, true);
    assert.equal(resetMediaHarness.cropHistory().active, false);
    assert.deepEqual(resetMediaHarness.cropHistory().state, { keep: 'value' });
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
        result = subprocess.run(
            [shutil.which("node"), "-", json.dumps(fixtures, ensure_ascii=False)],
            cwd=ROOT,
            input=node_test,
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_task_state_module_has_valid_syntax(self):
        for script in (TASK_STATE_SCRIPT, ROOT / "tiku_agent" / "demo_web" / "demo.js"):
            with self.subTest(script=script.name):
                result = subprocess.run(
                    [shutil.which("node"), "--check", str(script)],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
