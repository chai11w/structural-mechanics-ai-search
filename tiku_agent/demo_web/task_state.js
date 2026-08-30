(function installTaskStateV1(root) {
  'use strict';

  const ROOT_KEYS = Object.freeze([
    'schema_version', 'workflow', 'active_child_task', 'current_unit', 'units', 'consistency',
  ]);
  const WORKFLOW_KEYS = Object.freeze([
    'exists', 'workflow_id', 'kind', 'route', 'task_revision', 'phase', 'status',
    'completed_steps', 'allowed_actions', 'next_stage',
  ]);
  const CHILD_KEYS = Object.freeze([
    'task_id', 'kind', 'unit_id', 'task_revision', 'phase', 'status', 'completed_steps',
    'allowed_actions', 'next_stage', 'chapter', 'candidate_count', 'candidate_generation',
  ]);
  const UNIT_KEYS = Object.freeze(['unit_id', 'page_index', 'display_label', 'status']);
  const CONSISTENCY_KEYS = Object.freeze(['status', 'codes']);

  const WORKFLOW_STEP_ORDER = Object.freeze([
    'IMAGE_ACCEPTED', 'ROUTE_DECIDED', 'PAGE_UNDERSTOOD', 'UNIT_CATALOG_READY',
    'UNIT_SELECTED', 'CHILD_TASK_STARTED', 'WORKFLOW_COMPLETED',
  ]);
  const CHILD_STEP_ORDER = Object.freeze([
    'QUESTION_ACCEPTED', 'QUESTION_ANALYZED', 'CHAPTER_RESOLVED', 'SEARCH_ROUTE_SELECTED',
    'SEARCH_COMPLETED', 'CANDIDATES_READY', 'ANSWER_PREPARED',
  ]);
  const UNIT_STATUSES = Object.freeze([
    'AVAILABLE', 'PREPARED', 'ACTIVE', 'COMPLETED', 'CLOSED',
  ]);
  const WORKFLOW_KINDS = Object.freeze(['NONE', 'IMAGE_SEARCH']);
  const WORKFLOW_ROUTES = Object.freeze(['NONE', 'PENDING', 'A1', 'A2', 'A3']);
  const CONSISTENCY_CODES = Object.freeze([
    'WORKFLOW_ID_MISSING',
    'CHILD_TASK_ID_MISSING',
    'ACTIVE_CHILD_TASK_MISSING',
    'ACTIVE_UNIT_MISSING',
    'ACTIVE_UNIT_CLOSED',
    'UNIT_STATE_OVERLAP',
    'DUPLICATE_UNIT_ID',
    'UNKNOWN_WORKFLOW_PHASE',
    'UNKNOWN_CHILD_PHASE',
    'PARENT_CHILD_ID_COLLISION',
    'ORPHAN_CHILD_TASK',
    'WORKFLOW_STATE_UNREADABLE',
    'CHILD_STATE_UNREADABLE',
    'WORKFLOW_ROUTE_PHASE_MISMATCH',
    'WORKFLOW_ROUTE_UNIT_MISMATCH',
    'WORKFLOW_COMPLETE_UNIT_OPEN',
    'CHILD_CANDIDATE_GENERATION_MISMATCH',
  ]);

  function phase(status, nextStage, actions = []) {
    return Object.freeze({
      status,
      nextStage,
      actions: Object.freeze(actions),
    });
  }

  const WORKFLOW_PHASES = Object.freeze({
    IDLE: phase('IDLE', 'UPLOAD_IMAGE', ['upload_image', 'reset_session']),
    UNDERSTANDING_PAGE: phase('RUNNING', 'SYSTEM_CONTINUE'),
    AUTO_GROUNDING_PAGE: phase('RUNNING', 'SYSTEM_CONTINUE'),
    AUTO_VALIDATING_CROPS: phase('RUNNING', 'SYSTEM_CONTINUE'),
    WAIT_UNIT_SELECTION: phase('WAITING_USER', 'SELECT_UNIT', [
      'select_unit', 'prepare_units', 'finish_page', 'upload_image', 'reset_session',
    ]),
    CROP_REQUIRED: phase('WAITING_USER', 'SUBMIT_CROP', [
      'submit_crop', 'select_unit', 'prepare_units', 'cancel_current_unit', 'finish_page',
      'upload_image', 'reset_session',
    ]),
    VERIFYING_CROP: phase('RUNNING', 'SYSTEM_CONTINUE'),
    A2_ACTIVE: phase('RUNNING', 'FOLLOW_CHILD_TASK', [
      'select_unit', 'cancel_current_unit', 'finish_page', 'upload_image', 'reset_session',
    ]),
    COMPLETE: phase('COMPLETED', 'DONE', ['upload_image', 'reset_session']),
    ERROR: phase('FAILED', 'RETRY', ['retry_current_stage', 'upload_image', 'reset_session']),
    UNKNOWN: phase('INCONSISTENT', 'RETRY'),
  });

  const CHILD_PHASES = Object.freeze({
    IDLE: phase('IDLE', 'UPLOAD_IMAGE'),
    PROCESSING: phase('RUNNING', 'SYSTEM_CONTINUE'),
    WAIT_CHAPTER: phase('WAITING_USER', 'SET_CHAPTER', [
      'set_chapter', 'global_search', 'select_question', 'explain_failure', 'cancel',
    ]),
    WAIT_QUESTION_CHOICE: phase('WAITING_USER', 'SELECT_QUESTION', [
      'select_question', 'explain_failure', 'cancel',
    ]),
    WAIT_CANDIDATE_CHOICE: phase('WAITING_USER', 'SELECT_CANDIDATE', [
      'set_chapter', 'select_question', 'select_candidate', 'reject_candidates',
      'show_candidates', 'explain_failure', 'cancel',
    ]),
    READY_TO_ROUTE: phase('RUNNING', 'SYSTEM_CONTINUE'),
    READY_FOR_SEARCH: phase('RUNNING', 'SYSTEM_CONTINUE'),
    ANSWERED: phase('COMPLETED', 'DONE', [
      'set_chapter', 'select_question', 'select_candidate', 'reject_candidates',
      'show_candidates', 'report_answer_mismatch', 'resend_answer', 'explain_failure', 'cancel',
    ]),
    CANCELLED: phase('CANCELLED', 'DONE'),
    ERROR: phase('FAILED', 'RETRY', [
      'set_chapter', 'select_question', 'select_candidate', 'explain_failure',
      'retry_search', 'cancel',
    ]),
    NO_MATCH: phase('NO_MATCH', 'DONE', [
      'set_chapter', 'select_question', 'explain_failure', 'cancel',
    ]),
    UNKNOWN: phase('INCONSISTENT', 'RETRY'),
  });

  const WORKFLOW_ROUTE_PHASES = Object.freeze({
    NONE: Object.freeze(['IDLE']),
    PENDING: Object.freeze(['UNDERSTANDING_PAGE', 'ERROR']),
    A1: Object.freeze(['COMPLETE']),
    A2: Object.freeze(['A2_ACTIVE']),
    A3: Object.freeze([
      'UNDERSTANDING_PAGE', 'AUTO_GROUNDING_PAGE', 'AUTO_VALIDATING_CROPS',
      'WAIT_UNIT_SELECTION', 'CROP_REQUIRED', 'VERIFYING_CROP', 'A2_ACTIVE',
      'COMPLETE', 'ERROR',
    ]),
  });
  const CURRENT_UNIT_PHASES = Object.freeze(['CROP_REQUIRED', 'VERIFYING_CROP', 'A2_ACTIVE']);

  const WORKFLOW_STEPS_BY_ROUTE = Object.freeze({
    NONE: Object.freeze([]),
    PENDING: Object.freeze(['IMAGE_ACCEPTED']),
    A1: Object.freeze(['IMAGE_ACCEPTED', 'ROUTE_DECIDED', 'WORKFLOW_COMPLETED']),
    A2: Object.freeze(['IMAGE_ACCEPTED', 'ROUTE_DECIDED', 'CHILD_TASK_STARTED']),
    A3: WORKFLOW_STEP_ORDER,
  });
  const WORKFLOW_STEPS_BY_PHASE = Object.freeze({
    IDLE: Object.freeze([]),
    UNDERSTANDING_PAGE: Object.freeze(['IMAGE_ACCEPTED', 'ROUTE_DECIDED']),
    AUTO_GROUNDING_PAGE: Object.freeze([
      'IMAGE_ACCEPTED', 'ROUTE_DECIDED', 'PAGE_UNDERSTOOD', 'UNIT_CATALOG_READY',
    ]),
    AUTO_VALIDATING_CROPS: Object.freeze([
      'IMAGE_ACCEPTED', 'ROUTE_DECIDED', 'PAGE_UNDERSTOOD', 'UNIT_CATALOG_READY',
    ]),
    WAIT_UNIT_SELECTION: Object.freeze([
      'IMAGE_ACCEPTED', 'ROUTE_DECIDED', 'PAGE_UNDERSTOOD', 'UNIT_CATALOG_READY',
      'UNIT_SELECTED', 'CHILD_TASK_STARTED',
    ]),
    CROP_REQUIRED: Object.freeze([
      'IMAGE_ACCEPTED', 'ROUTE_DECIDED', 'PAGE_UNDERSTOOD', 'UNIT_CATALOG_READY',
      'UNIT_SELECTED', 'CHILD_TASK_STARTED',
    ]),
    VERIFYING_CROP: Object.freeze([
      'IMAGE_ACCEPTED', 'ROUTE_DECIDED', 'PAGE_UNDERSTOOD', 'UNIT_CATALOG_READY',
      'UNIT_SELECTED', 'CHILD_TASK_STARTED',
    ]),
    A2_ACTIVE: Object.freeze([
      'IMAGE_ACCEPTED', 'ROUTE_DECIDED', 'PAGE_UNDERSTOOD', 'UNIT_CATALOG_READY',
      'UNIT_SELECTED', 'CHILD_TASK_STARTED',
    ]),
    COMPLETE: WORKFLOW_STEP_ORDER,
    ERROR: Object.freeze([
      'IMAGE_ACCEPTED', 'ROUTE_DECIDED', 'PAGE_UNDERSTOOD', 'UNIT_CATALOG_READY',
      'UNIT_SELECTED', 'CHILD_TASK_STARTED',
    ]),
    UNKNOWN: Object.freeze([]),
  });
  const CHILD_STEPS_BY_PHASE = Object.freeze({
    IDLE: Object.freeze([]),
    PROCESSING: Object.freeze(['QUESTION_ACCEPTED']),
    WAIT_CHAPTER: Object.freeze(['QUESTION_ACCEPTED', 'QUESTION_ANALYZED']),
    WAIT_QUESTION_CHOICE: Object.freeze(['QUESTION_ACCEPTED', 'QUESTION_ANALYZED']),
    READY_TO_ROUTE: Object.freeze(['QUESTION_ACCEPTED', 'QUESTION_ANALYZED', 'CHAPTER_RESOLVED']),
    READY_FOR_SEARCH: Object.freeze([
      'QUESTION_ACCEPTED', 'QUESTION_ANALYZED', 'CHAPTER_RESOLVED', 'SEARCH_ROUTE_SELECTED',
    ]),
    WAIT_CANDIDATE_CHOICE: Object.freeze([
      'QUESTION_ACCEPTED', 'QUESTION_ANALYZED', 'CHAPTER_RESOLVED', 'SEARCH_ROUTE_SELECTED',
      'SEARCH_COMPLETED', 'CANDIDATES_READY',
    ]),
    ANSWERED: CHILD_STEP_ORDER,
    CANCELLED: CHILD_STEP_ORDER,
    ERROR: Object.freeze([
      'QUESTION_ACCEPTED', 'QUESTION_ANALYZED', 'CHAPTER_RESOLVED', 'SEARCH_ROUTE_SELECTED',
      'SEARCH_COMPLETED', 'CANDIDATES_READY',
    ]),
    NO_MATCH: Object.freeze([
      'QUESTION_ACCEPTED', 'QUESTION_ANALYZED', 'CHAPTER_RESOLVED', 'SEARCH_ROUTE_SELECTED',
      'SEARCH_COMPLETED',
    ]),
    UNKNOWN: Object.freeze([]),
  });

  const ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$/;
  const CANDIDATE_GENERATION_PATTERN = /^([1-9][0-9]{0,6}):([1-9][0-9]{0,6})$/;
  const CONTROL_PATTERN = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/;
  const EMPTY_ACTIONS = Object.freeze([]);
  const MODEL_INSTANCES = new WeakSet();

  class TaskStateValidationError extends Error {
    constructor(code) {
      super(`Task state validation failed (${code}).`);
      this.name = 'TaskStateValidationError';
      this.code = code;
    }
  }

  function fail(code) {
    throw new TaskStateValidationError(code);
  }

  function isPlainObject(value) {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  function exactObject(value, keys, code) {
    if (!isPlainObject(value)) fail(code);
    const ownKeys = Reflect.ownKeys(value);
    if (ownKeys.length !== keys.length || keys.some((key) => !Object.hasOwn(value, key))) fail(code);
    const result = {};
    keys.forEach((key) => {
      const descriptor = Object.getOwnPropertyDescriptor(value, key);
      if (!descriptor || !Object.hasOwn(descriptor, 'value') || !descriptor.enumerable) fail(code);
      result[key] = descriptor.value;
    });
    return result;
  }

  function exactArray(value, code, maxLength = 10000) {
    if (
      !Array.isArray(value)
      || Object.getPrototypeOf(value) !== Array.prototype
    ) fail(code);
    const lengthDescriptor = Object.getOwnPropertyDescriptor(value, 'length');
    if (
      !lengthDescriptor
      || !Object.hasOwn(lengthDescriptor, 'value')
      || !Number.isInteger(lengthDescriptor.value)
      || lengthDescriptor.value < 0
      || lengthDescriptor.value > maxLength
    ) fail(code);
    const length = lengthDescriptor.value;
    const ownKeys = Reflect.ownKeys(value);
    if (ownKeys.length !== length + 1) fail(code);
    const result = new Array(length);
    for (let index = 0; index < length; index += 1) {
      const descriptor = Object.getOwnPropertyDescriptor(value, index);
      if (!descriptor || !Object.hasOwn(descriptor, 'value') || !descriptor.enumerable) fail(code);
      result[index] = descriptor.value;
    }
    return result;
  }

  // The server public mapper owns sensitive-text screening. The browser only
  // repeats transport-stable checks whose Unicode semantics match Python.
  function publicText(value, maxChars, code) {
    let codePointCount = 0;
    if (typeof value === 'string') {
      for (const _character of value) {
        codePointCount += 1;
        if (codePointCount > maxChars) break;
      }
    }
    if (
      typeof value !== 'string'
      || codePointCount > maxChars
      || CONTROL_PATTERN.test(value)
    ) fail(code);
    return value;
  }

  function optionalId(value, code) {
    publicText(value, 128, code);
    if (value && !ID_PATTERN.test(value)) fail(code);
    return value;
  }

  function requiredId(value, code) {
    optionalId(value, code);
    if (!value) fail(code);
    return value;
  }

  function integerInRange(value, minimum, maximum, code) {
    if (!Number.isInteger(value) || value < minimum || value > maximum) fail(code);
    return value;
  }

  function enumValue(value, values, code) {
    if (typeof value !== 'string' || !values.includes(value)) fail(code);
    return value;
  }

  function tokenArray(value, allowed, order, code) {
    const source = exactArray(value, code, allowed.length);
    const seen = new Set();
    const result = source.map((item) => {
      if (typeof item !== 'string' || !allowed.includes(item) || seen.has(item)) fail(code);
      seen.add(item);
      return item;
    });
    if (order) {
      const expected = order.filter((item) => seen.has(item));
      if (expected.length !== result.length || expected.some((item, index) => item !== result[index])) fail(code);
    }
    return result;
  }

  function allowedWorkflowSteps(route, phaseName) {
    const byRoute = WORKFLOW_STEPS_BY_ROUTE[route];
    const byPhase = WORKFLOW_STEPS_BY_PHASE[phaseName];
    return WORKFLOW_STEP_ORDER.filter((step) => byRoute.includes(step) && byPhase.includes(step));
  }

  function parseWorkflow(raw) {
    const value = exactObject(raw, WORKFLOW_KEYS, 'WORKFLOW_SHAPE');
    if (typeof value.exists !== 'boolean') fail('WORKFLOW_EXISTS');
    const workflowId = optionalId(value.workflow_id, 'WORKFLOW_ID');
    const kind = enumValue(value.kind, WORKFLOW_KINDS, 'WORKFLOW_KIND');
    const route = enumValue(value.route, WORKFLOW_ROUTES, 'WORKFLOW_ROUTE');
    const taskRevision = integerInRange(value.task_revision, 0, 1000000, 'WORKFLOW_REVISION');
    if (typeof value.phase !== 'string' || !Object.hasOwn(WORKFLOW_PHASES, value.phase)) {
      fail('WORKFLOW_PHASE');
    }
    const spec = WORKFLOW_PHASES[value.phase];
    const phaseName = value.phase;
    const completedSteps = tokenArray(
      value.completed_steps,
      allowedWorkflowSteps(route, phaseName),
      WORKFLOW_STEP_ORDER,
      'WORKFLOW_COMPLETED_STEPS',
    );
    const allowedActions = tokenArray(
      value.allowed_actions,
      spec.actions,
      null,
      'WORKFLOW_ALLOWED_ACTIONS',
    );
    if (typeof value.status !== 'string') fail('WORKFLOW_STATUS');
    if (value.status === 'INCONSISTENT') {
      if (value.next_stage !== 'RETRY' || allowedActions.length) fail('WORKFLOW_FAIL_CLOSED');
    } else if (value.status !== spec.status || value.next_stage !== spec.nextStage) {
      fail('WORKFLOW_PHASE_VIEW');
    }

    if (value.exists) {
      if (!workflowId && value.status !== 'INCONSISTENT') fail('WORKFLOW_ID');
      if (kind === 'NONE' || route === 'NONE') fail('WORKFLOW_IDENTITY');
      if (value.status !== 'INCONSISTENT' && taskRevision === 0) fail('WORKFLOW_REVISION');
    } else if (
      workflowId
      || kind !== 'NONE'
      || route !== 'NONE'
      || taskRevision !== 0
      || phaseName !== 'IDLE'
      || value.status !== 'IDLE'
      || completedSteps.length
      || allowedActions.length
      || value.next_stage !== 'UPLOAD_IMAGE'
    ) {
      fail('WORKFLOW_EMPTY_PROJECTION');
    }

    return {
      exists: value.exists,
      workflow_id: workflowId,
      kind,
      route,
      task_revision: taskRevision,
      phase: phaseName,
      status: value.status,
      completed_steps: completedSteps,
      allowed_actions: allowedActions,
      next_stage: value.next_stage,
    };
  }

  function parseCandidateGeneration(taskRevision, candidateCount, value) {
    if (typeof value !== 'string') fail('CHILD_CANDIDATE_GENERATION');
    if (candidateCount === 0) {
      if (value) fail('CHILD_CANDIDATE_GENERATION');
      return value;
    }
    const match = CANDIDATE_GENERATION_PATTERN.exec(value);
    if (!match || Number(match[1]) !== taskRevision) fail('CHILD_CANDIDATE_GENERATION');
    return value;
  }

  function parseChild(raw) {
    const value = exactObject(raw, CHILD_KEYS, 'CHILD_SHAPE');
    const taskId = optionalId(value.task_id, 'CHILD_ID');
    if (value.kind !== 'A2_QUESTION') fail('CHILD_KIND');
    const unitId = optionalId(value.unit_id, 'CHILD_UNIT_ID');
    const taskRevision = integerInRange(value.task_revision, 0, 1000000, 'CHILD_REVISION');
    if (typeof value.phase !== 'string' || !Object.hasOwn(CHILD_PHASES, value.phase)) {
      fail('CHILD_PHASE');
    }
    const spec = CHILD_PHASES[value.phase];
    const completedSteps = tokenArray(
      value.completed_steps,
      CHILD_STEPS_BY_PHASE[value.phase],
      CHILD_STEP_ORDER,
      'CHILD_COMPLETED_STEPS',
    );
    const allowedActions = tokenArray(
      value.allowed_actions,
      spec.actions,
      null,
      'CHILD_ALLOWED_ACTIONS',
    );
    if (typeof value.status !== 'string') fail('CHILD_STATUS');
    if (value.status === 'INCONSISTENT') {
      if (value.next_stage !== 'RETRY' || allowedActions.length) fail('CHILD_FAIL_CLOSED');
    } else if (value.status !== spec.status || value.next_stage !== spec.nextStage) {
      fail('CHILD_PHASE_VIEW');
    }
    if (!taskId && value.status !== 'INCONSISTENT') fail('CHILD_ID');
    if (value.status !== 'INCONSISTENT' && taskRevision === 0) fail('CHILD_REVISION');
    const chapter = publicText(value.chapter, 64, 'CHILD_CHAPTER');
    const candidateCount = integerInRange(value.candidate_count, 0, 1000000, 'CHILD_CANDIDATE_COUNT');
    const candidateGeneration = parseCandidateGeneration(
      taskRevision,
      candidateCount,
      value.candidate_generation,
    );
    return {
      task_id: taskId,
      kind: value.kind,
      unit_id: unitId,
      task_revision: taskRevision,
      phase: value.phase,
      status: value.status,
      completed_steps: completedSteps,
      allowed_actions: allowedActions,
      next_stage: value.next_stage,
      chapter,
      candidate_count: candidateCount,
      candidate_generation: candidateGeneration,
    };
  }

  function parseUnit(raw) {
    const value = exactObject(raw, UNIT_KEYS, 'UNIT_SHAPE');
    return {
      unit_id: requiredId(value.unit_id, 'UNIT_ID'),
      page_index: integerInRange(value.page_index, 1, 10000, 'UNIT_PAGE_INDEX'),
      display_label: publicText(value.display_label, 64, 'UNIT_DISPLAY_LABEL'),
      status: enumValue(value.status, UNIT_STATUSES, 'UNIT_STATUS'),
    };
  }

  function parseConsistency(raw) {
    const value = exactObject(raw, CONSISTENCY_KEYS, 'CONSISTENCY_SHAPE');
    const status = enumValue(value.status, ['OK', 'INCONSISTENT'], 'CONSISTENCY_STATUS');
    const codes = tokenArray(value.codes, CONSISTENCY_CODES, null, 'CONSISTENCY_CODES');
    if ((status === 'OK') !== (codes.length === 0)) fail('CONSISTENCY_VIEW');
    return { status, codes };
  }

  function unitsEqual(left, right) {
    return UNIT_KEYS.every((key) => left[key] === right[key]);
  }

  function deepFreeze(value) {
    if (value && typeof value === 'object' && !Object.isFrozen(value)) {
      Object.values(value).forEach(deepFreeze);
      Object.freeze(value);
    }
    return value;
  }

  function parseTaskStateSnapshotV1(raw) {
    const value = exactObject(raw, ROOT_KEYS, 'ROOT_SHAPE');
    if (value.schema_version !== 1) fail('UNSUPPORTED_SCHEMA');
    const workflow = parseWorkflow(value.workflow);
    const activeChildTask = value.active_child_task === null ? null : parseChild(value.active_child_task);
    const currentUnit = value.current_unit === null ? null : parseUnit(value.current_unit);
    const units = exactArray(value.units, 'UNITS').map(parseUnit);
    const consistency = parseConsistency(value.consistency);

    const unitIds = new Set();
    const pageIndexes = new Set();
    let lastPageIndex = 0;
    units.forEach((unit) => {
      if (unitIds.has(unit.unit_id) || pageIndexes.has(unit.page_index) || unit.page_index <= lastPageIndex) {
        fail('UNIT_ORDER_OR_IDENTITY');
      }
      unitIds.add(unit.unit_id);
      pageIndexes.add(unit.page_index);
      lastPageIndex = unit.page_index;
    });

    if (!workflow.exists && (units.length || currentUnit)) fail('MISSING_WORKFLOW_UNITS');
    if (workflow.route !== 'A3' && (units.length || currentUnit)) fail('NON_A3_UNITS');
    const activeUnits = units.filter((unit) => unit.status === 'ACTIVE');
    if (activeUnits.length > 1 || activeUnits.length !== Number(currentUnit !== null)) fail('ACTIVE_UNIT_COUNT');
    if (currentUnit) {
      if (!workflow.exists || workflow.route !== 'A3' || !CURRENT_UNIT_PHASES.includes(workflow.phase)) {
        fail('CURRENT_UNIT_PHASE');
      }
      const match = units.find((unit) => unit.unit_id === currentUnit.unit_id);
      if (!match || currentUnit.status !== 'ACTIVE' || !unitsEqual(match, currentUnit)) fail('CURRENT_UNIT_MATCH');
    }
    if (
      workflow.phase === 'COMPLETE'
      && units.some((unit) => !['COMPLETED', 'CLOSED'].includes(unit.status))
    ) fail('COMPLETE_WITH_OPEN_UNIT');

    if (activeChildTask?.phase === 'IDLE') fail('ACTIVE_CHILD_IDLE');
    if (activeChildTask && !workflow.exists && activeChildTask.unit_id) fail('STANDALONE_CHILD_UNIT');
    if (activeChildTask && workflow.exists) {
      if (workflow.phase !== 'A2_ACTIVE' || !['A2', 'A3'].includes(workflow.route)) fail('CHILD_PARENT_PHASE');
      if (
        workflow.workflow_id
        && activeChildTask.task_id
        && workflow.workflow_id === activeChildTask.task_id
      ) fail('PARENT_CHILD_ID_COLLISION');
      if (workflow.route === 'A2' && activeChildTask.unit_id) fail('DIRECT_A2_CHILD_UNIT');
      if (workflow.route === 'A3') {
        if (!activeChildTask.unit_id || !currentUnit || activeChildTask.unit_id !== currentUnit.unit_id) {
          fail('A3_CHILD_UNIT');
        }
      }
    }

    if (consistency.status === 'INCONSISTENT') {
      if (workflow.exists && workflow.status !== 'INCONSISTENT') fail('INCONSISTENT_WORKFLOW');
      if (
        activeChildTask
        && (
          activeChildTask.status !== 'INCONSISTENT'
          || activeChildTask.next_stage !== 'RETRY'
          || activeChildTask.allowed_actions.length
        )
      ) fail('INCONSISTENT_CHILD');
    } else {
      if (workflow.status === 'INCONSISTENT' || activeChildTask?.status === 'INCONSISTENT') {
        fail('CONSISTENT_STATUS');
      }
      if (!WORKFLOW_ROUTE_PHASES[workflow.route].includes(workflow.phase)) fail('WORKFLOW_ROUTE_PHASE');
      if (
        workflow.route === 'A3'
        && ['CROP_REQUIRED', 'VERIFYING_CROP'].includes(workflow.phase)
        && !currentUnit
      ) fail('A3_CROP_CURRENT_UNIT');
      if (workflow.exists && workflow.phase === 'A2_ACTIVE' && !activeChildTask) fail('A2_ACTIVE_CHILD');
      if (workflow.route === 'A3' && workflow.phase === 'A2_ACTIVE' && !currentUnit) {
        fail('A3_ACTIVE_CURRENT_UNIT');
      }
    }

    return deepFreeze({
      schema_version: 1,
      workflow,
      active_child_task: activeChildTask,
      current_unit: currentUnit,
      units,
      consistency,
    });
  }

  function unavailableModel(reason) {
    const model = deepFreeze({
      available: false,
      consistent: false,
      actions_enabled: false,
      reason,
      snapshot: null,
      workflow_actions: [],
      child_actions: [],
      workflow_next_stage: null,
      child_next_stage: null,
    });
    MODEL_INSTANCES.add(model);
    return model;
  }

  const MISSING_MODEL = unavailableModel('MISSING');
  const INVALID_MODEL = unavailableModel('INVALID');
  const UNSUPPORTED_MODEL = unavailableModel('UNSUPPORTED_SCHEMA');

  function createTaskStateModel(raw) {
    if (typeof raw === 'undefined') return MISSING_MODEL;
    let snapshot;
    try {
      snapshot = parseTaskStateSnapshotV1(raw);
    } catch (error) {
      if (error instanceof TaskStateValidationError && error.code === 'UNSUPPORTED_SCHEMA') {
        return UNSUPPORTED_MODEL;
      }
      return INVALID_MODEL;
    }
    const consistent = snapshot.consistency.status === 'OK';
    const model = deepFreeze({
      available: true,
      consistent,
      actions_enabled: consistent,
      reason: consistent ? 'OK' : 'SERVER_INCONSISTENT',
      snapshot,
      workflow_actions: consistent ? snapshot.workflow.allowed_actions : EMPTY_ACTIONS,
      child_actions: consistent && snapshot.active_child_task
        ? snapshot.active_child_task.allowed_actions
        : EMPTY_ACTIONS,
      workflow_next_stage: snapshot.workflow.next_stage,
      child_next_stage: snapshot.active_child_task?.next_stage || null,
    });
    MODEL_INSTANCES.add(model);
    return model;
  }

  function allowsWorkflowAction(model, action) {
    return Boolean(
      model && typeof model === 'object'
      && MODEL_INSTANCES.has(model)
      && model.actions_enabled
      && typeof action === 'string'
      && model.workflow_actions.includes(action),
    );
  }

  function allowsChildAction(model, action) {
    return Boolean(
      model && typeof model === 'object'
      && MODEL_INSTANCES.has(model)
      && model.actions_enabled
      && typeof action === 'string'
      && model.child_actions.includes(action),
    );
  }

  const api = Object.freeze({
    TaskStateValidationError,
    parseTaskStateSnapshotV1,
    createTaskStateModel,
    allowsWorkflowAction,
    allowsChildAction,
  });
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TikuTaskStateV1 = api;
}(typeof globalThis === 'object' ? globalThis : this));
