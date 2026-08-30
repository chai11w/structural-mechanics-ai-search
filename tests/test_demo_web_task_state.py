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

        task_state_asset = 'src="/assets/task_state.js?v=20260830-task-state-3-4-1"'
        demo_asset = 'src="/assets/demo.js?v=20260830-task-state-3-4-1"'
        self.assertIn(task_state_asset, page)
        self.assertIn(demo_asset, page)
        self.assertLess(page.index(task_state_asset), page.index(demo_asset))
        self.assertIn("const taskStateV1 = globalThis.TikuTaskStateV1", demo)
        self.assertIn("let taskStateContext = taskStateV1.createTaskStateModel()", demo)

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
