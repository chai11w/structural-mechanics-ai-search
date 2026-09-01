const TASK_STATE_ASSET_URL = '/assets/task_state.js?v=20260830-task-state-3-4-5';
const TASK_STATE_BOOTSTRAP_ATTRIBUTE = 'data-tiku-task-state-bootstrap';

function showTaskStateBootstrapFailure() {
  const message = '页面资源加载不完整，请刷新页面或稍后重试。';
  const status = document.querySelector('#status-text');
  const runtime = document.querySelector('#runtime-status');
  if (status) status.textContent = message;
  if (runtime) runtime.dataset.state = 'error';
  console.error('Task-state frontend model unavailable.');
}

(function bootstrapTaskStateFrontend() {
  const start = () => {
    const taskStateV1 = globalThis.TikuTaskStateV1;
    if (!taskStateV1) {
      showTaskStateBootstrapFailure();
      return;
    }
    if (globalThis.__tikuDemoStarted) return;
    globalThis.__tikuDemoStarted = true;
    startDemo(taskStateV1);
  };
  if (globalThis.TikuTaskStateV1) {
    start();
    return;
  }
  const existing = document.querySelector(`script[${TASK_STATE_BOOTSTRAP_ATTRIBUTE}]`);
  if (existing) {
    existing.addEventListener('load', start, { once: true });
    existing.addEventListener('error', showTaskStateBootstrapFailure, { once: true });
    return;
  }
  const script = document.createElement('script');
  script.src = TASK_STATE_ASSET_URL;
  script.setAttribute(TASK_STATE_BOOTSTRAP_ATTRIBUTE, '');
  script.addEventListener('load', start, { once: true });
  script.addEventListener('error', showTaskStateBootstrapFailure, { once: true });
  document.head.appendChild(script);
})();

function startDemo(taskStateV1) {
const $ = (selector) => document.querySelector(selector);
const chat = $('#chat');
const empty = $('#empty');
const conversation = $('#conversation');
const form = $('#composer');
const textInput = $('#text');
const fileInput = $('#file');
const attach = $('#attach');
const sendButton = $('#send');
const heroUpload = $('#hero-upload');
const menuButton = $('#menu-button');
const drawer = $('#session-drawer');
const drawerBackdrop = $('#drawer-backdrop');
const closeDrawerButton = $('#close-drawer');
const newChatButton = $('#new-chat');
const topNewChatButton = $('#top-new-chat');
const dropOverlay = $('#drop-overlay');
const runtimeStatus = $('#runtime-status');
const statusText = $('#status-text');
const lightbox = $('#lightbox');
const lightboxImage = $('#lightbox-image');
const lightboxClose = $('#lightbox-close');
const feedbackBackdrop = $('#feedback-backdrop');
const feedbackClose = $('#feedback-close');
const feedbackSubtitle = $('#feedback-subtitle');
const feedbackTags = $('#feedback-tags');
const feedbackDetail = $('#feedback-detail');
const feedbackError = $('#feedback-error');
const feedbackCancel = $('#feedback-cancel');
const feedbackSubmit = $('#feedback-submit');
const authorContactBackdrop = $('#author-contact-backdrop');
const authorContactClose = $('#author-contact-close');
const authorContactChannel = $('#author-contact-channel');
const authorContactValue = $('#author-contact-value');
const authorContactCopy = $('#author-contact-copy');
const a3CropWorkspace = $('#a3-crop-workspace');
const a3CropBack = $('#a3-crop-back');
const a3CropLabel = $('#a3-crop-label');
const a3Reselect = $('#a3-reselect');
const a3Context = $('#a3-context');
const a3ContextText = $('#a3-context-text');
const a3ImageArea = $('.a3-image-area');
const a3ImageFrame = $('#a3-image-frame');
const a3SourceImage = $('#a3-source-image');
const a3Selection = $('#a3-selection');
const a3ImageHint = $('#a3-image-hint');
const a3CropStatus = $('#a3-crop-status');
const a3Submit = $('#a3-submit');
const a3SheetBackdrop = $('#a3-sheet-backdrop');
const a3SheetClose = $('#a3-sheet-close');
const a3SheetUnits = $('#a3-sheet-units');
const a3SheetSubtitle = $('#a3-sheet-subtitle');
const a3SheetOverlay = $('#a3-sheet-overlay');
const a3SheetOverlayImage = $('#a3-sheet-overlay-image');
const a3SheetFooter = $('#a3-sheet-footer');
const a3SheetCount = $('#a3-sheet-count');
const a3Prepare = $('#a3-prepare');
const a3ExampleButton = $('#a3-example-button');
const a3ExampleBackdrop = $('#a3-example-backdrop');
const a3ExampleClose = $('#a3-example-close');
const a3ExampleCanvas = $('#a3-example-canvas');

const TEXT_TIMEOUT_MS = 60000;
const IMAGE_TIMEOUT_MS = 90000;
const SESSION_BOOTSTRAP_TIMEOUT_MS = 15000;
const A3_TIMEOUT_MS = 180000;
const A3_AUTO_PREPARE_IDLE_TIMEOUT_MS = 210000;
const A3_TEXT_RETRY_TIMEOUT_MS = 100000;
const MAX_IMAGE_BYTES = 15 * 1024 * 1024;
const IMAGE_TARGET_BYTES = 1024 * 1024;
const IMAGE_MAX_DIMENSION = 2560;
const AUTHOR_CONTACT_FALLBACK = Object.freeze({ label: '联系作者', channel: '微信', value: 'jglxfd6666' });
const IMAGE_FALLBACK_DIMENSION = 2048;
const IMAGE_QUALITY_STEPS = [0.88, 0.82, 0.76, 0.70];
const HISTORY_TTL_MS = 2 * 60 * 60 * 1000;
const HISTORY_LIMIT = 50;
const HISTORY_KEY = 'tiku-agent-current-chat-v2';
const LEGACY_HISTORY_KEY = 'tiku-agent-current-chat-v1';
const SESSION_ACTIVITY_KEY = 'tiku-agent-session-activity-v1';
const SESSION_RESET_EVENT_KEY = 'tiku-agent-session-reset-v1';
const SESSION_STORAGE_PROBE_KEY = 'tiku-agent-session-storage-probe-v1';
const SESSION_REQUEST_FENCE_KEY = 'tiku-agent-session-request-fence-v1';
const SESSION_REQUEST_LOCK_NAME = 'tiku-agent-session-request-v1';
const OPERATIONAL_NOTICE_KEYS = new Set([
  'connection', 'session-recovery', 'history-storage',
]);
const LEGACY_EXPIRED_MEDIA_MESSAGE = '题图或结果图片已失效，请重新上传题图；如果问题反复出现，可以点踩告诉我们。';
const A3_INLINE_ONLY_INTENTS = new Set([
  'a3_unit_selected', 'a3_unit_already_selected', 'a3_crop_review_required',
]);
const A3_CROP_REVIEW_MESSAGES = Object.freeze({
  SELECTED_DIAGRAM_MISMATCH: '裁剪结果未通过，裁剪图与所选题目不匹配，请重新选择区域裁剪。',
  MULTIPLE_DIAGRAMS: '裁剪结果未通过，裁剪区域包含多个结构图，请重新选择区域裁剪。',
  EXTERNAL_LOADS_INCOMPLETE: '裁剪结果未通过，结构荷载不完整，请重新选择区域裁剪。',
  STRUCTURE_INCOMPLETE: '裁剪结果未通过，结构图不完整，请重新选择区域裁剪。',
  IMAGE_UNCLEAR: '裁剪结果未通过，裁剪图不清晰，请重新选择区域裁剪。',
  CROP_UNCONFIRMED: '裁剪结果未通过，无法确认裁剪图完整，请重新选择区域裁剪。',
  LOAD_CHECK_UNAVAILABLE: '裁剪结果暂时无法确认外荷载，请重新提交裁剪。',
  EXTERNAL_LOADS_NOT_FOUND: '裁剪结果未通过，未识别到结构荷载，请重新选择区域裁剪。',
});
const TASK_STATE_JSON_PATHS = new Set([
  '/api/session', '/api/message', '/api/image', '/api/a3/select', '/api/reset',
]);
const TASK_STATE_STREAM_PATHS = new Set([
  '/api/message/stream', '/api/image/stream', '/api/a3/select/stream',
  '/api/a3/prepare/stream', '/api/a3/crop/stream',
]);
const TASK_STATE_QUEUE_CODES = new Set(['QUEUE_FULL', 'QUEUE_TIMEOUT']);
const a3PrepareSelection = new Set();
const ALLOWED_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/bmp']);
const ALLOWED_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp']);
const FEEDBACK_OPTIONS = {
  positive: [
    ['found_answer', '找到了正确答案'], ['relevant_results', '结果很相关'],
    ['clear_reply', '回复很清楚'], ['fast', '速度很快'], ['other', '其他'],
  ],
  negative: [
    ['not_found', '没找到正确题'], ['irrelevant_results', '结果不相关'],
    ['ranking_issue', '正确题没排前面'], ['wrong_answer', '答案不对'],
    ['too_slow', '搜索太慢'], ['system_error', '系统报错'], ['other', '其他'],
  ],
};
const RECOVERY_ACTION_LABELS = {
  relogin: '重新登录', reupload: '重新上传题图', new_chat: '开始新对话',
  retry_connection: '重新连接', retry_request: '重试上一条', retry_search: '重试搜索',
};

class UserVisibleError extends Error {
  constructor(message, recoveryActions = [], { retryable = true, protocol = {} } = {}) {
    super(message);
    this.name = 'UserVisibleError';
    this.recoveryActions = normalizeRecoveryActions(recoveryActions);
    this.retryable = Boolean(retryable);
    Object.assign(this, protocolFields(protocol));
  }
}

let history = [];
let isBusy = false;
let dragDepth = 0;
let activeController = null;
let focusBeforeModal = null;
let operationVersion = 0;
let pendingUpload = null;
let activeFeedback = null;
let feedbackRequestPending = false;
let historyLastActivityAt = 0;
let historyExpiryTimer = null;
let historyStorageWarningShown = false;
const activeFailureNotices = new Map();
let pendingSessionExpiredNotice = false;
let pendingHistoryStorageNotice = '';
let sessionResetRequired = false;
let sessionResetActivityAt = 0;
let sessionResetEpoch = 0;
let lastHandledSessionResetEventId = storedSessionResetEventId() || '';
let lastHandledSessionRequestFenceId = '';
let sessionBootstrap = null;
let sessionBootstrapPending = false;
let sessionContext = {
  session_valid: false, phase: 'IDLE', has_active_image: false,
  task_revision: 0, candidate_generation: '', candidate_count: 0, search_id: '',
  a3WorkflowId: '', a3WorkflowRevision: 0,
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
let a3Bounds = null;
let a3Pointer = null;
let a3CropHistoryActive = false;
let a3CropHistoryKey = '';
let a3PendingClose = null;
let a3DismissedKey = '';
let a3DismissNextCrop = false;
let a3KnownWorkflowKey = '';
const a3LocalDrafts = new Map();
const objectUrls = new Set();

function syncVisualViewport() {
  const viewport = window.visualViewport;
  const height = viewport?.height || window.innerHeight;
  const offsetTop = viewport?.offsetTop || 0;
  if (!Number.isFinite(height) || height <= 0) return;
  document.documentElement.style.setProperty('--app-height', `${Math.round(height)}px`);
  document.documentElement.style.setProperty('--app-top', `${Math.round(offsetTop)}px`);
}

function isPersistentImage(url) {
  return typeof url === 'string' && (url.startsWith('/api/media/') || url.startsWith('/api/upload/'));
}

function normalizeFeedbackImages(value) {
  return (Array.isArray(value) ? value : []).map((item) => ({
    kind: String(item?.kind || ''),
    url: String(item?.url || ''),
    label: String(item?.label || ''),
  })).filter((item) => item.kind === 'a3_overlay' && isPersistentImage(item.url));
}

function scheduleHistoryExpiry() {
  if (historyExpiryTimer !== null) clearTimeout(historyExpiryTimer);
  historyExpiryTimer = null;
  if (!history.length || !Number.isFinite(historyLastActivityAt) || historyLastActivityAt <= 0) return;
  const remaining = historyLastActivityAt + HISTORY_TTL_MS - Date.now();
  if (remaining <= 0) {
    expireHistoryIfNeeded();
    return;
  }
  historyExpiryTimer = setTimeout(() => {
    historyExpiryTimer = null;
    expireHistoryIfNeeded();
  }, remaining);
}

function createRequestId() {
  const value = globalThis.crypto?.randomUUID?.().replaceAll('-', '');
  return `req_${value || `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`.padEnd(32, '0').slice(0, 32)}`;
}

let sessionActivityFallbackAt = 0;

function safeLocalStorageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch (_error) {
    return undefined;
  }
}

function safeLocalStorageSet(key, value) {
  try {
    localStorage.setItem(key, value);
    return true;
  } catch (_error) {
    return false;
  }
}

function safeLocalStorageRemove(key) {
  try {
    localStorage.removeItem(key);
    return true;
  } catch (_error) {
    return false;
  }
}

function storedHistoryActivityAt() {
  const now = Date.now();
  let activityAt = 0;
  try {
    const raw = safeLocalStorageGet(HISTORY_KEY) || safeLocalStorageGet(LEGACY_HISTORY_KEY);
    const stored = JSON.parse(raw || 'null');
    const storedActivityAt = Number(stored?.lastActivityAt ?? stored?.savedAt);
    if (
      Array.isArray(stored?.messages)
      && Number.isFinite(storedActivityAt)
      && storedActivityAt > 0
      && storedActivityAt <= now + 60000
    ) activityAt = storedActivityAt;
  } catch (_error) { /* invalid history cannot authorize a reset */ }
  const storedSharedActivityAt = Number(safeLocalStorageGet(SESSION_ACTIVITY_KEY) || 0);
  const sharedActivityAt = Math.max(
    sessionActivityFallbackAt,
    Number.isFinite(storedSharedActivityAt) ? storedSharedActivityAt : 0,
  );
  if (
    Number.isFinite(sharedActivityAt)
    && sharedActivityAt > 0
    && sharedActivityAt <= now + 60000
  ) activityAt = Math.max(activityAt, sharedActivityAt);
  return activityAt;
}

function cancelPendingExpiredReset(activityAt) {
  if (
    !sessionResetRequired
    || !sessionResetActivityAt
    || activityAt <= sessionResetActivityAt
  ) return false;
  sessionResetRequired = false;
  sessionResetActivityAt = 0;
  pendingSessionExpiredNotice = false;
  return true;
}

function refreshHistoryActivityFromStorage() {
  const activityAt = storedHistoryActivityAt();
  const resetCancelled = cancelPendingExpiredReset(activityAt);
  if (!activityAt || activityAt <= historyLastActivityAt) return resetCancelled;
  historyLastActivityAt = activityAt;
  scheduleHistoryExpiry();
  return true;
}

function touchSharedSessionActivity() {
  const activityAt = Math.max(
    Date.now(),
    historyLastActivityAt + 1,
    storedHistoryActivityAt() + 1,
  );
  historyLastActivityAt = activityAt;
  sessionActivityFallbackAt = activityAt;
  safeLocalStorageSet(SESSION_ACTIVITY_KEY, String(activityAt));
  cancelPendingExpiredReset(activityAt);
  return activityAt;
}

function taskStateApiPath(url) {
  return String(url || '').split('?', 1)[0];
}

function isTaskStateRequestPath(url) {
  const path = taskStateApiPath(url);
  return TASK_STATE_JSON_PATHS.has(path) || TASK_STATE_STREAM_PATHS.has(path);
}

function isTaskStartingPath(url) {
  const path = taskStateApiPath(url);
  return isTaskStateRequestPath(path) && !['/api/session', '/api/reset'].includes(path);
}

function isExplicitSessionResetText(value) {
  const compact = String(value || '')
    .toLowerCase()
    .replace(/[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000，,。！？!?、.]+/gu, '');
  return /^(?:(?:开始|创建|开个|开启)?新对话|(?:清空|删除)(?:整个|全部|当前)?(?:会话|对话|聊天记录)|全部清空)$/u
    .test(compact);
}

function sessionRequestLockAvailable() {
  return typeof globalThis.navigator?.locks?.request === 'function';
}

function sessionResetCoordinationAvailable() {
  if (!sessionRequestLockAvailable()) return false;
  const probe = createRequestId();
  if (!safeLocalStorageSet(SESSION_STORAGE_PROBE_KEY, probe)) return false;
  const readable = safeLocalStorageGet(SESSION_STORAGE_PROBE_KEY) === probe;
  const removable = safeLocalStorageRemove(SESSION_STORAGE_PROBE_KEY);
  return readable && removable;
}

function storedSessionResetEventId() {
  const value = safeLocalStorageGet(SESSION_RESET_EVENT_KEY);
  return value === undefined ? null : String(value || '');
}

function storedSessionRequestFenceId() {
  const value = safeLocalStorageGet(SESSION_REQUEST_FENCE_KEY);
  return value === undefined ? undefined : String(value || '');
}

function retireUnhandledSessionReset(resetEventId = storedSessionResetEventId()) {
  const eventId = typeof resetEventId === 'string' ? resetEventId : '';
  if (!eventId || eventId === lastHandledSessionResetEventId) return false;
  lastHandledSessionResetEventId = eventId;
  sessionResetEpoch += 1;
  retireSessionForExternalReset();
  return true;
}

function createSessionRequestFence() {
  const id = `${Date.now()}:${createRequestId()}`;
  if (!safeLocalStorageSet(SESSION_REQUEST_FENCE_KEY, id)) return null;
  if (storedSessionRequestFenceId() !== id) {
    safeLocalStorageRemove(SESSION_REQUEST_FENCE_KEY);
    return null;
  }
  return { id, preserve: true, inherited: false };
}

function preserveSessionRequestFence(fence) {
  if (fence?.id) fence.preserve = true;
}

function resolveSessionRequestFence(fence) {
  if (fence?.id) fence.preserve = false;
}

function clearSessionRequestFence(fence) {
  if (!fence?.id || storedSessionRequestFenceId() !== fence.id) return false;
  return safeLocalStorageRemove(SESSION_REQUEST_FENCE_KEY)
    && storedSessionRequestFenceId() === '';
}

function staleSessionActionError(message = '会话已在另一页面更新，请重新连接后继续。') {
  return new UserVisibleError(
    message,
    ['retry_connection', 'new_chat'],
    {
      protocol: {
        status: 'ERROR', layer: 'network', code: 'STALE_ACTION',
        retryable: true, action: 'retry_connection', request_id: createRequestId(),
      },
    },
  );
}

function sessionCoordinationError() {
  return clientProtocolError(
    '浏览器无法安全确认当前会话，请重新连接或开始新对话。',
    'RESPONSE_INVALID',
    createRequestId(),
    ['retry_connection', 'new_chat'],
  );
}

function sessionLockUnavailableError() {
  return clientProtocolError(
    '当前浏览器无法安全协调多个页面，请升级浏览器或改用最新版 Chrome、Edge 后重试。',
    'RESPONSE_INVALID',
    createRequestId(),
    ['retry_connection'],
  );
}

function retireUnresolvedSessionRequestFence(fenceId) {
  const id = String(fenceId || '');
  if (!id || id === lastHandledSessionRequestFenceId) return false;
  lastHandledSessionRequestFenceId = id;
  sessionResetEpoch += 1;
  retireSessionForExternalReset();
  return true;
}

async function completeSessionRequestWithFence(callback, fence) {
  let result;
  let failure = null;
  try {
    result = await callback(fence);
  } catch (error) {
    failure = error;
  }
  if (!fence.preserve && !clearSessionRequestFence(fence)) {
    preserveSessionRequestFence(fence);
    retireUnresolvedSessionRequestFence(fence.id);
    throw sessionCoordinationError();
  }
  if (fence.preserve && !failure) failure = sessionCoordinationError();
  if (failure) throw failure;
  return result;
}

async function withSessionRequestLock(url, callback, { requireSupport = false } = {}) {
  if (!isTaskStateRequestPath(url)) return callback();
  const requestEpoch = sessionResetEpoch;
  if (!sessionRequestLockAvailable()) {
    if (requireSupport) return null;
    retireUnhandledSessionReset();
    const pendingFenceId = storedSessionRequestFenceId();
    if (
      (requestEpoch !== sessionResetEpoch || pendingFenceId)
      && isTaskStartingPath(url)
    ) {
      if (pendingFenceId) retireUnresolvedSessionRequestFence(pendingFenceId);
      throw staleSessionActionError();
    }
    if (isTaskStartingPath(url)) throw sessionLockUnavailableError();
    if (pendingFenceId === undefined) throw sessionCoordinationError();
    if (!pendingFenceId) return callback(null);
    return completeSessionRequestWithFence(
      callback,
      { id: pendingFenceId, preserve: true, inherited: true },
    );
  }
  return globalThis.navigator.locks.request(
    SESSION_REQUEST_LOCK_NAME,
    { mode: 'exclusive' },
    async () => {
      retireUnhandledSessionReset();
      if (requestEpoch !== sessionResetEpoch && isTaskStartingPath(url)) {
        throw staleSessionActionError();
      }
      const pendingFenceId = storedSessionRequestFenceId();
      if (pendingFenceId === undefined) throw sessionCoordinationError();
      if (pendingFenceId && isTaskStartingPath(url)) {
        retireUnresolvedSessionRequestFence(pendingFenceId);
        throw staleSessionActionError(
          '上次请求结果尚未确认，请重新连接或开始新对话。',
        );
      }
      const fence = pendingFenceId
        ? { id: pendingFenceId, preserve: true, inherited: true }
        : createSessionRequestFence();
      if (!fence) throw sessionCoordinationError();
      return completeSessionRequestWithFence(callback, fence);
    },
  );
}

function publishSessionReset(sessionRequestFence = null) {
  const eventId = `${Date.now()}:${createRequestId()}`;
  if (
    !safeLocalStorageSet(SESSION_RESET_EVENT_KEY, eventId)
    || safeLocalStorageGet(SESSION_RESET_EVENT_KEY) !== eventId
  ) {
    preserveSessionRequestFence(sessionRequestFence);
    return false;
  }
  sessionResetEpoch += 1;
  lastHandledSessionResetEventId = eventId;
  sessionActivityFallbackAt = 0;
  safeLocalStorageRemove(SESSION_ACTIVITY_KEY);
  resolveSessionRequestFence(sessionRequestFence);
  return true;
}

function authoritativeTaskStateEnvelopeAccepted(envelope) {
  return Boolean(
    envelope
    && typeof envelope === 'object'
    && taskStateAcceptedEnvelopes.get(envelope) === taskStateRequestGeneration
    && taskStateContext.available
    && taskStateContext.consistent
  );
}

function authoritativeTaskStateIsEmpty() {
  const snapshot = taskStateContext?.snapshot;
  return taskStateContext.available
    && taskStateContext.consistent
    && snapshot?.workflow?.exists === false
    && snapshot.active_child_task === null
    && snapshot.current_unit === null
    && snapshot.units.length === 0;
}

function publishAuthoritativeReset(
  url,
  envelope,
  { error = false, sessionRequestFence = null } = {},
) {
  if (!authoritativeTaskStateEnvelopeAccepted(envelope)) return true;
  const isEmpty = authoritativeTaskStateIsEmpty();
  if (
    isEmpty
    && (
      error
      || taskStateApiPath(url) === '/api/reset'
      || envelope?.intent === 'a3_session_reset'
      || sessionRequestFence?.inherited
    )
  ) return publishSessionReset(sessionRequestFence);
  return true;
}

function applyAuthoritativeEmptyError(url, envelope, { sessionRequestFence = null } = {}) {
  if (!authoritativeTaskStateEnvelopeAccepted(envelope) || !authoritativeTaskStateIsEmpty()) {
    return null;
  }
  const published = publishAuthoritativeReset(
    url,
    envelope,
    { error: true, sessionRequestFence },
  );
  const applied = applyResetSessionContext(envelope);
  if (applied) {
    clearHistory();
    renderHistory();
  }
  if (!published || !applied) preserveSessionRequestFence(sessionRequestFence);
  return published && applied;
}

function applyAuthoritativeEmptyAfterCoordinationFailure(envelope, sessionRequestFence) {
  preserveSessionRequestFence(sessionRequestFence);
  const applied = applyResetSessionContext(envelope);
  if (applied) {
    clearHistory();
    renderHistory();
  }
  return applied;
}

function resolveSessionRequestFenceFromEnvelope(envelope, sessionRequestFence) {
  if (
    authoritativeTaskStateEnvelopeAccepted(envelope)
    || isTaskStateQueueNoUpdate(envelope)
  ) resolveSessionRequestFence(sessionRequestFence);
}

function beginTaskStateRequest(url, responseMode) {
  const paths = responseMode === 'stream' ? TASK_STATE_STREAM_PATHS : TASK_STATE_JSON_PATHS;
  if (!paths.has(taskStateApiPath(url))) return null;
  const request = taskStateConsumer.begin();
  activeTaskStateRequest = request;
  taskStateRequestGeneration += 1;
  activeTaskStateRequestGeneration = taskStateRequestGeneration;
  taskStateContext = taskStateConsumer.current();
  syncTaskStateActionButtons();
  return request;
}

function isTaskStateQueueNoUpdate(envelope) {
  return envelope?.layer === 'queue' && TASK_STATE_QUEUE_CODES.has(envelope?.code);
}

function consumeTaskStateResponse(request, envelope, { error = false } = {}) {
  if (request === null || (error && isTaskStateQueueNoUpdate(envelope))) return;
  const accepted = request === activeTaskStateRequest;
  const acceptedGeneration = accepted ? activeTaskStateRequestGeneration : 0;
  taskStateContext = taskStateConsumer.consume(request, envelope);
  if (accepted && envelope && typeof envelope === 'object') {
    taskStateAcceptedEnvelopes.delete(envelope);
    taskStateEnvelopeBindings.delete(envelope);
    if (taskStateContext.available && taskStateContext.consistent) {
      const workflow = taskStateContext.snapshot?.workflow;
      const target = currentWorkflowActionTarget();
      const legacyA3 = normalizeA3Snapshot(envelope?.session?.a3);
      const projectionAllowed = workflow?.route === 'A3'
        ? a3SnapshotMatchesTaskState(legacyA3, target)
        : legacyA3 === null;
      if (projectionAllowed) {
        if (workflow?.route === 'A3') taskStateEnvelopeBindings.set(envelope, target);
        taskStateAcceptedEnvelopes.set(envelope, acceptedGeneration);
      } else {
        invalidateTaskStateContext();
      }
    }
  }
  if (accepted) {
    activeTaskStateRequest = null;
    activeTaskStateRequestGeneration = 0;
  }
  syncTaskStateActionButtons();
}

function finishTaskStateRequest(request) {
  if (request === null) return;
  if (request === activeTaskStateRequest) {
    activeTaskStateRequest = null;
    activeTaskStateRequestGeneration = 0;
  }
  taskStateContext = taskStateConsumer.finish(request);
  syncTaskStateActionButtons();
}

function invalidateTaskStateContext() {
  const retirement = taskStateConsumer.begin();
  taskStateContext = taskStateConsumer.current();
  taskStateConsumer.finish(retirement);
  activeTaskStateRequest = null;
  activeTaskStateRequestGeneration = 0;
  taskStateRequestGeneration += 1;
  syncTaskStateActionButtons();
}

function currentChildActionTarget() {
  const child = taskStateContext?.snapshot?.active_child_task;
  if (!child) return null;
  return {
    childTaskId: String(child.task_id || ''),
    childTaskRevision: Number(child.task_revision || 0),
    childCandidateGeneration: String(child.candidate_generation || ''),
  };
}

function candidateChildActionBinding(item, index) {
  const actionTarget = {
    childTaskId: String(item.childTaskId || ''),
    childTaskRevision: Number(item.childTaskRevision || 0),
    childCandidateGeneration: String(item.childCandidateGeneration || ''),
    candidateRank: Number(index) + 1,
  };
  return {
    actionTarget,
    actionContext: {
      type: 'select_candidate',
      task_id: actionTarget.childTaskId,
      rank: actionTarget.candidateRank,
      task_revision: actionTarget.childTaskRevision,
      candidate_generation: actionTarget.childCandidateGeneration,
    },
  };
}

function recoveryChildActionBinding(action, retryAction, item) {
  if (action === 'retry_search') {
    const target = {
      childTaskId: String(item.childTaskId || ''),
      childTaskRevision: Number(item.childTaskRevision || 0),
    };
    return {
      action: 'retry_search',
      target,
      actionContext: {
        type: 'retry_search',
        task_id: target.childTaskId,
        task_revision: target.childTaskRevision,
      },
    };
  }
  if (
    action !== 'retry_request'
    || !['select_candidate', 'retry_search'].includes(retryAction?.actionContext?.type)
  ) {
    return null;
  }
  const actionContext = retryAction.actionContext;
  return {
    action: actionContext.type,
    target: {
      childTaskId: String(actionContext.task_id || ''),
      childTaskRevision: Number(actionContext.task_revision || 0),
      ...(actionContext.type === 'select_candidate' ? {
        childCandidateGeneration: String(actionContext.candidate_generation || ''),
        candidateRank: Number(actionContext.rank || 0),
      } : {}),
    },
    actionContext,
  };
}

function taskStateAllowsChildAction(action, target = null) {
  if (!taskStateV1.allowsChildAction(taskStateContext, action)) return false;
  if (!target || typeof target !== 'object') return false;
  const child = taskStateContext.snapshot?.active_child_task;
  const taskId = String(target.childTaskId || '');
  const taskRevision = Number(target.childTaskRevision || 0);
  if (!child || !taskId || taskId !== child.task_id || taskRevision !== child.task_revision) return false;
  if (Object.hasOwn(target, 'childCandidateGeneration')) {
    const generation = String(target.childCandidateGeneration || '');
    if (!generation || generation !== child.candidate_generation) return false;
  }
  if (Object.hasOwn(target, 'candidateRank')) {
    const rank = Number(target.candidateRank || 0);
    if (!Number.isInteger(rank) || rank < 1 || rank > child.candidate_count) return false;
  }
  return true;
}

function childActionTargetFromButton(button) {
  const target = {
    childTaskId: String(button.dataset.childTaskId || ''),
    childTaskRevision: Number(button.dataset.childTaskRevision || 0),
  };
  if (button.dataset.childAction === 'select_candidate') {
    target.childCandidateGeneration = String(button.dataset.childCandidateGeneration || '');
    target.candidateRank = Number(button.dataset.candidateRank || 0);
  }
  return target;
}

function bindChildActionButton(button, action, target) {
  button.dataset.childAction = action;
  button.dataset.childTaskId = String(target?.childTaskId || '');
  button.dataset.childTaskRevision = String(Number(target?.childTaskRevision || 0));
  if (Object.hasOwn(target || {}, 'childCandidateGeneration')) {
    button.dataset.childCandidateGeneration = String(target.childCandidateGeneration || '');
  }
  if (Object.hasOwn(target || {}, 'candidateRank')) {
    button.dataset.candidateRank = String(Number(target.candidateRank || 0));
  }
}

function currentWorkflowActionTarget() {
  const workflow = taskStateContext?.snapshot?.workflow;
  if (!workflow?.exists) return null;
  return {
    workflowId: String(workflow.workflow_id || ''),
    workflowRevision: Number(workflow.task_revision || 0),
  };
}

function workflowActionTargetMatchesA3(target, a3) {
  const workflow = taskStateContext?.snapshot?.workflow;
  return Boolean(
    target
    && a3
    && workflow?.exists
    && workflow.route === 'A3'
    && String(target.workflowId || '') === workflow.workflow_id
    && Number(target.workflowRevision || 0) === workflow.task_revision
    && Number(a3.task_revision || 0) === Number(target.workflowRevision || 0)
    && String(a3.phase || '') === workflow.phase
  );
}

function taskStateWorkflowUnit(unitId) {
  const cleanUnitId = String(unitId || '');
  return taskStateContext?.snapshot?.units.find((unit) => unit.unit_id === cleanUnitId) || null;
}

function taskStateAllowsWorkflowAction(action, target = null) {
  if (!taskStateV1.allowsWorkflowAction(taskStateContext, action)) return false;
  if (!target || typeof target !== 'object') return false;
  const workflow = taskStateContext.snapshot?.workflow;
  const workflowId = String(target.workflowId || '');
  const workflowRevision = Number(target.workflowRevision || 0);
  if (
    !workflow?.exists
    || workflow.route !== 'A3'
    || !workflowId
    || workflowId !== workflow.workflow_id
    || workflowRevision !== workflow.task_revision
  ) return false;
  if (action === 'select_unit') {
    const unitId = String(target.unitId || '');
    const unit = unitId ? taskStateWorkflowUnit(unitId) : null;
    return Boolean(unit && ['AVAILABLE', 'PREPARED'].includes(unit.status));
  }
  if (action === 'prepare_units') {
    const unitIds = Array.isArray(target.unitIds) ? target.unitIds.map(String) : [];
    if (!unitIds.length || unitIds.some((unitId) => !unitId) || new Set(unitIds).size !== unitIds.length) {
      return false;
    }
    return unitIds.every((unitId) => taskStateWorkflowUnit(unitId)?.status === 'AVAILABLE');
  }
  if (action === 'submit_crop') {
    const unitId = String(target.unitId || '');
    const currentUnit = taskStateContext.snapshot?.current_unit;
    return Boolean(unitId && currentUnit?.unit_id === unitId && currentUnit.status === 'ACTIVE');
  }
  return false;
}

function taskStateAllowsA3Action(action, target, a3 = a3Current()) {
  if (!workflowActionTargetMatchesA3(target, a3)) return false;
  if (
    action === 'submit_crop'
    && String(a3?.selected_unit?.unit_id || '') !== String(target?.unitId || '')
  ) return false;
  return taskStateAllowsWorkflowAction(action, target);
}

function a3SnapshotMatchesTaskState(a3, target) {
  if (!workflowActionTargetMatchesA3(target, a3)) return false;
  const stateUnits = taskStateContext?.snapshot?.units || [];
  if (a3.units.length !== stateUnits.length) return false;
  const legacyUnits = new Map();
  for (const unit of a3.units) {
    if (!unit.unit_id || legacyUnits.has(unit.unit_id)) return false;
    legacyUnits.set(unit.unit_id, unit);
  }
  for (const unit of stateUnits) {
    const legacyUnit = legacyUnits.get(unit.unit_id);
    const preparationStatus = String(legacyUnit?.preparation_status || '');
    if (
      !legacyUnit
      || legacyUnit.page_index !== unit.page_index
      || legacyUnit.display_label !== unit.display_label
      || legacyUnit.completed !== (unit.status === 'COMPLETED')
      || legacyUnit.searched !== (unit.status === 'CLOSED')
      || legacyUnit.selected !== (unit.status === 'ACTIVE')
      || !['pending', 'located', 'manual', 'ready'].includes(preparationStatus)
    ) return false;
    if (!legacyUnit.completed && !legacyUnit.searched && !legacyUnit.selected) {
      if (preparationStatus === 'ready' && !legacyUnit.crop_available) return false;
      if ((preparationStatus === 'ready') !== (unit.status === 'PREPARED')) return false;
    }
  }
  const currentUnit = taskStateContext.snapshot?.current_unit || null;
  const currentUnitId = String(currentUnit?.unit_id || '');
  if (String(a3.selected_unit?.unit_id || '') !== currentUnitId) return false;
  if (String(a3.selected_unit?.display_label || '') !== String(currentUnit?.display_label || '')) {
    return false;
  }
  return a3.units.every((unit) => unit.selected === (unit.unit_id === currentUnitId));
}

function taskStateAllowsA3UnitNavigation(target, a3 = a3Current()) {
  if (!workflowActionTargetMatchesA3(target, a3)) return false;
  const units = taskStateContext?.snapshot?.units || [];
  return units.some((unit) => taskStateAllowsWorkflowAction('select_unit', {
    ...target,
    unitId: unit.unit_id,
  })) || units.some((unit) => taskStateAllowsWorkflowAction('prepare_units', {
    ...target,
    unitIds: [unit.unit_id],
  }));
}

function workflowActionTargetFromControl(control) {
  const target = {
    workflowId: String(control.dataset.workflowId || ''),
    workflowRevision: Number(control.dataset.workflowRevision || 0),
  };
  if (Object.hasOwn(control.dataset, 'workflowUnitId')) {
    target.unitId = String(control.dataset.workflowUnitId || '');
  }
  if (Object.hasOwn(control.dataset, 'workflowUnitIds')) {
    try {
      const unitIds = JSON.parse(control.dataset.workflowUnitIds || '[]');
      target.unitIds = Array.isArray(unitIds) ? unitIds.map(String) : [];
    } catch (_error) {
      target.unitIds = [];
    }
  }
  return target;
}

function bindWorkflowActionControl(control, action, target, { hideWhenDenied = false } = {}) {
  delete control.dataset.a3UnitNavigation;
  delete control.dataset.workflowUnitId;
  delete control.dataset.workflowUnitIds;
  control.dataset.workflowAction = action;
  control.dataset.workflowId = String(target?.workflowId || '');
  control.dataset.workflowRevision = String(Number(target?.workflowRevision || 0));
  control.dataset.hideWhenDenied = String(Boolean(hideWhenDenied));
  if (Object.hasOwn(target || {}, 'unitId')) {
    control.dataset.workflowUnitId = String(target.unitId || '');
  }
  if (Object.hasOwn(target || {}, 'unitIds')) {
    control.dataset.workflowUnitIds = JSON.stringify(target.unitIds || []);
  }
}

function bindA3UnitNavigationControl(control, target, { hideWhenDenied = false } = {}) {
  delete control.dataset.workflowAction;
  delete control.dataset.workflowUnitId;
  delete control.dataset.workflowUnitIds;
  control.dataset.a3UnitNavigation = 'true';
  control.dataset.workflowId = String(target?.workflowId || '');
  control.dataset.workflowRevision = String(Number(target?.workflowRevision || 0));
  control.dataset.hideWhenDenied = String(Boolean(hideWhenDenied));
}

function syncA2ActionButtons() {
  if (typeof document !== 'object') return;
  document.querySelectorAll('[data-child-action]').forEach((button) => {
    const action = String(button.dataset.childAction || '');
    const allowed = !(typeof isBusy === 'boolean' && isBusy)
      && button.dataset.mediaAvailable !== 'false'
      && taskStateAllowsChildAction(action, childActionTargetFromButton(button));
    button.disabled = !allowed;
    if (button.classList.contains('select-candidate')) {
      button.textContent = allowed ? '选择' : '候选已失效';
    }
    if (button.classList.contains('message-recovery')) button.hidden = !allowed;
  });
}

function syncTaskStateActionButtons() {
  syncA2ActionButtons();
  if (typeof syncA3ActionButtons === 'function') syncA3ActionButtons();
}

function protocolFields(source = {}) {
  return {
    status: String(source.status || ''),
    layer: String(source.layer || ''),
    code: String(source.code || ''),
    retryable: Boolean(source.retryable),
    action: String(source.action || ''),
    requestId: String(source.request_id || source.requestId || ''),
    searchId: String(source.search_id || source.searchId || ''),
    responseId: String(source.response_id || source.responseId || ''),
  };
}

function protocolRecoveryAction(action) {
  const value = String(action || '');
  if (value === 'retry_upload') return 'reupload';
  return Object.hasOwn(RECOVERY_ACTION_LABELS, value) ? value : '';
}

function clientProtocolError(message, code, requestId, recoveryActions = ['retry_request']) {
  return new UserVisibleError(message, recoveryActions, {
    protocol: {
      status: 'ERROR', layer: 'network', code, retryable: true,
      action: recoveryActions.includes('retry_connection') ? 'retry_connection' : 'retry_request',
      request_id: requestId, search_id: sessionContext.search_id || '',
    },
  });
}

function saveHistory({ refreshActivity = false } = {}) {
  if (refreshActivity || !Number.isFinite(historyLastActivityAt) || historyLastActivityAt <= 0) {
    historyLastActivityAt = touchSharedSessionActivity();
  } else {
    historyLastActivityAt = Math.max(historyLastActivityAt, storedHistoryActivityAt());
  }
  const saved = safeLocalStorageSet(HISTORY_KEY, JSON.stringify({
    savedAt: historyLastActivityAt,
    lastActivityAt: historyLastActivityAt,
    messages: history.slice(-HISTORY_LIMIT),
  }));
  if (!saved) {
    if (!historyStorageWarningShown) {
      historyStorageWarningShown = true;
      setTimeout(() => showFailureNotice(
        'history-storage',
        '浏览器无法保存临时对话。当前页面仍可使用，但刷新后记录可能丢失，请检查浏览器存储设置。',
      ), 0);
    }
  }
  scheduleHistoryExpiry();
}

function releaseObjectUrl(url) {
  if (!objectUrls.has(url)) return;
  URL.revokeObjectURL(url);
  objectUrls.delete(url);
}

function releaseAllObjectUrls() {
  objectUrls.forEach((url) => URL.revokeObjectURL(url));
  objectUrls.clear();
}

function clearPendingUpload({ releasePreview = true } = {}) {
  if (releasePreview && pendingUpload?.preview) releaseObjectUrl(pendingUpload.preview);
  pendingUpload = null;
}

function remember(item) {
  history.push({
    message: String(item.message || ''),
    me: Boolean(item.me),
    images: (item.images || []).filter(isPersistentImage),
    imageAlt: String(item.imageAlt || '题库图片'),
    intent: String(item.intent || ''),
    variant: String(item.variant || ''),
    taskRevision: Number(item.taskRevision || 0),
    candidateCount: Number(item.candidateCount || 0),
    candidateGeneration: String(item.candidateGeneration || ''),
    childTaskId: String(item.childTaskId || ''),
    childTaskRevision: Number(item.childTaskRevision || 0),
    childCandidateGeneration: String(item.childCandidateGeneration || ''),
    workflowId: String(item.workflowId || ''),
    workflowRevision: Number(item.workflowRevision || 0),
    messageId: String(item.messageId || ''),
    responseId: String(item.responseId || ''),
    noticeKey: String(item.noticeKey || ''),
    createdAt: Number(item.createdAt || 0),
    feedback: item.feedback || null,
    feedbackImages: normalizeFeedbackImages(item.feedbackImages),
    recoveryActions: normalizeRecoveryActions(item.recoveryActions),
    retryAction: normalizeRetryAction(item.retryAction),
    authorContact: normalizeAuthorContact(item.authorContact),
    a3: normalizeA3Snapshot(item.a3),
    ...protocolFields(item),
  });
  saveHistory({ refreshActivity: true });
}

function scrollToLatest() {
  requestAnimationFrame(() => conversation.scrollTo({ top: conversation.scrollHeight, behavior: 'smooth' }));
}

function mediaKind(item) {
  if (item.me) return 'upload';
  if (item.intent === 'select_candidate' || item.intent === 'resend_answer') return 'answer';
  return item.images?.length ? 'candidate' : '';
}

function normalizeRecoveryActions(actions) {
  return Array.from(new Set(Array.isArray(actions) ? actions : []))
    .filter((action) => Object.hasOwn(RECOVERY_ACTION_LABELS, action));
}

function normalizeRetryAction(action) {
  if (!action || action.type !== 'text') return null;
  const value = String(action.value || '').trim();
  if (!value) return null;
  const rawContext = action.actionContext && typeof action.actionContext === 'object'
    ? action.actionContext
    : null;
  const contextType = String(rawContext?.type || '');
  const context = ['select_candidate', 'retry_search'].includes(contextType)
    ? {
        type: contextType,
        task_id: String(rawContext.task_id || ''),
        task_revision: Number(rawContext.task_revision || 0),
        ...(contextType === 'select_candidate' ? {
          rank: Number(rawContext.rank || 0),
          candidate_generation: String(rawContext.candidate_generation || ''),
        } : {}),
      }
    : null;
  return {
    type: 'text', value, displayValue: String(action.displayValue || value),
    actionContext: context,
  };
}

function mergeRecoveryActions(...groups) {
  return normalizeRecoveryActions(groups.flat());
}

function taskStateFailureRecoveryActions(actions = []) {
  const normalized = normalizeRecoveryActions(actions);
  return taskStateContext?.reason === 'MISSING'
    ? mergeRecoveryActions(normalized, ['retry_connection'])
    : normalized;
}

function normalizeA3Snapshot(value) {
  if (!value || typeof value !== 'object' || value.enabled !== true) return null;
  return {
    enabled: true,
    auto_crop_enabled: Boolean(value.auto_crop_enabled),
    auto_prepare_all_enabled: Boolean(value.auto_prepare_all_enabled),
    auto_prepare_all_units: Boolean(value.auto_prepare_all_units),
    phase: String(value.phase || ''),
    page_finished: Boolean(value.page_finished),
    units: Array.isArray(value.units) ? value.units.map((unit) => ({
      unit_id: String(unit.unit_id || ''),
      page_index: Number(unit.page_index || 0),
      display_label: String(unit.display_label || ''),
      title_text: String(unit.title_text || ''),
      completed: Boolean(unit.completed),
      searched: Boolean(unit.searched),
      selected: Boolean(unit.selected),
      requested: Boolean(unit.requested),
      crop_available: Boolean(unit.crop_available),
      preparation_status: String(unit.preparation_status || 'pending'),
    })) : [],
    selected_unit: value.selected_unit && typeof value.selected_unit === 'object' ? {
      unit_id: String(value.selected_unit.unit_id || ''),
      display_label: String(value.selected_unit.display_label || ''),
      context_text: String(value.selected_unit.context_text || ''),
    } : { unit_id: '', display_label: '', context_text: '' },
    auto_crop_overlay_available: Boolean(value.auto_crop_overlay_available),
    crop_review_required: Boolean(value.crop_review_required),
    crop_review_code: String(value.crop_review_code || ''),
    crop_draft: value.crop_draft && typeof value.crop_draft === 'object' ? value.crop_draft : {},
    task_revision: Number(value.task_revision || 0),
  };
}

function openLightbox(url, alt) {
  focusBeforeModal = document.activeElement;
  lightboxImage.src = url;
  lightboxImage.alt = alt;
  lightbox.hidden = false;
  document.body.dataset.modal = 'lightbox';
  lightboxClose.focus();
}

function closeLightbox() {
  if (lightbox.hidden) return;
  lightbox.hidden = true;
  lightboxImage.removeAttribute('src');
  delete document.body.dataset.modal;
  focusBeforeModal?.focus();
}

function feedbackConversation(messageId) {
  const target = history.findIndex((item) => item.messageId === messageId);
  if (target < 0) return null;
  const visible = history.slice(0, target + 1);
  const targetRevision = Number(visible.at(-1)?.taskRevision || 0);
  let start = visible.length - 1;
  if (targetRevision > 0) {
    for (let index = visible.length - 1; index >= 0; index -= 1) {
      const item = visible[index];
      if (
        item.me
        && Number(item.taskRevision || 0) === targetRevision
        && (item.images || []).some(isPersistentImage)
      ) {
        start = index;
        break;
      }
    }
    if (start === visible.length - 1) {
      for (let index = visible.length - 1; index >= 0; index -= 1) {
        const item = visible[index];
        if (item.me && Number(item.taskRevision || 0) === targetRevision) {
          start = index;
          break;
        }
      }
    }
  } else {
    for (let index = visible.length - 1; index >= 0; index -= 1) {
      if (visible[index].me) {
        start = index;
        break;
      }
    }
  }
  return visible.slice(start).map((item) => ({
    message: String(item.message || ''),
    me: Boolean(item.me),
    images: (item.images || []).filter(isPersistentImage),
    imageAlt: String(item.imageAlt || '题目图片'),
    intent: String(item.intent || ''),
    variant: String(item.variant || ''),
    taskRevision: Number(item.taskRevision || 0),
    candidateCount: Number(item.candidateCount || 0),
    messageId: String(item.messageId || ''),
    responseId: String(item.responseId || ''),
    createdAt: Number(item.createdAt || 0),
    a3Overlay: item.messageId === messageId
      ? String(normalizeFeedbackImages(item.feedbackImages).find((image) => image.kind === 'a3_overlay')?.url || '')
      : '',
  }));
}

function createMessageId() {
  if (typeof globalThis.crypto?.randomUUID === 'function') return globalThis.crypto.randomUUID().replaceAll('-', '');
  return `msg_${Date.now()}_${Math.random().toString(36).slice(2, 12)}`;
}

function formatMessageTime(value) {
  const date = new Date(Number(value || Date.now()));
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date);
}

function updateFeedbackHistory(messageId, feedback) {
  const stored = history.find((entry) => entry.messageId === messageId);
  if (stored) stored.feedback = feedback;
  saveHistory();
}

function syncFeedbackButtons(article, rating) {
  article.querySelectorAll('.message-action').forEach((button) => {
    const selected = button.dataset.rating === rating;
    button.classList.toggle('is-selected', selected);
    button.setAttribute('aria-pressed', String(selected));
  });
}

function closeFeedback() {
  if (feedbackBackdrop.hidden || feedbackRequestPending) return;
  feedbackBackdrop.hidden = true;
  activeFeedback = null;
  feedbackTags.replaceChildren();
  feedbackDetail.value = '';
  feedbackError.hidden = true;
  feedbackCancel.hidden = true;
  feedbackCancel.disabled = false;
  feedbackCancel.textContent = '取消反馈';
  feedbackSubmit.disabled = false;
  delete document.body.dataset.modal;
  focusBeforeModal?.focus();
}

function showFailureNotice(key, message, recoveryActions = [], protocol = {}) {
  const noticeKey = String(key || '').trim();
  if (!noticeKey || activeFailureNotices.has(noticeKey)) return null;
  const item = {
    message, variant: 'error', recoveryActions, noticeKey, ...protocolFields(protocol),
  };
  activeFailureNotices.set(noticeKey, item);
  return addMessage(item, false);
}

function resolveFailureNotice(key) {
  const noticeKey = String(key || '').trim();
  if (!noticeKey) return;
  activeFailureNotices.delete(noticeKey);
  chat.querySelectorAll('[data-notice-key]').forEach((article) => {
    if (article.dataset.noticeKey === noticeKey) article.remove();
  });
  if (!history.length) empty.hidden = false;
}

function setFeedbackPending(pending) {
  feedbackRequestPending = pending;
  feedbackClose.disabled = pending;
  feedbackCancel.disabled = pending;
  feedbackSubmit.disabled = pending;
}

function renderFeedbackTags() {
  feedbackTags.replaceChildren();
  FEEDBACK_OPTIONS[activeFeedback.rating].forEach(([value, label]) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'feedback-tag';
    button.textContent = label;
    button.dataset.value = value;
    button.setAttribute('aria-pressed', String(activeFeedback.tags.has(value)));
    button.addEventListener('click', () => {
      if (activeFeedback.tags.has(value)) activeFeedback.tags.delete(value);
      else activeFeedback.tags.add(value);
      button.setAttribute('aria-pressed', String(activeFeedback.tags.has(value)));
    });
    feedbackTags.append(button);
  });
}

function openFeedback(item, article, rating) {
  focusBeforeModal = document.activeElement;
  const prior = item.feedback?.rating === rating ? item.feedback : null;
  activeFeedback = {
    item, article, rating, tags: new Set(prior?.tags || []),
  };
  feedbackSubtitle.textContent = rating === 'positive'
    ? '告诉我们这条回复哪里做得好'
    : '告诉我们这条回复哪里需要改进';
  feedbackDetail.value = prior?.detail || '';
  feedbackError.hidden = true;
  feedbackCancel.hidden = !prior;
  feedbackCancel.disabled = false;
  feedbackCancel.textContent = '取消反馈';
  feedbackSubmit.disabled = false;
  feedbackSubmit.textContent = prior ? '更新反馈' : '提交';
  renderFeedbackTags();
  feedbackBackdrop.hidden = false;
  document.body.dataset.modal = 'feedback';
  feedbackDetail.focus();
}

function createMessageActions(item, article) {
  const actions = document.createElement('div');
  actions.className = 'message-actions';
  actions.setAttribute('aria-label', '回复反馈');
  [
    ['positive', 'thumb-up.svg', '赞，这条回复有帮助'],
    ['negative', 'thumb-down.svg', '踩，这条回复需要改进'],
  ].forEach(([rating, icon, label]) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'message-action';
    button.dataset.rating = rating;
    button.setAttribute('aria-label', label);
    button.setAttribute('aria-pressed', 'false');
    const image = document.createElement('img');
    image.src = `/assets/icons/${icon}`;
    image.alt = '';
    button.append(image);
    button.addEventListener('click', () => openFeedback(item, article, rating));
    actions.append(button);
  });
  const time = document.createElement('time');
  time.className = 'message-time';
  time.dateTime = new Date(item.createdAt).toISOString();
  time.textContent = formatMessageTime(item.createdAt);
  actions.append(time);
  syncFeedbackButtons(actions, item.feedback?.rating || '');
  return actions;
}

function createRecoveryActions(actions, item = {}) {
  const retryAction = normalizeRetryAction(item.retryAction);
  const values = normalizeRecoveryActions(actions).filter((action) => (
    action !== 'retry_request' || retryAction
  ));
  if (!values.length) return null;
  const host = document.createElement('div');
  host.className = 'message-recovery-actions';
  values.forEach((action) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'message-recovery';
    button.textContent = RECOVERY_ACTION_LABELS[action];
    const childBinding = recoveryChildActionBinding(action, retryAction, item);
    const childAction = childBinding?.action || '';
    const childActionTarget = childBinding?.target || null;
    if (childBinding) {
      bindChildActionButton(button, childAction, childActionTarget);
      const allowed = taskStateAllowsChildAction(childAction, childActionTarget);
      button.disabled = !allowed;
      button.hidden = !allowed;
    }
    button.addEventListener('click', () => {
      if (action === 'relogin') window.location.assign('/invite');
      else if (action === 'reupload') fileInput.click();
      else if (action === 'new_chat') resetConversation();
      else if (action === 'retry_connection') retryConnection();
      else if (action === 'retry_request') retryTextAction(retryAction, childActionTarget);
      else if (action === 'retry_search' && taskStateAllowsChildAction(action, childActionTarget)) {
        sendTextValue('重试', '重试', childBinding.actionContext, childActionTarget);
      }
    });
    host.append(button);
  });
  return host;
}

function normalizeAuthorContact(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const label = String(raw.label || '').trim();
  const channel = String(raw.channel || '').trim();
  const value = String(raw.value || '').trim();
  if (!label || !channel || !value) return null;
  return { label, channel, value };
}

function closeAuthorContact() {
  if (authorContactBackdrop.hidden) return;
  authorContactBackdrop.hidden = true;
  authorContactCopy.textContent = '复制微信号';
  delete document.body.dataset.modal;
  focusBeforeModal?.focus();
}

function openAuthorContact(raw) {
  const contact = normalizeAuthorContact(raw);
  if (!contact) return;
  focusBeforeModal = document.activeElement;
  authorContactChannel.textContent = `作者${contact.channel}：`;
  authorContactValue.textContent = contact.value;
  authorContactCopy.dataset.value = contact.value;
  authorContactCopy.textContent = `复制${contact.channel}号`;
  authorContactBackdrop.hidden = false;
  document.body.dataset.modal = 'author-contact';
  authorContactCopy.focus();
}

function createAuthorContactAction(raw) {
  const contact = normalizeAuthorContact(raw);
  if (!contact) return null;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'a3-unit-choice message-author-contact';
  button.textContent = contact.label;
  button.addEventListener('click', () => openAuthorContact(contact));
  return button;
}

async function copyAuthorContact() {
  const value = String(authorContactCopy.dataset.value || '').trim();
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    authorContactCopy.textContent = '已复制';
  } catch (_error) {
    const input = document.createElement('textarea');
    input.value = value;
    input.setAttribute('readonly', '');
    input.style.position = 'fixed';
    input.style.opacity = '0';
    document.body.append(input);
    input.select();
    const copied = document.execCommand('copy');
    input.remove();
    authorContactCopy.textContent = copied ? '已复制' : '复制失败，请手动复制';
  }
}

function createA3UnitActions(rawA3, item = {}) {
  const a3 = normalizeA3Snapshot(rawA3);
  if (!a3 || !a3.units.length) return null;
  const workflowTarget = {
    workflowId: String(item.workflowId || ''),
    workflowRevision: Number(item.workflowRevision || 0),
  };
  if (a3.phase === 'A2_ACTIVE') {
    const host = document.createElement('div');
    host.className = 'a3-unit-actions';
    host.dataset.a3Revision = String(a3.task_revision || 0);
    host.dataset.a3Phase = a3.phase;
    host.dataset.workflowId = workflowTarget.workflowId;
    host.dataset.workflowRevision = String(workflowTarget.workflowRevision);
    const switchButton = document.createElement('button');
    switchButton.type = 'button';
    switchButton.className = 'a3-unit-choice a3-switch-question';
    switchButton.textContent = '换题重新搜';
    bindA3UnitNavigationControl(switchButton, workflowTarget, { hideWhenDenied: true });
    const allowed = taskStateAllowsA3UnitNavigation(workflowTarget, a3);
    switchButton.disabled = !allowed;
    switchButton.hidden = !allowed;
    switchButton.addEventListener('click', () => {
      if (!taskStateAllowsA3UnitNavigation(workflowTarget, a3Current())) return;
      openA3Sheet(workflowTarget);
    });
    host.append(switchButton);
    return host;
  }
  if (a3.auto_crop_enabled && a3.phase === 'WAIT_UNIT_SELECTION') {
    const host = document.createElement('div');
    host.className = 'a3-unit-actions';
    host.dataset.a3Revision = String(a3.task_revision || 0);
    host.dataset.a3Phase = a3.phase;
    host.dataset.workflowId = workflowTarget.workflowId;
    host.dataset.workflowRevision = String(workflowTarget.workflowRevision);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'a3-unit-choice a3-open-auto-selection';
    const prepared = a3.units.filter((unit) => unit.requested && !unit.completed && !unit.searched).length;
    button.textContent = prepared ? `查看已准备题目（${prepared}）` : '选择要查询的题目';
    bindA3UnitNavigationControl(button, workflowTarget, { hideWhenDenied: true });
    const allowed = taskStateAllowsA3UnitNavigation(workflowTarget, a3);
    button.disabled = !allowed;
    button.hidden = !allowed;
    button.addEventListener('click', () => {
      if (!taskStateAllowsA3UnitNavigation(workflowTarget, a3Current())) return;
      openA3Sheet(workflowTarget);
    });
    host.append(button);
    return host;
  }
  if (!['WAIT_UNIT_SELECTION', 'CROP_REQUIRED', 'COMPLETE'].includes(a3.phase)) return null;
  const host = document.createElement('div');
  host.className = 'a3-unit-actions';
  host.dataset.a3Revision = String(a3.task_revision || 0);
  host.dataset.a3Phase = a3.phase;
  host.dataset.workflowId = workflowTarget.workflowId;
  host.dataset.workflowRevision = String(workflowTarget.workflowRevision);
  a3.units.forEach((unit) => {
    const completed = Boolean(unit.completed);
    const searched = Boolean(unit.searched);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `a3-unit-choice${completed ? ' is-complete' : ''}`;
    button.dataset.a3UnitId = unit.unit_id;
    button.dataset.a3Revision = String(a3.task_revision || 0);
    const actionTarget = { ...workflowTarget, unitId: unit.unit_id };
    bindWorkflowActionControl(button, 'select_unit', actionTarget);
    button.disabled = !taskStateAllowsA3Action('select_unit', actionTarget, a3);
    if (completed) {
      button.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="m5 12 4 4L19 6"/></svg>';
      const label = document.createElement('span');
      label.textContent = `${unit.display_label} · 已完成`;
      button.append(label);
    } else if (searched) {
      button.textContent = `${unit.display_label || '未标号题目'} · 已检索`;
    } else {
      button.textContent = unit.display_label || '未标号题目';
    }
    button.addEventListener('click', () => selectA3Unit(actionTarget));
    host.append(button);
  });
  if (a3.phase === 'CROP_REQUIRED' && a3.selected_unit?.unit_id) {
    host.append(createA3ContinueCropButton({
      ...workflowTarget,
      unitId: a3.selected_unit.unit_id,
    }, a3));
  }
  return host;
}

function createA3ContinueCropButton(actionTarget, a3 = a3Current()) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'a3-unit-choice a3-continue-crop';
  button.textContent = '继续裁剪';
  bindWorkflowActionControl(button, 'submit_crop', actionTarget, { hideWhenDenied: true });
  const allowed = taskStateAllowsA3Action('submit_crop', actionTarget, a3);
  button.disabled = !allowed;
  button.hidden = !allowed;
  button.addEventListener('click', () => {
    if (!taskStateAllowsA3Action('submit_crop', actionTarget)) return;
    openA3Crop(actionTarget, { force: true });
  });
  return button;
}

async function submitFeedback() {
  if (!activeFeedback || feedbackRequestPending) return;
  const context = activeFeedback;
  setFeedbackPending(true);
  feedbackSubmit.textContent = '正在提交…';
  feedbackError.hidden = true;
  try {
    const payload = {
      message_id: context.item.messageId,
      rated_response_id: context.item.responseId,
      rating: context.rating,
      tags: Array.from(context.tags),
      detail: feedbackDetail.value.trim(),
    };
    const conversation = feedbackConversation(context.item.messageId);
    if (conversation) payload.conversation = conversation;
    await request('/api/feedback', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    }, 8000, '反馈提交超时，请稍后重试。', false, '暂时无法提交反馈，请检查网络。');
    const feedback = { rating: payload.rating, tags: payload.tags, detail: payload.detail };
    context.item.feedback = feedback;
    updateFeedbackHistory(context.item.messageId, feedback);
    syncFeedbackButtons(context.article, payload.rating);
    setFeedbackPending(false);
    closeFeedback();
    setStatus('ready', '感谢你的反馈');
    setTimeout(() => { if (!isBusy) setStatus('ready', '准备就绪'); }, 2200);
  } catch (error) {
    feedbackError.textContent = error.message || '反馈提交失败，请稍后重试。';
    feedbackError.hidden = false;
    setFeedbackPending(false);
    feedbackSubmit.textContent = '重新提交';
    setStatus('error', '反馈提交失败，可重新提交');
  }
}

async function cancelFeedback() {
  if (!activeFeedback || feedbackRequestPending || feedbackCancel.hidden) return;
  const context = activeFeedback;
  setFeedbackPending(true);
  feedbackCancel.textContent = '正在取消…';
  feedbackError.hidden = true;
  try {
    await request(`/api/feedback/${encodeURIComponent(context.item.responseId)}`, {
      method: 'DELETE',
    }, 8000, '取消反馈超时，请稍后重试。', false, '暂时无法取消反馈，请检查网络。');
    context.item.feedback = null;
    updateFeedbackHistory(context.item.messageId, null);
    syncFeedbackButtons(context.article, '');
    setFeedbackPending(false);
    closeFeedback();
    setStatus('ready', '反馈已取消');
    setTimeout(() => { if (!isBusy) setStatus('ready', '准备就绪'); }, 2200);
  } catch (error) {
    feedbackError.textContent = error.message || '取消反馈失败，请稍后重试。';
    feedbackError.hidden = false;
    setFeedbackPending(false);
    feedbackCancel.textContent = '重新取消';
    setStatus('error', '取消反馈失败，可重新取消');
  }
}

function createMediaCard(url, index, item) {
  const card = document.createElement('figure');
  card.className = 'media-card';
  const openButton = document.createElement('button');
  openButton.type = 'button';
  openButton.className = 'media-open';
  openButton.setAttribute('aria-label', `查看${item.imageAlt}${item.images.length > 1 ? ` ${index + 1}` : ''}大图`);
  const image = document.createElement('img');
  image.src = url;
  image.alt = item.imageAlt;
  image.loading = 'eager';
  image.addEventListener('load', scrollToLatest, { once: true });
  image.addEventListener('error', () => {
    releaseObjectUrl(url);
    const note = document.createElement('span');
    note.className = 'expired-image';
    note.textContent = '图片已失效，请重新上传';
    openButton.replaceWith(note);
    const candidateButton = card.querySelector('.select-candidate');
    if (candidateButton) {
      candidateButton.dataset.mediaAvailable = 'false';
      candidateButton.disabled = true;
      candidateButton.textContent = '候选已失效';
    }
  }, { once: true });
  openButton.append(image);
  openButton.addEventListener('click', () => openLightbox(image.currentSrc || image.src, item.imageAlt));
  card.append(openButton);

  const kind = mediaKind(item);
  if (kind === 'candidate') {
    const badge = document.createElement('span');
    badge.className = 'candidate-index';
    badge.textContent = String(index + 1);
    card.append(badge);
    const footer = document.createElement('figcaption');
    footer.className = 'media-footer';
    const label = document.createElement('span');
    const originalLabel = String(item.a3?.selected_unit?.display_label || '').trim();
    label.textContent = originalLabel ? `${originalLabel} · 候选 ${index + 1}` : `候选 ${index + 1}`;
    const choose = document.createElement('button');
    choose.type = 'button';
    choose.className = 'select-candidate';
    const { actionContext, actionTarget } = candidateChildActionBinding(item, index);
    bindChildActionButton(choose, 'select_candidate', actionTarget);
    choose.dataset.mediaAvailable = 'true';
    const isCurrent = taskStateAllowsChildAction('select_candidate', actionTarget);
    choose.disabled = !isCurrent;
    choose.textContent = isCurrent ? '选择' : '候选已失效';
    choose.addEventListener('click', () => {
      if (!taskStateAllowsChildAction('select_candidate', actionTarget)) return;
      sendTextValue(
        `选择候选 ${index + 1}`,
        `选择候选 ${index + 1}`,
        actionContext,
        actionTarget,
      );
    });
    footer.append(label, choose);
    card.append(footer);
  }
  return card;
}

function addMessage(item, persist = true) {
  item = { ...item, createdAt: Number(item.createdAt || Date.now()) };
  const noticeKey = String(item.noticeKey || '').trim();
  const retryAction = normalizeRetryAction(item.retryAction);
  if (
    persist
    && (
      normalizeRecoveryActions(item.recoveryActions).includes('retry_search')
      || ['select_candidate', 'retry_search'].includes(retryAction?.actionContext?.type)
    )
    && !item.childTaskId
  ) {
    const childTarget = currentChildActionTarget();
    if (childTarget) item = { ...item, ...childTarget };
  }
  const inferredAuthorContact = normalizeAuthorContact(item.authorContact)
    || (!item.me && String(item.message || '').includes('联系作者手搓')
      ? AUTHOR_CONTACT_FALLBACK
      : null);
  if (inferredAuthorContact) item = { ...item, authorContact: inferredAuthorContact };
  const feedbackEligible = !item.me && item.variant !== 'pending' && Boolean(item.responseId);
  if (feedbackEligible) {
    item = {
      ...item,
      messageId: item.messageId || createMessageId(),
      createdAt: item.createdAt,
    };
  }
  if (!noticeKey) empty.hidden = true;
  const article = document.createElement('article');
  article.className = `message${item.me ? ' user' : ''}${item.variant ? ` ${item.variant}` : ''}`;
  if (noticeKey) article.dataset.noticeKey = noticeKey;
  if (item.variant === 'error') article.setAttribute('role', 'alert');
  if (!item.me) {
    const avatar = document.createElement('div');
    avatar.className = 'message-avatar';
    avatar.setAttribute('aria-hidden', 'true');
    avatar.textContent = '力';
    article.append(avatar);
  }
  const content = document.createElement('div');
  content.className = 'message-content';
  const paragraph = document.createElement('p');
  paragraph.className = 'message-text';
  paragraph.textContent = item.message || '';
  if (item.variant === 'pending') {
    const dots = document.createElement('span');
    dots.className = 'typing-dots';
    dots.setAttribute('aria-hidden', 'true');
    dots.innerHTML = '<i></i><i></i><i></i>';
    paragraph.append(dots);
  }
  content.append(paragraph);

  const images = Array.isArray(item.images) ? item.images : [];
  if (images.length) {
    if (mediaKind(item) === 'answer') {
      const answerLabel = document.createElement('span');
      answerLabel.className = 'answer-label';
      const originalLabel = String(item.a3?.selected_unit?.display_label || '').trim();
      answerLabel.textContent = originalLabel ? `${originalLabel} · 题库答案` : '题库答案';
      content.append(answerLabel);
    }
    const grid = document.createElement('div');
    grid.className = 'media-grid';
    images.forEach((url, index) => grid.append(createMediaCard(url, index, { ...item, images })));
    content.append(grid);
  }
  const a3Actions = createA3UnitActions(item.a3, item);
  const recoveryActions = createRecoveryActions(item.recoveryActions, item);
  const authorContactAction = createAuthorContactAction(item.authorContact);
  if (a3Actions && authorContactAction) a3Actions.append(authorContactAction);
  if (a3Actions) content.append(a3Actions);
  if (recoveryActions) content.append(recoveryActions);
  if (authorContactAction && !a3Actions) {
    const contactActions = document.createElement('div');
    contactActions.className = 'message-recovery-actions';
    contactActions.append(authorContactAction);
    content.append(contactActions);
  }
  if (feedbackEligible) content.append(createMessageActions(item, article));
  article.append(content);
  chat.append(article);
  syncTaskStateActionButtons();
  if (persist) remember(item);
  scrollToLatest();
  return article;
}

function renderHistory() {
  chat.replaceChildren();
  empty.hidden = history.length > 0;
  history.forEach((item) => addMessage({ ...item, images: (item.images || []).filter(isPersistentImage) }, false));
  activeFailureNotices.forEach((item) => addMessage(item, false));
}

function isLegacyInlineOnlyMessage(item, index, messages) {
  if (A3_INLINE_ONLY_INTENTS.has(String(item?.intent || ''))) return true;
  if (!item?.me) return false;
  const next = messages[index + 1];
  if (!['a3_unit_selected', 'a3_unit_already_selected'].includes(String(next?.intent || ''))) return false;
  const label = String(next?.a3?.selected_unit?.display_label || '').trim();
  return Boolean(label) && item.message === `选择${label}`;
}

function restoreHistory() {
  restoreA3CropHistoryState();
  try {
    const currentRaw = safeLocalStorageGet(HISTORY_KEY);
    const legacyRaw = currentRaw ? null : safeLocalStorageGet(LEGACY_HISTORY_KEY);
    if (currentRaw === undefined || legacyRaw === undefined) {
      throw new Error('browser storage is unavailable');
    }
    const raw = currentRaw || legacyRaw;
    const stored = JSON.parse(raw || 'null');
    const activityAt = Number(stored?.lastActivityAt ?? stored?.savedAt);
    const now = Date.now();
    if (!stored) {
      clearHistory();
      return;
    }
    if (!Array.isArray(stored.messages) || !Number.isFinite(activityAt) || activityAt > now + 60000) {
      clearHistory();
      pendingHistoryStorageNotice = '浏览器中的临时对话无法读取，已为你开始新对话。请检查浏览器存储设置。';
      return;
    }
    if (now - activityAt >= HISTORY_TTL_MS) {
      clearHistory({ preserveStoredHistory: true });
      sessionResetRequired = true;
      sessionResetActivityAt = activityAt;
      pendingSessionExpiredNotice = true;
      return;
    }
    historyLastActivityAt = activityAt;
    const storedMessages = stored.messages.slice(-HISTORY_LIMIT);
    const restoredMessages = storedMessages.filter((item, index) => (
      !isLegacyInlineOnlyMessage(item, index, storedMessages)
      && !OPERATIONAL_NOTICE_KEYS.has(String(item?.noticeKey || '').trim())
      && !(
        item?.variant === 'error'
        && item?.code === 'MEDIA_NOT_FOUND'
        && item?.message === LEGACY_EXPIRED_MEDIA_MESSAGE
      )
    ));
    history = restoredMessages.map((item) => {
      if (item.me || item.variant) return item;
      return {
        ...item,
        messageId: item.messageId || createMessageId(),
        createdAt: Number(item.createdAt || activityAt),
      };
    });
    activeFailureNotices.clear();
    safeLocalStorageRemove(LEGACY_HISTORY_KEY);
    saveHistory();
    renderHistory();
  } catch (_error) {
    clearHistory();
    pendingHistoryStorageNotice = '浏览器中的临时对话无法读取，已为你开始新对话。请检查浏览器存储设置。';
  }
}

function flushStartupNotices() {
  if (pendingHistoryStorageNotice) {
    const message = pendingHistoryStorageNotice;
    pendingHistoryStorageNotice = '';
    showFailureNotice('history-storage', message);
  }
  if (pendingSessionExpiredNotice) {
    pendingSessionExpiredNotice = false;
    showSessionExpiredNotice();
  }
}

async function repairUploadedImageHistory() {
  try {
    refreshHistoryActivityFromStorage();
    let resetRequired = sessionResetRequired;
    let data;
    if (resetRequired) {
      if (!sessionResetCoordinationAvailable()) {
        flushStartupNotices();
        showFailureNotice(
          'connection',
          '旧对话已经过期，请点击开始新对话后继续。',
          ['new_chat'],
        );
        return false;
      }
      const lockedResult = await withSessionRequestLock(
        '/api/reset',
        async (sessionRequestFence) => {
          refreshHistoryActivityFromStorage();
          if (!sessionResetRequired) {
            return {
              reset: false,
              data: await request(
                '/api/session', {}, SESSION_BOOTSTRAP_TIMEOUT_MS, '会话恢复超时。', false, '', true,
                sessionRequestFence,
              ),
            };
          }
          return {
            reset: true,
            data: await request(
              '/api/reset',
              { method: 'POST' },
              5000,
              '新对话创建超时。',
              false,
              '',
              true,
              sessionRequestFence,
            ),
          };
        },
        { requireSupport: true },
      );
      if (!lockedResult) return false;
      if (lockedResult.coordinationFailed) {
        flushStartupNotices();
        showFailureNotice(
          'connection',
          '浏览器无法安全同步新会话，请点击开始新对话后继续。',
          ['new_chat'],
        );
        return false;
      }
      resetRequired = lockedResult.reset;
      data = lockedResult.data;
    } else {
      data = await request(
        '/api/session', {}, SESSION_BOOTSTRAP_TIMEOUT_MS, '会话恢复超时。', false,
      );
    }
    const accepted = resetRequired ? applyResetSessionContext(data) : updateSessionContext(data);
    if (!accepted) {
      throw clientProtocolError(
        '会话返回格式异常，请重新连接。',
        'RESPONSE_INVALID',
        createRequestId(),
        ['retry_connection'],
      );
    }
    resolveFailureNotice('connection');
    resolveFailureNotice('session-recovery');
    if (!isBusy) setStatus('ready', '准备就绪');
    if (resetRequired) {
      clearHistory();
      renderHistory();
      flushStartupNotices();
      return true;
    }
    renderHistory();
    if (!data.session?.session_valid) {
      if (history.length) {
        clearHistory();
        renderHistory();
        showSessionExpiredNotice();
      } else {
        flushStartupNotices();
      }
      return true;
    }
    flushStartupNotices();
    if (!isPersistentImage(data.uploaded_image)) return true;
    for (let index = history.length - 1; index >= 0; index -= 1) {
      const item = history[index];
      if (item.me && item.message === '我发了一张题图。' && (!Array.isArray(item.images) || !item.images.length)) {
        item.images = [data.uploaded_image];
        saveHistory();
        renderHistory();
        return true;
      }
    }
    return true;
  } catch (error) {
    flushStartupNotices();
    const coordinationFailed = ['RESPONSE_INVALID', 'STALE_ACTION'].includes(
      String(error?.code || ''),
    ) && Boolean(storedSessionRequestFenceId());
    if (coordinationFailed) {
      resolveFailureNotice('connection');
      setStatus('error', '等待重新连接');
      showFailureNotice(
        'session-recovery',
        '连接暂时不稳定，上次请求结果尚待确认。当前对话已保留，请重新连接完成确认。',
        ['retry_connection'],
        {
          status: 'ERROR', layer: 'session', code: 'STALE_ACTION', retryable: true,
          action: 'retry_connection', request_id: createRequestId(), search_id: sessionContext.search_id || '',
        },
      );
    } else {
      setStatus('error', '暂时无法连接');
      showFailureNotice(
        'connection',
        '暂时无法连接服务。当前对话仍保留在本机，请检查网络后重新连接。',
        ['retry_connection'],
        { status: 'ERROR', layer: 'network', code: 'NETWORK_UNAVAILABLE', retryable: true, action: 'retry_connection', request_id: createRequestId(), search_id: sessionContext.search_id || '' },
      );
    }
    return false;
  }
}

function runSessionBootstrap() {
  if (sessionBootstrapPending && sessionBootstrap) return sessionBootstrap;
  sessionBootstrapPending = true;
  const pending = repairUploadedImageHistory().finally(() => {
    if (sessionBootstrap === pending) sessionBootstrapPending = false;
  });
  sessionBootstrap = pending;
  return pending;
}

async function sessionTaskStartAllowed() {
  refreshHistoryActivityFromStorage();
  const pending = sessionBootstrap || runSessionBootstrap();
  if (!(await pending)) return false;
  if (!sessionRequestLockAvailable()) {
    setStatus('error', '当前浏览器不支持安全会话');
    showFailureNotice(
      'connection',
      '当前浏览器无法安全协调多个页面，请升级浏览器或改用最新版 Chrome、Edge 后重试。',
      ['retry_connection'],
      { status: 'ERROR', layer: 'session', code: 'RESPONSE_INVALID', retryable: true, action: 'retry_connection', request_id: createRequestId(), search_id: sessionContext.search_id || '' },
    );
    return false;
  }
  if (!sessionResetRequired) return true;
  setStatus('error', '需要先重新建立会话');
  showFailureNotice(
    'connection',
    '旧对话已经过期，请先重新连接并建立新会话。',
    ['retry_connection'],
    { status: 'ERROR', layer: 'network', code: 'NETWORK_UNAVAILABLE', retryable: true, action: 'retry_connection', request_id: createRequestId(), search_id: '' },
  );
  return false;
}

function clearA3WorkflowState() {
  a3SourceUrl = '';
  a3SourceWorkflowKey = '';
  a3Bounds = null;
  a3LocalDrafts.clear();
  a3PrepareSelection.clear();
  a3DismissedKey = '';
  a3KnownWorkflowKey = '';
  clearA3MediaElementSources();
  closeA3TransientUi();
  clearA3CropHistoryState();
  closeLightbox();
}

function clearHistory({ preserveStoredHistory = false } = {}) {
  history = [];
  activeFailureNotices.clear();
  historyLastActivityAt = 0;
  if (historyExpiryTimer !== null) clearTimeout(historyExpiryTimer);
  historyExpiryTimer = null;
  clearPendingUpload();
  releaseAllObjectUrls();
  clearA3WorkflowState();
  if (!preserveStoredHistory) {
    safeLocalStorageRemove(HISTORY_KEY);
    safeLocalStorageRemove(LEGACY_HISTORY_KEY);
  }
}

function retireSessionForExternalReset() {
  const controller = activeController;
  activeController = null;
  operationVersion += 1;
  if (controller) controller.abort('session-reset');
  invalidateTaskStateContext();
  setBusy(false);
  clearHistory({ preserveStoredHistory: true });
  sessionResetRequired = false;
  sessionResetActivityAt = 0;
  pendingSessionExpiredNotice = false;
  sessionContext = {
    session_valid: false, phase: 'IDLE', has_active_image: false,
    task_revision: 0, candidate_generation: '', candidate_count: 0, search_id: '', a3: null,
    a3WorkflowId: '', a3WorkflowRevision: 0,
  };
  renderHistory();
  closeLightbox();
  setStatus('ready', '会话已在另一页面重置');
}

function expireHistoryIfNeeded() {
  if (!history.length || !Number.isFinite(historyLastActivityAt) || historyLastActivityAt <= 0) return false;
  if (refreshHistoryActivityFromStorage()) return false;
  if (Date.now() - historyLastActivityAt < HISTORY_TTL_MS) {
    scheduleHistoryExpiry();
    return false;
  }
  const expiredActivityAt = historyLastActivityAt;
  const controller = activeController;
  activeController = null;
  operationVersion += 1;
  if (controller) controller.abort('history-expired');
  invalidateTaskStateContext();
  setBusy(false);
  clearHistory({ preserveStoredHistory: true });
  sessionResetRequired = true;
  sessionResetActivityAt = expiredActivityAt;
  renderHistory();
  sessionContext = {
    session_valid: false, phase: 'IDLE', has_active_image: false,
    task_revision: 0, candidate_generation: '', candidate_count: 0, search_id: '', a3: null,
    a3WorkflowId: '', a3WorkflowRevision: 0,
  };
  closeLightbox();
  showSessionExpiredNotice();
  return true;
}

function showSessionExpiredNotice() {
  renderHistory();
  setStatus('ready', '准备就绪');
}

function replacePending(row, item) {
  if (row?.isConnected) row.remove();
  addMessage(item);
}

function updatePendingMessage(row, message) {
  const paragraph = row?.querySelector('.message-text');
  if (!paragraph) return;
  const textNode = Array.from(paragraph.childNodes).find((node) => node.nodeType === Node.TEXT_NODE);
  if (textNode) textNode.nodeValue = message;
  else paragraph.prepend(document.createTextNode(message));
  scrollToLatest();
}

function setStatus(state, message) {
  runtimeStatus.dataset.state = state;
  statusText.textContent = message;
}

function resizeComposer() {
  textInput.style.height = 'auto';
  textInput.style.height = `${Math.min(textInput.scrollHeight, 160)}px`;
}

function updateComposer() {
  sendButton.disabled = isBusy || !textInput.value.trim();
}

function refocusComposerOnDesktop() {
  if (window.matchMedia('(hover: hover) and (pointer: fine)').matches) {
    textInput.focus({ preventScroll: true });
  }
}

function setBusy(value) {
  isBusy = value;
  textInput.disabled = value;
  fileInput.disabled = value;
  form.setAttribute('aria-busy', String(value));
  updateComposer();
  syncTaskStateActionButtons();
  if (!a3CropWorkspace.hidden) renderA3Selection();
}

function validateImage(file) {
  if (!file) return '没有读取到图片，请重新选择。';
  const name = String(file.name || '');
  const extension = name.includes('.') ? name.split('.').pop().toLowerCase() : '';
  const normalizedType = String(file.type || '').toLowerCase();
  const ambiguousType = !normalizedType || normalizedType === 'application/octet-stream';
  if (!ALLOWED_TYPES.has(normalizedType) && (!ambiguousType || (extension && !ALLOWED_EXTENSIONS.has(extension)))) {
    return '图片格式不支持，请上传 PNG、JPG、WEBP、GIF 或 BMP 图片。';
  }
  if (file.size > MAX_IMAGE_BYTES) return '图片太大，请上传不超过 15MB 的图片。';
  return '';
}

function debugUploadMetadata(stage, value, filename = '') {
  console.debug('[image-upload]', stage, {
    name: value?.name || filename || '',
    type: value?.type || '',
    size: Number(value?.size || 0),
  });
}

function imageFromObjectUrl(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('图片格式不支持，浏览器无法读取该图片。'));
    image.src = url;
  });
}

async function normalizeImage(selected, sourceUrl) {
  debugUploadMetadata('selected', selected);
  const image = await imageFromObjectUrl(sourceUrl);
  if (!image.naturalWidth || !image.naturalHeight) throw new Error('裁剪处理失败，请重新选择图片。');
  const encode = async (maxDimension, qualities) => {
    const scale = Math.min(1, maxDimension / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
    const context = canvas.getContext('2d');
    if (!context) throw new Error('裁剪处理失败，请重新选择图片。');
    context.fillStyle = '#fff';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    for (const quality of qualities) {
      const encoded = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality));
      if (!encoded) throw new Error('裁剪处理失败，请重新选择图片。');
      debugUploadMetadata('encoded', encoded, `dimension=${canvas.width}x${canvas.height};quality=${quality}`);
      if (encoded.size <= IMAGE_TARGET_BYTES || quality === qualities[qualities.length - 1]) return encoded;
    }
    return null;
  };
  let blob = await encode(IMAGE_MAX_DIMENSION, IMAGE_QUALITY_STEPS);
  if (blob && blob.size > IMAGE_TARGET_BYTES) {
    blob = await encode(IMAGE_FALLBACK_DIMENSION, IMAGE_QUALITY_STEPS.slice(1));
  }
  if (!blob || blob.size > MAX_IMAGE_BYTES) throw new Error('图片太大，请上传不超过 15MB 的图片。');
  const filename = `cropped_${Date.now()}.jpg`;
  const preview = URL.createObjectURL(blob);
  objectUrls.add(preview);
  debugUploadMetadata('normalized', blob, filename);
  return { blob, filename, preview };
}

function safeHttpError(status, data, requestId = '') {
  const rawDetail = typeof data?.detail === 'string' ? data.detail : '';
  const detail = rawDetail.toLowerCase();
  const protocol = { ...data, request_id: data?.request_id || requestId };
  const action = protocolRecoveryAction(data?.action);
  if (data?.status) {
    const messages = {
      UPLOAD_REQUIRED: '没有读取到图片，请重新选择。',
      UPLOAD_TOO_LARGE: '图片太大，请上传不超过 15MB 的图片。',
      UPLOAD_UNSUPPORTED_FORMAT: '图片格式不支持，请上传 PNG、JPG、WEBP、GIF 或 BMP 图片。',
      UPLOAD_DECODE_FAILED: '服务端无法读取该图片，请重新选择清晰、完整的题图。',
      MESSAGE_INVALID: '这条消息无法处理，请重新输入后提交。',
      FEEDBACK_INVALID: '反馈内容无法提交，请修改后重试。',
      FEEDBACK_TOO_LARGE: '反馈内容过长，请精简后重试。',
    };
    return new UserVisibleError(
      messages[data.code] || rawDetail || '这次请求没有处理成功，请稍后重试。',
      action ? [action] : [],
      { retryable: Boolean(data.retryable), protocol },
    );
  }
  if (status === 401) return new UserVisibleError('登录状态已失效，请重新登录。', ['relogin'], { retryable: false });
  if (status === 403) return new UserVisibleError('当前请求无权处理，请重新登录或联系管理员。', ['relogin'], { retryable: false });
  if (status === 429) return new UserVisibleError(rawDetail || '当前请求较多，请稍后再试。', ['retry_request']);
  if (status === 503 && (detail.includes('额度') || detail.includes('邀请码'))) {
    const actions = detail.includes('重新登录') ? ['relogin'] : [];
    return new UserVisibleError(rawDetail || '今日服务额度已用完，请明天再试。', actions, { retryable: false });
  }
  if (status === 413 || detail.includes('too large')) return new UserVisibleError('图片太大，请上传不超过 15MB 的图片。');
  if (status === 415 || detail.includes('unsupported image')) return new UserVisibleError('图片格式不支持，请上传 PNG、JPG、WEBP、GIF 或 BMP 图片。');
  if (status === 400 && detail.includes('invalid image')) return new UserVisibleError('服务端无法读取该图片，请检查图片后重试。');
  if (status >= 500) return new UserVisibleError('服务端处理失败，请稍后重试。', ['retry_request']);
  if (status === 400) return new UserVisibleError(
    '这次请求没有处理成功，请直接重试；如果仍然失败，请点踩并补充说明。',
    ['retry_request'],
  );
  return new UserVisibleError(`请求失败（HTTP ${status}），请稍后重试。`, ['retry_request']);
}

function streamedError(event) {
  const text = String(event?.message || event || '服务端处理失败，请稍后重试。');
  if (event?.status) {
    const action = protocolRecoveryAction(event.action);
    return new UserVisibleError(
      text,
      action ? [action] : [],
      { retryable: Boolean(event.retryable), protocol: event },
    );
  }
  const retryable = !text.includes('重新登录') && !text.includes('额度') && !text.includes('邀请码');
  const actions = text.includes('重新登录') ? ['relogin'] : retryable ? ['retry_request'] : [];
  return new UserVisibleError(text, actions, { retryable });
}

async function request(
  url,
  options,
  timeoutMs,
  timeoutMessage,
  track = true,
  networkMessage = '',
  sessionLockHeld = false,
  sessionRequestFence = null,
) {
  if (!sessionLockHeld && isTaskStateRequestPath(url)) {
    if (isTaskStartingPath(url)) touchSharedSessionActivity();
    return withSessionRequestLock(
      url,
      (requestFence) => request(
        url,
        options,
        timeoutMs,
        timeoutMessage,
        track,
        networkMessage,
        true,
        requestFence,
      ),
    );
  }
  if (taskStateApiPath(url) === '/api/reset') {
    preserveSessionRequestFence(sessionRequestFence);
  }
  const taskStateRequest = beginTaskStateRequest(url, 'json');
  const controller = new AbortController();
  const requestId = createRequestId();
  const headers = new Headers(options?.headers || {});
  headers.set('x-request-id', requestId);
  if (track) activeController = controller;
  const timer = setTimeout(() => controller.abort('timeout'), timeoutMs);
  try {
    const response = await fetch(url, { ...options, headers, signal: controller.signal });
    const contentType = response.headers.get('content-type') || '';
    let data = {};
    if (contentType.includes('application/json')) {
      try { data = await response.json(); } catch (_error) { data = {}; }
    } else {
      await response.text();
    }
    if (!response.ok) {
      consumeTaskStateResponse(taskStateRequest, data, { error: true });
      const emptyResetApplied = applyAuthoritativeEmptyError(
        url,
        data,
        { sessionRequestFence },
      );
      if (emptyResetApplied === false) throw sessionCoordinationError();
      if (emptyResetApplied === null) {
        resolveSessionRequestFenceFromEnvelope(data, sessionRequestFence);
      }
      throw safeHttpError(response.status, data, requestId);
    }
    if (!contentType.includes('application/json')) throw clientProtocolError('服务返回格式异常，请稍后重试。', 'RESPONSE_INVALID', requestId);
    consumeTaskStateResponse(taskStateRequest, data);
    if (!publishAuthoritativeReset(url, data, { sessionRequestFence })) {
      applyAuthoritativeEmptyAfterCoordinationFailure(data, sessionRequestFence);
      throw sessionCoordinationError();
    }
    resolveSessionRequestFenceFromEnvelope(data, sessionRequestFence);
    return data;
  } catch (error) {
    if (error.name === 'AbortError') {
      if (controller.signal.reason === 'new-chat') throw new UserVisibleError('当前识别已取消。');
      throw new UserVisibleError(timeoutMessage, ['retry_request'], {
        protocol: { status: 'ERROR', layer: 'network', code: 'REQUEST_TIMEOUT', retryable: true, action: 'retry_request', request_id: requestId },
      });
    }
    if (error instanceof UserVisibleError) throw error;
    if (error instanceof TypeError) throw new UserVisibleError(networkMessage || '无法连接服务，请检查网络后重试。', ['retry_request'], {
      protocol: { status: 'ERROR', layer: 'network', code: 'NETWORK_UNAVAILABLE', retryable: true, action: 'retry_request', request_id: requestId },
    });
    throw clientProtocolError('服务返回格式异常，请稍后重试。', 'RESPONSE_INVALID', requestId);
  } finally {
    finishTaskStateRequest(taskStateRequest);
    clearTimeout(timer);
    if (activeController === controller) activeController = null;
  }
}

async function requestStream(
  url,
  options,
  timeoutMs,
  timeoutMessage,
  onProgress,
  networkMessage = '',
  { renewTimeoutOnProgress = false } = {},
  sessionLockHeld = false,
  sessionRequestFence = null,
) {
  if (!sessionLockHeld && isTaskStateRequestPath(url)) {
    if (isTaskStartingPath(url)) touchSharedSessionActivity();
    return withSessionRequestLock(
      url,
      (requestFence) => requestStream(
        url,
        options,
        timeoutMs,
        timeoutMessage,
        onProgress,
        networkMessage,
        { renewTimeoutOnProgress },
        true,
        requestFence,
      ),
    );
  }
  const taskStateRequest = beginTaskStateRequest(url, 'stream');
  const controller = new AbortController();
  const requestId = createRequestId();
  const headers = new Headers(options?.headers || {});
  headers.set('x-request-id', requestId);
  activeController = controller;
  let timer;
  const renewTimeout = () => {
    clearTimeout(timer);
    timer = setTimeout(() => controller.abort('timeout'), timeoutMs);
  };
  renewTimeout();
  try {
    const response = await fetch(url, { ...options, headers, signal: controller.signal });
    if (!response.ok) {
      let data = {};
      try { data = await response.json(); } catch (_error) { data = {}; }
      consumeTaskStateResponse(taskStateRequest, data, { error: true });
      const emptyResetApplied = applyAuthoritativeEmptyError(
        url,
        data,
        { sessionRequestFence },
      );
      if (emptyResetApplied === false) throw sessionCoordinationError();
      if (emptyResetApplied === null) {
        resolveSessionRequestFenceFromEnvelope(data, sessionRequestFence);
      }
      throw safeHttpError(response.status, data, requestId);
    }
    if (!response.body) throw clientProtocolError('服务返回格式异常，请稍后重试。', 'RESPONSE_INVALID', requestId);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === 'progress') {
          if (renewTimeoutOnProgress) renewTimeout();
          onProgress?.(event);
        }
        if (event.type === 'result') {
          const terminalResult = event.data;
          consumeTaskStateResponse(taskStateRequest, terminalResult);
          if (!publishAuthoritativeReset(
            url,
            terminalResult,
            { sessionRequestFence },
          )) {
            applyAuthoritativeEmptyAfterCoordinationFailure(
              terminalResult,
              sessionRequestFence,
            );
            throw sessionCoordinationError();
          }
          resolveSessionRequestFenceFromEnvelope(terminalResult, sessionRequestFence);
          clearTimeout(timer);
          try { await reader.cancel(); } catch (_error) { /* terminal result already won */ }
          if (!terminalResult) throw clientProtocolError('服务返回格式异常，请稍后重试。', 'RESPONSE_INVALID', requestId);
          return terminalResult;
        }
        if (event.type === 'error') {
          consumeTaskStateResponse(taskStateRequest, event, { error: true });
          const emptyResetApplied = applyAuthoritativeEmptyError(
            url,
            event,
            { sessionRequestFence },
          );
          if (emptyResetApplied === false) throw sessionCoordinationError();
          if (emptyResetApplied === null) {
            resolveSessionRequestFenceFromEnvelope(event, sessionRequestFence);
          }
          throw streamedError(event);
        }
      }
      if (done) break;
    }
    throw clientProtocolError('服务返回格式异常，请稍后重试。', 'RESPONSE_INVALID', requestId);
  } catch (error) {
    if (error.name === 'AbortError') {
      if (controller.signal.reason === 'new-chat') throw new UserVisibleError('当前识别已取消。');
      throw new UserVisibleError(timeoutMessage, ['retry_request'], {
        protocol: { status: 'ERROR', layer: 'network', code: 'REQUEST_TIMEOUT', retryable: true, action: 'retry_request', request_id: requestId },
      });
    }
    if (error instanceof UserVisibleError) throw error;
    if (error instanceof TypeError) throw new UserVisibleError(networkMessage || '无法连接服务，请检查网络后重试。', ['retry_request'], {
      protocol: { status: 'ERROR', layer: 'network', code: 'NETWORK_UNAVAILABLE', retryable: true, action: 'retry_request', request_id: requestId },
    });
    throw clientProtocolError('服务返回格式异常，请稍后重试。', 'RESPONSE_INVALID', requestId);
  } finally {
    finishTaskStateRequest(taskStateRequest);
    clearTimeout(timer);
    if (activeController === controller) activeController = null;
  }
}

function responseItem(data) {
  const accepted = updateSessionContext(data);
  const childTarget = accepted ? (currentChildActionTarget() || {}) : {};
  const workflowTarget = accepted?.workflowTarget || {};
  const failure = data?.failure && typeof data.failure === 'object' ? data.failure : null;
  const protocol = protocolFields(data);
  const recoveryAction = protocolRecoveryAction(data?.action)
    || String(failure?.recovery_action || '');
  return {
    message: data.text || '处理完成。',
    me: false,
    images: data.images || [],
    imageAlt: data.intent === 'select_candidate' || data.intent === 'resend_answer' ? '题库答案' : '相似题候选',
    intent: data.intent || '',
    taskRevision: Number(data.session?.task_revision || 0),
    candidateCount: Number(data.session?.candidate_count || 0),
    candidateGeneration: String(data.session?.candidate_generation || ''),
    ...childTarget,
    ...workflowTarget,
    variant: protocol.status === 'ERROR' || failure
      ? 'error'
      : protocol.status === 'PARTIAL' ? 'partial' : '',
    recoveryActions: recoveryAction ? [recoveryAction] : [],
    authorContact: normalizeAuthorContact(data?.author_contact),
    messageId: createMessageId(),
    responseId: String(data.response_id || ''),
    createdAt: Date.now(),
    a3: accepted?.a3 || null,
    feedbackImages: normalizeFeedbackImages(data.feedback_images),
    ...protocol,
  };
}

function setResponseStatus(data) {
  if (data?.status === 'PARTIAL') {
    setStatus('ready', '结果已返回，部分能力暂时降级');
  } else if (data?.status === 'ERROR' || data?.failure) {
    setStatus('error', '处理失败，可重新尝试');
  } else {
    setStatus('ready', '准备就绪');
  }
}

function maybeOpenAutoPreparedA3Sheet(response) {
  const target = {
    workflowId: String(response.workflowId || ''),
    workflowRevision: Number(response.workflowRevision || 0),
  };
  if (
    response.intent === 'a3_units_prepared'
    && response.a3?.auto_prepare_all_units
    && workflowActionTargetMatchesA3(target, response.a3)
    && taskStateAllowsA3UnitNavigation(target)
  ) {
    openA3Sheet(target);
  }
}

function updateSessionContext(data) {
  const acceptedGeneration = data && typeof data === 'object'
    ? taskStateAcceptedEnvelopes.get(data)
    : undefined;
  if (!data?.session || acceptedGeneration !== taskStateRequestGeneration) {
    if (data && typeof data === 'object') {
      taskStateAcceptedEnvelopes.delete(data);
      taskStateEnvelopeBindings.delete(data);
    }
    return null;
  }
  taskStateAcceptedEnvelopes.delete(data);
  const a3 = normalizeA3Snapshot(data.session.a3);
  const envelopeWorkflowTarget = taskStateEnvelopeBindings.get(data) || null;
  taskStateEnvelopeBindings.delete(data);
  const workflowTarget = workflowActionTargetMatchesA3(envelopeWorkflowTarget, a3)
    ? envelopeWorkflowTarget
    : null;
  const workflowKey = workflowIdentityKey(workflowTarget);
  if (workflowKey !== a3SourceWorkflowKey) {
    a3SourceUrl = '';
    a3SourceWorkflowKey = '';
  }
  sessionContext = {
    ...sessionContext,
    ...data.session,
    a3,
    a3WorkflowId: String(workflowTarget?.workflowId || ''),
    a3WorkflowRevision: Number(workflowTarget?.workflowRevision || 0),
  };
  if (workflowKey && isPersistentImage(data.uploaded_image)) {
    a3SourceUrl = data.uploaded_image;
    a3SourceWorkflowKey = workflowKey;
  }
  syncA3Interface();
  return { workflowTarget, a3 };
}

function applyResetSessionContext(data) {
  const acceptedGeneration = data && typeof data === 'object'
    ? taskStateAcceptedEnvelopes.get(data)
    : undefined;
  const snapshot = taskStateContext?.snapshot;
  const accepted = acceptedGeneration === taskStateRequestGeneration
    && taskStateContext.available
    && taskStateContext.consistent
    && snapshot?.workflow?.exists === false
    && snapshot.active_child_task === null
    && snapshot.current_unit === null
    && snapshot.units.length === 0;
  if (data && typeof data === 'object') {
    taskStateAcceptedEnvelopes.delete(data);
    taskStateEnvelopeBindings.delete(data);
  }
  if (!accepted) {
    invalidateTaskStateContext();
    return false;
  }
  sessionContext = {
    session_valid: false, phase: 'IDLE', has_active_image: false,
    task_revision: 0, candidate_generation: '', candidate_count: 0, search_id: '', a3: null,
    a3WorkflowId: '', a3WorkflowRevision: 0,
  };
  sessionResetRequired = false;
  sessionResetActivityAt = 0;
  clearA3WorkflowState();
  return true;
}

function invalidateCandidateActions() {
  sessionContext = { ...sessionContext, session_valid: false, phase: 'PROCESSING', candidate_generation: '', candidate_count: 0 };
  document.querySelectorAll('.select-candidate').forEach((button) => {
    button.disabled = true;
    button.textContent = '候选已失效';
  });
  syncA3ActionButtons();
}

function a3Current() {
  return normalizeA3Snapshot(sessionContext.a3);
}

function currentA3WorkflowTarget(a3 = a3Current()) {
  const target = {
    workflowId: String(sessionContext.a3WorkflowId || ''),
    workflowRevision: Number(sessionContext.a3WorkflowRevision || 0),
  };
  return workflowActionTargetMatchesA3(target, a3) ? target : null;
}

function currentA3CropActionTarget(a3 = a3Current()) {
  const workflowTarget = currentA3WorkflowTarget(a3);
  const currentUnit = taskStateContext?.snapshot?.current_unit;
  const selectedUnitId = String(a3?.selected_unit?.unit_id || '');
  if (!workflowTarget || !currentUnit || !selectedUnitId || selectedUnitId !== currentUnit.unit_id) {
    return null;
  }
  return { ...workflowTarget, unitId: currentUnit.unit_id };
}

function a3CropReviewMessage(a3 = a3Current()) {
  const code = String(a3?.crop_review_code || '');
  return A3_CROP_REVIEW_MESSAGES[code]
    || '裁剪结果未通过，请重新选择区域裁剪。';
}

function workflowIdentityKey(target) {
  const workflowId = String(target?.workflowId || '');
  const workflowRevision = Number(target?.workflowRevision || 0);
  return workflowId && Number.isInteger(workflowRevision) && workflowRevision > 0
    ? JSON.stringify([workflowId, workflowRevision])
    : '';
}

function a3MediaUrl(path, target) {
  if (!workflowIdentityKey(target)) return '';
  return `${path}?workflow_id=${encodeURIComponent(target.workflowId)}`
    + `&task_revision=${encodeURIComponent(target.workflowRevision)}`;
}

function clearImageSource(image) {
  if (!image) return;
  if (typeof image.removeAttribute === 'function') image.removeAttribute('src');
  else image.src = '';
}

function clearA3MediaElementSources() {
  if (typeof a3SheetOverlayImage !== 'undefined') clearImageSource(a3SheetOverlayImage);
  if (typeof a3SourceImage !== 'undefined') clearImageSource(a3SourceImage);
  if (typeof a3SheetUnits !== 'undefined' && a3SheetUnits) {
    a3SheetUnits.querySelectorAll?.('img').forEach(clearImageSource);
    a3SheetUnits.replaceChildren?.();
  }
  if (typeof lightboxImage !== 'undefined' && lightboxImage) {
    const lightboxSource = String(lightboxImage.getAttribute?.('src') || lightboxImage.src || '');
    if (
      lightboxSource.includes('/api/a3/overlay')
      || lightboxSource.includes('/api/a3/crop/')
      || lightboxSource.includes('/api/upload/')
      || lightboxSource.includes('/api/media/')
    ) {
      if (typeof lightbox !== 'undefined' && lightbox && !lightbox.hidden) closeLightbox();
      else clearImageSource(lightboxImage);
    }
  }
}

function a3DraftKey(a3 = a3Current(), target = currentA3WorkflowTarget(a3)) {
  const unitId = Object.hasOwn(target || {}, 'unitId')
    ? String(target.unitId || '')
    : String(a3?.selected_unit?.unit_id || '');
  const identity = workflowIdentityKey(target);
  return identity && unitId ? JSON.stringify([target.workflowId, target.workflowRevision, unitId]) : '';
}

function validA3Bounds(value) {
  if (!value || typeof value !== 'object') return null;
  const bounds = {
    x: Number(value.x), y: Number(value.y),
    width: Number(value.width), height: Number(value.height),
  };
  if (!Object.values(bounds).every(Number.isFinite)) return null;
  if (bounds.x < 0 || bounds.y < 0 || bounds.width < 0.02 || bounds.height < 0.02) return null;
  if (bounds.x + bounds.width > 1.000001 || bounds.y + bounds.height > 1.000001) return null;
  return bounds;
}

function syncA3ActionButtons() {
  if (typeof document !== 'object') return;
  const a3 = a3Current();
  document.querySelectorAll('.a3-unit-choice[data-a3-unit-id]').forEach((button) => {
    const unit = a3?.units.find((item) => item.unit_id === button.dataset.a3UnitId);
    const completed = Boolean(unit?.completed);
    const searched = Boolean(unit?.searched);
    if (unit && searched) button.textContent = `${unit.display_label || '未标号题目'} · 已检索`;
    else if (unit && !completed) button.textContent = unit.display_label || '未标号题目';
    else if (unit && completed) {
      const label = button.querySelector('span');
      if (label) label.textContent = `${unit.display_label || '未标号题目'} · 已完成`;
    }
    button.classList.toggle('is-complete', completed);
  });

  const actionGroups = Array.from(document.querySelectorAll('.a3-unit-actions'));
  const workflowTarget = currentA3WorkflowTarget(a3);
  const currentGroups = actionGroups.filter((host) => Boolean(
    workflowTarget
    && host.dataset.workflowId === workflowTarget.workflowId
    && Number(host.dataset.workflowRevision || 0) === workflowTarget.workflowRevision
    && host.dataset.a3Phase === taskStateContext.snapshot.workflow.phase
  ));
  let latestGroup = currentGroups.at(-1) || null;
  const cropTarget = currentA3CropActionTarget(a3);
  const canContinueCrop = Boolean(
    cropTarget && taskStateAllowsA3Action('submit_crop', cropTarget, a3)
  );
  if (canContinueCrop && !latestGroup) {
    const latestAssistantContent = Array.from(document.querySelectorAll('.message:not(.user) .message-content')).at(-1);
    if (latestAssistantContent) {
      latestGroup = document.createElement('div');
      latestGroup.className = 'a3-unit-actions';
      latestGroup.dataset.a3Revision = String(a3.task_revision || 0);
      latestGroup.dataset.a3Phase = a3.phase;
      latestGroup.dataset.workflowId = workflowTarget.workflowId;
      latestGroup.dataset.workflowRevision = String(workflowTarget.workflowRevision);
      latestAssistantContent.append(latestGroup);
      actionGroups.push(latestGroup);
    }
  }
  if (canContinueCrop && latestGroup && !latestGroup.querySelector('.a3-continue-crop')) {
    latestGroup.append(createA3ContinueCropButton(cropTarget, a3));
  }
  actionGroups.forEach((host) => {
    host.hidden = host !== latestGroup;
  });

  document.querySelectorAll('[data-workflow-action]').forEach((control) => {
    const action = String(control.dataset.workflowAction || '');
    const allowed = !(typeof isBusy === 'boolean' && isBusy)
      && taskStateAllowsA3Action(action, workflowActionTargetFromControl(control), a3);
    control.disabled = !allowed;
    if (control.dataset.hideWhenDenied === 'true') control.hidden = !allowed;
  });
  document.querySelectorAll('[data-a3-unit-navigation]').forEach((control) => {
    const allowed = !(typeof isBusy === 'boolean' && isBusy)
      && taskStateAllowsA3UnitNavigation(workflowActionTargetFromControl(control), a3);
    control.disabled = !allowed;
    if (control.dataset.hideWhenDenied === 'true') control.hidden = !allowed;
  });

  if (!a3CropWorkspace.hidden) renderA3Selection();
}

function syncA3Interface() {
  const a3 = a3Current();
  syncA3ActionButtons();
  if (!a3) {
    clearA3MediaElementSources();
    a3Bounds = null;
    a3LocalDrafts.clear();
    a3PrepareSelection.clear();
    a3DismissedKey = '';
    a3DismissNextCrop = false;
    a3KnownWorkflowKey = '';
    if (!a3CropWorkspace.hidden) requestCloseA3Crop({ dismiss: false });
    else if (a3CropHistoryActive) clearA3CropHistoryState();
    if (!a3SheetBackdrop.hidden) closeA3Sheet();
    return;
  }
  const workflowTarget = currentA3WorkflowTarget(a3);
  const workflowKey = workflowIdentityKey(workflowTarget);
  const cropTarget = currentA3CropActionTarget(a3);
  const cropKey = cropTarget ? a3DraftKey(a3, cropTarget) : '';
  if (a3KnownWorkflowKey !== workflowKey) {
    clearA3MediaElementSources();
    a3Bounds = null;
    a3LocalDrafts.clear();
    a3PrepareSelection.clear();
    if (!cropKey || a3DismissedKey !== cropKey) a3DismissedKey = '';
  }
  a3KnownWorkflowKey = workflowKey;
  renderA3SheetUnits(a3);
  if (cropTarget && taskStateAllowsA3Action('submit_crop', cropTarget, a3)) {
    const key = cropKey;
    if (a3DismissNextCrop && key) {
      a3DismissedKey = key;
      a3DismissNextCrop = false;
    }
    if (key && key !== a3DismissedKey) openA3Crop(cropTarget);
  } else if (!a3CropWorkspace.hidden) {
    requestCloseA3Crop({ dismiss: false });
  } else if (a3CropHistoryActive) {
    clearA3CropHistoryState();
  } else {
    a3DismissNextCrop = false;
  }
  if (!taskStateAllowsA3UnitNavigation(workflowTarget, a3) && !a3SheetBackdrop.hidden) {
    closeA3Sheet();
  }
}

async function selectA3Unit(target) {
  if (isBusy) return;
  const actionTarget = target && typeof target === 'object' ? {
    workflowId: String(target.workflowId || ''),
    workflowRevision: Number(target.workflowRevision || 0),
    unitId: String(target.unitId || ''),
  } : null;
  if (!taskStateAllowsA3Action('select_unit', actionTarget)) return;
  closeA3Sheet();
  const operation = ++operationVersion;
  const pending = addMessage({ message: '正在打开裁剪页', variant: 'pending' }, false);
  setBusy(true);
  setStatus('working', '正在准备裁剪…');
  try {
    const data = await requestStream('/api/a3/select/stream', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        workflow_id: actionTarget.workflowId,
        unit_id: actionTarget.unitId,
        task_revision: actionTarget.workflowRevision,
      }),
    }, A3_TIMEOUT_MS, '选题或搜题等待超时，请重新选择。', (event) => {
      if (operation !== operationVersion) return;
      updatePendingMessage(pending, event.message);
      setStatus('working', event.message);
    });
    if (operation !== operationVersion) return;
    pending.remove();
    const response = responseItem(data);
    if (!A3_INLINE_ONLY_INTENTS.has(data.intent)) addMessage(response);
    setResponseStatus(data);
  } catch (error) {
    if (operation !== operationVersion) return;
    pending.remove();
    addMessage({
      message: error.message || '选题失败，请重新选择。',
      variant: 'error', recoveryActions: taskStateFailureRecoveryActions(error.recoveryActions),
      ...protocolFields(error),
    });
    setStatus('error', '选题失败');
  } finally {
    if (operation !== operationVersion) return;
    setBusy(false);
  }
}

function openA3Crop(actionTarget, { force = false } = {}) {
  const a3 = a3Current();
  const selected = a3?.selected_unit;
  if (!selected?.unit_id || !taskStateAllowsA3Action('submit_crop', actionTarget, a3)) return;
  const workflowKey = workflowIdentityKey(actionTarget);
  if (!a3SourceUrl || !workflowKey || workflowKey !== a3SourceWorkflowKey) {
    setStatus('error', '原始题图未能恢复');
    return;
  }
  const key = a3DraftKey(a3, actionTarget);
  if (force) a3DismissedKey = '';
  if (!force && key === a3DismissedKey) return;
  a3CropLabel.textContent = selected.display_label || '未标号题目';
  const contextText = String(selected.context_text || '').trim();
  a3Context.hidden = !contextText;
  a3ContextText.textContent = contextText;
  const workflowTarget = {
    workflowId: actionTarget.workflowId,
    workflowRevision: actionTarget.workflowRevision,
  };
  bindA3UnitNavigationControl(a3Reselect, workflowTarget, { hideWhenDenied: true });
  const canReselect = taskStateAllowsA3UnitNavigation(workflowTarget, a3);
  a3Reselect.hidden = !canReselect;
  a3Reselect.disabled = !canReselect;
  bindWorkflowActionControl(a3Submit, 'submit_crop', actionTarget);
  const serverDraft = validA3Bounds(a3.crop_draft?.bounds);
  a3Bounds = validA3Bounds(a3LocalDrafts.get(key)) || serverDraft;
  if (a3Bounds) a3LocalDrafts.set(key, { ...a3Bounds });
  a3SourceImage.src = a3SourceUrl;
  renderA3Selection();
  a3CropWorkspace.hidden = false;
  a3CropWorkspace.setAttribute('aria-hidden', 'false');
  document.body.dataset.modal = 'a3-crop';
  if (a3SourceImage.complete && a3SourceImage.naturalWidth) fitA3Image();
  if (!a3CropHistoryActive) {
    window.history.pushState({
      ...(window.history.state || {}),
      a3Crop: {
        workflowId: actionTarget.workflowId,
        workflowRevision: actionTarget.workflowRevision,
        unitId: actionTarget.unitId,
      },
    }, '');
    a3CropHistoryActive = true;
    a3CropHistoryKey = key;
  } else if (a3CropHistoryKey !== key) {
    window.history.replaceState({
      ...(window.history.state || {}),
      a3Crop: {
        workflowId: actionTarget.workflowId,
        workflowRevision: actionTarget.workflowRevision,
        unitId: actionTarget.unitId,
      },
    }, '');
    a3CropHistoryKey = key;
  }
  a3CropBack.focus();
}

function finishCloseA3Crop({ dismiss = true, dismissKey = null } = {}) {
  a3PendingClose = null;
  if (dismiss) {
    const resolvedKey = dismissKey === null
      ? a3DraftKey(a3Current(), workflowActionTargetFromControl(a3Submit))
      : String(dismissKey);
    if (resolvedKey) a3DismissedKey = resolvedKey;
    else a3DismissNextCrop = true;
  }
  if (a3CropWorkspace.hidden) return;
  a3CropWorkspace.hidden = true;
  a3CropWorkspace.setAttribute('aria-hidden', 'true');
  a3Pointer = null;
  closeA3Sheet();
  closeA3Example();
  delete document.body.dataset.modal;
}

function closeA3TransientUi() {
  if (!a3CropWorkspace.hidden) finishCloseA3Crop({ dismiss: false });
  closeA3Sheet();
  closeA3Example();
}

function clearA3CropHistoryState() {
  const historyState = window.history.state;
  if (historyState && typeof historyState === 'object' && Object.hasOwn(historyState, 'a3Crop')) {
    const nextState = { ...historyState };
    delete nextState.a3Crop;
    window.history.replaceState(nextState, '');
  }
  a3CropHistoryActive = false;
  a3CropHistoryKey = '';
  a3PendingClose = null;
  a3DismissNextCrop = false;
}

function a3CropHistoryMarker(historyState) {
  if (!historyState || typeof historyState !== 'object' || !Object.hasOwn(historyState, 'a3Crop')) {
    return { active: false, target: null, key: '' };
  }
  if (historyState.a3Crop === true) return { active: true, target: null, key: '' };
  const raw = historyState.a3Crop;
  if (!raw || typeof raw !== 'object') return { active: false, target: null, key: '' };
  const target = {
    workflowId: String(raw.workflowId || ''),
    workflowRevision: Number(raw.workflowRevision || 0),
    unitId: String(raw.unitId || ''),
  };
  const key = a3DraftKey(null, target);
  return key ? { active: true, target, key } : { active: false, target: null, key: '' };
}

function restoreA3CropHistoryState() {
  const marker = a3CropHistoryMarker(window.history.state);
  a3CropHistoryActive = marker.active;
  a3CropHistoryKey = marker.key;
  a3PendingClose = null;
}

function requestCloseA3Crop({ dismiss = true } = {}) {
  const pendingClose = {
    dismiss: Boolean(dismiss),
    key: a3DraftKey(a3Current(), workflowActionTargetFromControl(a3Submit)),
  };
  if (a3CropHistoryActive) {
    const navigationPending = a3PendingClose !== null;
    a3PendingClose = pendingClose;
    if (!navigationPending) window.history.back();
    return;
  }
  finishCloseA3Crop({ dismiss: pendingClose.dismiss, dismissKey: pendingClose.key });
}

function renderA3Selection() {
  const bounds = validA3Bounds(a3Bounds);
  const cropTarget = workflowActionTargetFromControl(a3Submit);
  const canSubmit = taskStateAllowsA3Action('submit_crop', cropTarget);
  a3Selection.hidden = !bounds;
  a3ImageHint.hidden = Boolean(bounds);
  a3Submit.disabled = !bounds || isBusy || !canSubmit;
  a3CropStatus.classList.toggle('is-warning', Boolean(a3Current()?.crop_review_required));
  if (!bounds) {
    a3CropStatus.textContent = '尚未框选结构图';
    return;
  }
  a3Selection.style.left = `${bounds.x * 100}%`;
  a3Selection.style.top = `${bounds.y * 100}%`;
  a3Selection.style.width = `${bounds.width * 100}%`;
  a3Selection.style.height = `${bounds.height * 100}%`;
  a3CropStatus.textContent = a3Current()?.crop_review_required
    ? a3CropReviewMessage()
    : '已框选，可以提交校验';
}

function fitA3Image() {
  if (!a3SourceImage.naturalWidth || !a3SourceImage.naturalHeight) return;
  const availableWidth = Math.max(120, a3ImageArea.clientWidth - 44);
  const availableHeight = Math.max(120, a3ImageArea.clientHeight - 44);
  const fitScale = Math.min(
    1,
    availableWidth / a3SourceImage.naturalWidth,
    availableHeight / a3SourceImage.naturalHeight,
  );
  a3SourceImage.style.width = `${Math.round(a3SourceImage.naturalWidth * fitScale)}px`;
  a3SourceImage.style.height = `${Math.round(a3SourceImage.naturalHeight * fitScale)}px`;
  renderA3Selection();
}

function a3Point(event) {
  const rect = a3ImageFrame.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  return {
    x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
    y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
  };
}

function paintA3Selection(bounds) {
  a3Selection.style.left = `${bounds.x * 100}%`;
  a3Selection.style.top = `${bounds.y * 100}%`;
  a3Selection.style.width = `${bounds.width * 100}%`;
  a3Selection.style.height = `${bounds.height * 100}%`;
}

function startA3Selection(event) {
  const cropTarget = workflowActionTargetFromControl(a3Submit);
  if (
    isBusy
    || !taskStateAllowsA3Action('submit_crop', cropTarget)
    || a3SourceImage.complete === false
    || a3Pointer
  ) return;
  if (event.pointerType === 'mouse' && event.button !== 0) return;
  if (event.pointerType !== 'mouse' && !event.isPrimary) return;
  const point = a3Point(event);
  if (!point) return;
  event.preventDefault();
  a3ImageFrame.setPointerCapture(event.pointerId);
  const origin = validA3Bounds(a3Bounds);
  const handle = event.target instanceof Element
    ? String(event.target.closest('[data-a3-handle]')?.dataset.a3Handle || '')
    : '';
  let mode = 'create';
  if (origin && handle) mode = 'resize';
  else if (origin && event.target instanceof Element && event.target.closest('.a3-selection')) mode = 'move';
  a3Pointer = { id: event.pointerId, start: point, mode, handle, origin: origin ? { ...origin } : null };
  if (mode === 'create') a3Bounds = { x: point.x, y: point.y, width: 0, height: 0 };
  a3Selection.hidden = false;
  a3ImageHint.hidden = true;
}

function moveA3Selection(event) {
  if (!a3Pointer || event.pointerId !== a3Pointer.id) return;
  const point = a3Point(event);
  if (!point) return;
  event.preventDefault();
  if (a3Pointer.mode === 'move' && a3Pointer.origin) {
    const origin = a3Pointer.origin;
    a3Bounds = {
      ...origin,
      x: Math.max(0, Math.min(1 - origin.width, origin.x + point.x - a3Pointer.start.x)),
      y: Math.max(0, Math.min(1 - origin.height, origin.y + point.y - a3Pointer.start.y)),
    };
  } else if (a3Pointer.mode === 'resize' && a3Pointer.origin) {
    const origin = a3Pointer.origin;
    const frameRect = a3ImageFrame.getBoundingClientRect();
    const minWidth = Math.min(0.25, Math.max(0.02, 44 / frameRect.width));
    const minHeight = Math.min(0.25, Math.max(0.02, 44 / frameRect.height));
    let left = origin.x;
    let right = origin.x + origin.width;
    let top = origin.y;
    let bottom = origin.y + origin.height;
    if (a3Pointer.handle.includes('w')) left = Math.max(0, Math.min(right - minWidth, point.x));
    if (a3Pointer.handle.includes('e')) right = Math.min(1, Math.max(left + minWidth, point.x));
    if (a3Pointer.handle.includes('n')) top = Math.max(0, Math.min(bottom - minHeight, point.y));
    if (a3Pointer.handle.includes('s')) bottom = Math.min(1, Math.max(top + minHeight, point.y));
    a3Bounds = { x: left, y: top, width: right - left, height: bottom - top };
  } else {
    const left = Math.min(a3Pointer.start.x, point.x);
    const top = Math.min(a3Pointer.start.y, point.y);
    a3Bounds = {
      x: left, y: top,
      width: Math.abs(point.x - a3Pointer.start.x),
      height: Math.abs(point.y - a3Pointer.start.y),
    };
  }
  paintA3Selection(a3Bounds);
}

function endA3Selection(event) {
  if (!a3Pointer || event.pointerId !== a3Pointer.id) return;
  const pointer = a3Pointer;
  a3Pointer = null;
  if (a3ImageFrame.hasPointerCapture(event.pointerId)) a3ImageFrame.releasePointerCapture(event.pointerId);
  if (event.type === 'pointercancel') a3Bounds = pointer.origin;
  if (!validA3Bounds(a3Bounds)) a3Bounds = null;
  else {
    const key = a3DraftKey(a3Current(), workflowActionTargetFromControl(a3Submit));
    if (key) a3LocalDrafts.set(key, { ...a3Bounds });
  }
  renderA3Selection();
}

async function submitA3Crop() {
  const bounds = validA3Bounds(a3Bounds);
  const actionTarget = workflowActionTargetFromControl(a3Submit);
  if (!bounds || isBusy || !taskStateAllowsA3Action('submit_crop', actionTarget)) return;
  const operation = ++operationVersion;
  setBusy(true);
  a3Submit.disabled = true;
  a3CropStatus.classList.remove('is-warning');
  a3CropStatus.textContent = '正在校验裁剪图…';
  setStatus('working', '正在校验裁剪图…');
  try {
    const data = await requestStream('/api/a3/crop/stream', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        workflow_id: actionTarget.workflowId,
        bounds,
        unit_id: actionTarget.unitId,
        task_revision: actionTarget.workflowRevision,
      }),
    }, A3_TIMEOUT_MS, '裁剪校验或搜题等待超时，已保留裁剪范围。', (event) => {
      if (operation !== operationVersion) return;
      a3CropStatus.textContent = event.message;
      setStatus('working', event.message);
    });
    if (operation !== operationVersion) return;
    const response = responseItem(data);
    if (isPersistentImage(data.submitted_crop)) {
      addMessage({
        message: '我提交了裁剪后的题图。', me: true,
        images: [data.submitted_crop], imageAlt: '裁剪后的题图',
        taskRevision: response.taskRevision,
      });
    }
    if (!A3_INLINE_ONLY_INTENTS.has(data.intent)) addMessage(response);
    setResponseStatus(data);
    if (data.intent === 'a3_crop_review_required') {
      a3CropStatus.classList.add('is-warning');
      a3CropStatus.textContent = response.message
        || a3CropReviewMessage()
        || '裁剪结果未通过，请重新选择区域裁剪。';
    }
  } catch (error) {
    if (operation !== operationVersion) return;
    a3CropStatus.classList.add('is-warning');
    a3CropStatus.textContent = error.message || '裁剪校验失败，可以直接重试';
    addMessage({
      message: error.message || '裁剪校验失败，可以直接重试。',
      variant: 'error', recoveryActions: taskStateFailureRecoveryActions(error.recoveryActions),
      ...protocolFields(error),
    });
    setStatus('error', '裁剪校验失败');
  } finally {
    if (operation !== operationVersion) return;
    setBusy(false);
    renderA3Selection();
  }
}

function renderA3SheetUnits(a3 = a3Current()) {
  if (!a3SheetUnits || !a3) return;
  const workflowTarget = currentA3WorkflowTarget(a3);
  a3SheetUnits.replaceChildren();
  if (a3.auto_crop_enabled) {
    renderA3AutoSheetUnits(a3);
    return;
  }
  a3SheetSubtitle.hidden = false;
  a3SheetSubtitle.textContent = '选择其他题目后会重新裁剪并搜索';
  const overlayUrl = a3.auto_crop_overlay_available
    ? a3MediaUrl('/api/a3/overlay', workflowTarget)
    : '';
  a3SheetOverlay.hidden = !overlayUrl;
  if (overlayUrl) a3SheetOverlayImage.src = overlayUrl;
  else clearImageSource(a3SheetOverlayImage);
  a3SheetFooter.hidden = true;
  a3.units.forEach((unit) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'a3-sheet-unit';
    const isCurrent = unit.unit_id === a3.selected_unit?.unit_id;
    const actionTarget = { ...workflowTarget, unitId: unit.unit_id };
    bindWorkflowActionControl(button, 'select_unit', actionTarget);
    button.disabled = isBusy || !taskStateAllowsA3Action('select_unit', actionTarget, a3);
    button.setAttribute('aria-current', String(isCurrent));
    const text = document.createElement('span');
    const title = document.createElement('strong');
    title.textContent = unit.display_label || '未标号题目';
    const detail = document.createElement('small');
    detail.textContent = unit.completed
      ? '已完成'
      : unit.searched ? '已检索'
      : isCurrent ? '当前正在处理' : (unit.title_text || '待裁剪');
    text.append(title, detail);
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('viewBox', '0 0 24 24');
    icon.innerHTML = unit.completed ? '<path d="m5 12 4 4L19 6"/>' : '<path d="m9 18 6-6-6-6"/>';
    button.append(text);
    if (!isCurrent) button.append(icon);
    button.addEventListener('click', () => selectA3Unit(actionTarget));
    a3SheetUnits.append(button);
  });
}

function renderA3AutoSheetUnits(a3) {
  const workflowTarget = currentA3WorkflowTarget(a3);
  const currentIds = new Set(a3.units.filter((unit) => {
    const canSelect = taskStateAllowsA3Action(
      'select_unit', { ...workflowTarget, unitId: unit.unit_id }, a3,
    );
    const canPrepare = taskStateAllowsA3Action(
      'prepare_units', { ...workflowTarget, unitIds: [unit.unit_id] }, a3,
    );
    const prepared = taskStateWorkflowUnit(unit.unit_id)?.status === 'PREPARED';
    const directlySelectable = canSelect && (prepared || unit.requested || !canPrepare);
    return canPrepare && !directlySelectable;
  }).map((unit) => unit.unit_id));
  Array.from(a3PrepareSelection).forEach((unitId) => {
    if (!currentIds.has(unitId)) a3PrepareSelection.delete(unitId);
  });
  a3SheetSubtitle.hidden = a3.auto_prepare_all_units;
  a3SheetSubtitle.textContent = a3.auto_prepare_all_units
    ? ''
    : '可多选；只校验你准备查询的裁图';
  const overlayUrl = a3.auto_crop_overlay_available
    ? a3MediaUrl('/api/a3/overlay', workflowTarget)
    : '';
  a3SheetOverlay.hidden = !overlayUrl;
  if (overlayUrl) a3SheetOverlayImage.src = overlayUrl;
  else clearImageSource(a3SheetOverlayImage);
  a3SheetFooter.hidden = currentIds.size === 0;
  a3.units.forEach((unit) => {
    const stateUnit = taskStateWorkflowUnit(unit.unit_id);
    const selectTarget = { ...workflowTarget, unitId: unit.unit_id };
    const prepareTarget = { ...workflowTarget, unitIds: [unit.unit_id] };
    const canSelect = taskStateAllowsA3Action('select_unit', selectTarget, a3);
    const canPrepare = taskStateAllowsA3Action('prepare_units', prepareTarget, a3);
    const prepared = stateUnit?.status === 'PREPARED';
    const directlySelectable = canSelect && (prepared || unit.requested || !canPrepare);
    const host = document.createElement(directlySelectable ? 'button' : 'label');
    if (directlySelectable) host.type = 'button';
    const closed = !canSelect && !canPrepare;
    host.className = `a3-auto-unit${closed ? ' is-closed' : ''}${prepared ? ' is-prepared' : ''}`;
    const visual = document.createElement('span');
    visual.className = 'a3-auto-unit-visual';
    const cropUrl = unit.crop_available
      ? a3MediaUrl(`/api/a3/crop/${encodeURIComponent(unit.unit_id)}`, workflowTarget)
      : '';
    if (cropUrl) {
      const image = document.createElement('img');
      image.src = cropUrl;
      image.alt = `${unit.display_label || '未标号题目'}自动裁图`;
      visual.append(image);
    } else {
      visual.textContent = '需手动裁剪';
    }
    const copy = document.createElement('span');
    copy.className = 'a3-auto-unit-copy';
    const title = document.createElement('strong');
    title.textContent = unit.display_label || '未标号题目';
    const detail = document.createElement('small');
    if (unit.completed) detail.textContent = '已完成';
    else if (unit.searched) detail.textContent = '已检索，不可重复进入';
    else if (unit.preparation_status === 'ready') detail.textContent = '已校验，可直接检索';
    else if (unit.preparation_status === 'located') detail.textContent = '待校验';
    else if (unit.preparation_status === 'manual') detail.textContent = '需要人工裁剪';
    else detail.textContent = '选择后使用人工裁剪';
    copy.append(title, detail);
    host.append(visual, copy);
    if (directlySelectable) {
      const arrow = document.createElement('span');
      arrow.className = 'a3-auto-unit-arrow';
      arrow.textContent = unit.completed ? '已完成' : unit.searched ? '已检索' : '继续';
      host.append(arrow);
      bindWorkflowActionControl(host, 'select_unit', selectTarget);
      host.disabled = isBusy || !canSelect;
      host.addEventListener('click', () => selectA3Unit(selectTarget));
    } else {
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = a3PrepareSelection.has(unit.unit_id);
      bindWorkflowActionControl(input, 'prepare_units', prepareTarget);
      input.disabled = isBusy || !canPrepare;
      input.addEventListener('change', () => {
        if (!taskStateAllowsA3Action('prepare_units', prepareTarget)) {
          input.checked = false;
          a3PrepareSelection.delete(unit.unit_id);
          updateA3PrepareFooter();
          return;
        }
        if (input.checked) a3PrepareSelection.add(unit.unit_id);
        else a3PrepareSelection.delete(unit.unit_id);
        updateA3PrepareFooter();
      });
      host.prepend(input);
    }
    a3SheetUnits.append(host);
  });
  updateA3PrepareFooter();
}

function updateA3PrepareFooter() {
  const count = a3PrepareSelection.size;
  const a3 = a3Current();
  const workflowTarget = currentA3WorkflowTarget(a3);
  const actionTarget = { ...workflowTarget, unitIds: Array.from(a3PrepareSelection) };
  bindWorkflowActionControl(a3Prepare, 'prepare_units', actionTarget);
  a3SheetCount.textContent = count ? `已选择 ${count} 道` : '尚未选择';
  a3Prepare.disabled = isBusy || !taskStateAllowsA3Action('prepare_units', actionTarget, a3);
  a3Prepare.textContent = count ? `校验所选 ${count} 道题` : '校验所选题目';
}

async function prepareA3Units() {
  if (isBusy) return;
  const actionTarget = workflowActionTargetFromControl(a3Prepare);
  if (!taskStateAllowsA3Action('prepare_units', actionTarget)) return;
  const unitIds = [...actionTarget.unitIds];
  const operation = ++operationVersion;
  setBusy(true);
  updateA3PrepareFooter();
  setStatus('working', '正在并发校验所选裁图…');
  try {
    const data = await requestStream('/api/a3/prepare/stream', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        workflow_id: actionTarget.workflowId,
        unit_ids: unitIds,
        task_revision: actionTarget.workflowRevision,
      }),
    }, A3_TIMEOUT_MS, '裁图校验等待超时，请重新选择。', (event) => {
      if (operation !== operationVersion) return;
      a3SheetCount.textContent = event.message;
      setStatus('working', event.message);
    });
    if (operation !== operationVersion) return;
    const response = responseItem(data);
    a3PrepareSelection.clear();
    addMessage(response);
    setResponseStatus(data);
    renderA3SheetUnits();
  } catch (error) {
    if (operation !== operationVersion) return;
    addMessage({
      message: error.message || '裁图校验失败，请重新选择。',
      variant: 'error', recoveryActions: taskStateFailureRecoveryActions(error.recoveryActions),
      ...protocolFields(error),
    });
    setStatus('error', '裁图校验失败');
  } finally {
    if (operation !== operationVersion) return;
    setBusy(false);
    updateA3PrepareFooter();
  }
}

function openA3Sheet(target) {
  const a3 = a3Current();
  const workflowTarget = target && typeof target === 'object' ? {
    workflowId: String(target.workflowId || ''),
    workflowRevision: Number(target.workflowRevision || 0),
  } : null;
  if (
    !a3
    || !taskStateAllowsA3UnitNavigation(workflowTarget, a3)
    || (!a3.auto_crop_enabled && a3.units.length <= 1)
  ) return;
  renderA3SheetUnits(a3);
  a3SheetBackdrop.hidden = false;
  a3SheetClose.focus();
}

function closeA3Sheet() {
  if (a3SheetBackdrop) a3SheetBackdrop.hidden = true;
}

function drawA3Example() {
  const canvas = a3ExampleCanvas;
  const context = canvas.getContext('2d');
  if (!context) return;
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = '#f4f4f1';
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = '#fff';
  context.fillRect(86, 62, 788, 416);
  context.strokeStyle = '#0f766e';
  context.lineWidth = 5;
  context.setLineDash([14, 10]);
  context.strokeRect(116, 88, 728, 364);
  context.setLineDash([]);
  context.strokeStyle = '#202020';
  context.lineWidth = 9;
  context.beginPath();
  context.moveTo(260, 356);
  context.lineTo(260, 196);
  context.lineTo(700, 196);
  context.lineTo(700, 356);
  context.stroke();
  context.lineWidth = 4;
  context.beginPath();
  context.moveTo(228, 388); context.lineTo(260, 356); context.lineTo(292, 388); context.closePath();
  context.moveTo(668, 388); context.lineTo(700, 356); context.lineTo(732, 388); context.closePath();
  context.stroke();
  context.strokeStyle = '#777770';
  context.beginPath();
  context.moveTo(210, 390); context.lineTo(310, 390);
  context.moveTo(650, 390); context.lineTo(750, 390);
  context.stroke();
  context.strokeStyle = '#c2410c';
  context.fillStyle = '#c2410c';
  context.lineWidth = 4;
  for (let x = 310; x <= 650; x += 68) {
    context.beginPath(); context.moveTo(x, 122); context.lineTo(x, 180); context.stroke();
    context.beginPath(); context.moveTo(x - 9, 166); context.lineTo(x, 180); context.lineTo(x + 9, 166); context.closePath(); context.fill();
  }
  context.beginPath(); context.moveTo(310, 122); context.lineTo(650, 122); context.stroke();
  context.font = '600 28px system-ui, sans-serif';
  context.fillText('q', 670, 132);
  context.fillStyle = '#0f766e';
  context.font = '600 24px system-ui, sans-serif';
  context.fillText('保留完整结构、支座与全部荷载', 258, 432);
}

function openA3Example() {
  drawA3Example();
  a3ExampleBackdrop.hidden = false;
  a3ExampleClose.focus();
}

function closeA3Example() {
  if (a3ExampleBackdrop) a3ExampleBackdrop.hidden = true;
}

async function sendTextValue(value, displayValue = value, actionContext = null, childActionTarget = null) {
  const clean = String(value || '').trim();
  if (!clean || isBusy) return;
  if (actionContext === null && isExplicitSessionResetText(clean)) {
    await resetConversation();
    return;
  }
  if (!(await sessionTaskStartAllowed()) || isBusy) return;
  const childAction = ['select_candidate', 'retry_search'].includes(actionContext?.type)
    ? actionContext.type
    : '';
  if (childAction && !taskStateAllowsChildAction(childAction, childActionTarget)) return;
  addMessage({ message: displayValue, me: true });
  textInput.value = '';
  resizeComposer();
  const operation = ++operationVersion;
  let pending = null;
  setBusy(true);
  setStatus('working', '正在处理…');
  const isA3ErrorRetry = sessionContext.a3?.enabled && sessionContext.a3?.phase === 'ERROR';
  const autoPrepareRetry = isA3ErrorRetry && sessionContext.a3?.auto_prepare_all_enabled;
  const timeoutMs = autoPrepareRetry
    ? A3_AUTO_PREPARE_IDLE_TIMEOUT_MS
    : isA3ErrorRetry ? A3_TEXT_RETRY_TIMEOUT_MS : TEXT_TIMEOUT_MS;
  try {
    const data = await requestStream('/api/message/stream', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text: clean, ...(actionContext ? { action_context: actionContext } : {}) }),
    }, timeoutMs, '请求等待时间过长，请稍后重试。', (event) => {
      if (operation !== operationVersion) return;
      if (!pending) pending = addMessage({ message: event.message, variant: 'pending' }, false);
      else updatePendingMessage(pending, event.message);
      setStatus('working', event.message);
    }, '', { renewTimeoutOnProgress: true });
    if (operation !== operationVersion) return;
    pending?.remove();
    if (data.intent === 'a3_session_reset') {
      if (!applyResetSessionContext(data)) {
        throw clientProtocolError(
          '服务返回格式异常，请稍后重试。',
          'RESPONSE_INVALID',
          String(data?.request_id || createRequestId()),
        );
      }
      clearHistory();
      chat.replaceChildren();
      empty.hidden = false;
      setStatus('ready', '已开始新对话');
      return;
    }
    const response = responseItem(data);
    addMessage(response);
    setResponseStatus(data);
    maybeOpenAutoPreparedA3Sheet(response);
  } catch (error) {
    if (operation !== operationVersion) return;
    pending?.remove();
    if (error.message !== '当前识别已取消。') addMessage({
      message: error.message || '暂时无法处理，请再试一次。',
      variant: 'error',
      recoveryActions: error.retryable === false
        ? normalizeRecoveryActions(error.recoveryActions || [])
        : mergeRecoveryActions(error.recoveryActions || [], ['retry_request']),
      retryAction: { type: 'text', value: clean, displayValue, actionContext },
      ...protocolFields(error),
    });
    setStatus('error', '处理失败，可重新尝试');
  } finally {
    if (operation !== operationVersion) return;
    setBusy(false);
    refocusComposerOnDesktop();
  }
}

async function sendText() {
  await sendTextValue(textInput.value);
}

function addLocalUploadPreview(preview) {
  const row = addMessage({
    message: '我发了一张题图。',
    me: true,
    images: [preview],
    imageAlt: '待上传的题图',
  }, false);
  row.dataset.startedAt = String(Date.now());
  return row;
}

async function retryTextAction(action, childActionTarget = null) {
  const retry = normalizeRetryAction(action);
  if (!retry || isBusy) return;
  await sendTextValue(retry.value, retry.displayValue, retry.actionContext, childActionTarget);
}

function setUploadRowStatus(row, message, variant = '') {
  row.classList.remove('error');
  if (variant) row.classList.add(variant);
  const paragraph = row.querySelector('.message-text');
  if (paragraph) paragraph.textContent = message;
  row.querySelector('.retry-upload')?.remove();
}

function setUploadRowPreview(row, url) {
  const image = row.querySelector('img');
  if (image) image.src = url;
}

function addUploadFailure(row, message, prepared, recoveryActions = [], protocol = {}) {
  const fullMessage = `${message} 裁剪后的图片已保留，可直接重新上传。`;
  setUploadRowStatus(row, fullMessage, 'error');
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.className = 'retry-upload';
  retry.textContent = '重新上传';
  retry.addEventListener('click', () => retryUpload(row, prepared));
  row.querySelector('.message-content')?.append(retry);
  addMessage({
    message: fullMessage, variant: 'error',
    recoveryActions: mergeRecoveryActions(recoveryActions, ['reupload']),
    ...protocolFields(protocol),
  });
  return row;
}

async function submitPreparedImage(prepared, uploadRow) {
  if (isBusy) return;
  setUploadRowStatus(uploadRow, '我发了一张题图。');
  const operation = ++operationVersion;
  const pending = addMessage({ message: '正在识别题目', variant: 'pending' }, false);
  setBusy(true);
  setStatus('working', '正在识别题目…');
  try {
    const formData = new FormData();
    formData.append('file', prepared.blob, prepared.filename);
    debugUploadMetadata('form-data:file', prepared.blob, prepared.filename);
    const autoPrepareAll = Boolean(sessionContext.a3?.auto_prepare_all_enabled);
    const data = await requestStream('/api/image/stream', {
      method: 'POST', body: formData,
    }, autoPrepareAll
      ? A3_AUTO_PREPARE_IDLE_TIMEOUT_MS
      : sessionContext.a3?.enabled ? A3_TIMEOUT_MS : IMAGE_TIMEOUT_MS,
    '网络上传或题图识别超时，请直接重新上传。', (event) => {
      if (operation !== operationVersion) return;
      updatePendingMessage(pending, event.message);
      setStatus('working', event.message);
    }, '网络上传失败，请检查网络后重试。', {
      renewTimeoutOnProgress: autoPrepareAll,
    });
    if (operation !== operationVersion) return;
    if (!isPersistentImage(data.uploaded_image)) throw new UserVisibleError('服务端处理失败，未返回已上传的题图，请直接重新上传。');
    pending.remove();
    setUploadRowPreview(uploadRow, data.uploaded_image);
    setUploadRowStatus(uploadRow, '我发了一张题图。');
    const response = responseItem(data);
    remember({
      message: '我发了一张题图。', me: true, images: [data.uploaded_image],
      imageAlt: '已上传题图', taskRevision: response.taskRevision,
      createdAt: Number(uploadRow.dataset.startedAt || Date.now()),
    });
    releaseObjectUrl(prepared.preview);
    clearPendingUpload({ releasePreview: false });
    addMessage(response);
    setResponseStatus(data);
    maybeOpenAutoPreparedA3Sheet(response);
  } catch (error) {
    if (operation !== operationVersion) return;
    pending.remove();
    addUploadFailure(
      uploadRow,
      error.message || '服务端处理失败，请稍后重试。',
      prepared,
      error.recoveryActions || [],
      error,
    );
    setStatus('error', '上传失败，可直接重试');
  } finally {
    if (operation !== operationVersion) return;
    setBusy(false);
    refocusComposerOnDesktop();
  }
}

async function retryUpload(row, prepared) {
  if (pendingUpload !== prepared || isBusy) return;
  await submitPreparedImage(prepared, row);
}

async function uploadImage(selected) {
  if (isBusy) return;
  if (!(await sessionTaskStartAllowed()) || isBusy) return;
  const validationError = validateImage(selected);
  fileInput.value = '';
  if (validationError) {
    const code = validationError.includes('太大')
      ? 'UPLOAD_TOO_LARGE'
      : validationError.includes('格式') ? 'UPLOAD_UNSUPPORTED_FORMAT' : 'UPLOAD_REQUIRED';
    addMessage({
      message: validationError,
      variant: 'error',
      recoveryActions: ['reupload'],
      status: 'NEEDS_INPUT', layer: 'upload', code, retryable: false,
      action: 'retry_upload', requestId: createRequestId(), searchId: '',
    });
    return;
  }
  invalidateCandidateActions();
  const sourcePreview = URL.createObjectURL(selected);
  objectUrls.add(sourcePreview);
  const uploadRow = addLocalUploadPreview(sourcePreview);
  const operation = ++operationVersion;
  setBusy(true);
  setStatus('working', '正在处理题图…');
  try {
    const prepared = await normalizeImage(selected, sourcePreview);
    if (operation !== operationVersion) {
      releaseObjectUrl(prepared.preview);
      return;
    }
    setUploadRowPreview(uploadRow, prepared.preview);
    releaseObjectUrl(sourcePreview);
    clearPendingUpload();
    pendingUpload = prepared;
  } catch (error) {
    if (operation === operationVersion) {
      setUploadRowStatus(uploadRow, error.message || '裁剪处理失败，请重新选择图片。', 'error');
      addMessage({
        message: error.message || '图片处理失败，请重新选择图片。',
        variant: 'error', recoveryActions: ['reupload'],
        status: 'NEEDS_INPUT', layer: 'upload', code: 'UPLOAD_DECODE_FAILED',
        retryable: false, action: 'retry_upload', requestId: createRequestId(), searchId: '',
      });
      setStatus('error', '图片处理失败');
    }
    return;
  } finally {
    if (operation === operationVersion) setBusy(false);
  }
  if (operation === operationVersion) await submitPreparedImage(pendingUpload, uploadRow);
}

function openDrawer() {
  focusBeforeModal = document.activeElement;
  drawerBackdrop.hidden = false;
  drawer.classList.add('is-open');
  drawer.inert = false;
  drawer.setAttribute('aria-hidden', 'false');
  menuButton.setAttribute('aria-expanded', 'true');
  closeDrawerButton.focus();
}

function closeDrawer() {
  if (!drawer.classList.contains('is-open')) return;
  drawer.classList.remove('is-open');
  drawer.setAttribute('aria-hidden', 'true');
  drawer.inert = true;
  menuButton.setAttribute('aria-expanded', 'false');
  drawerBackdrop.hidden = true;
  focusBeforeModal?.focus();
}

function hideDropOverlay() {
  dragDepth = 0;
  dropOverlay.classList.remove('is-visible');
  dropOverlay.setAttribute('aria-hidden', 'true');
}

function hasDraggedFiles(event) {
  return Array.from(event.dataTransfer?.types || []).includes('Files');
}

async function resetConversation() {
  if (activeController) activeController.abort('new-chat');
  const operation = ++operationVersion;
  setBusy(true);
  closeDrawer();
  setStatus('working', '正在创建新对话…');
  try {
    const data = await request('/api/reset', { method: 'POST' }, TEXT_TIMEOUT_MS, '新对话创建超时，请稍后重试。');
    if (operation !== operationVersion) return;
    if (!applyResetSessionContext(data)) {
      throw clientProtocolError(
        '服务返回格式异常，请稍后重试。',
        'RESPONSE_INVALID',
        String(data?.request_id || createRequestId()),
      );
    }
    clearHistory();
    chat.replaceChildren();
    empty.hidden = false;
    setStatus('ready', '已开始新对话');
  } catch (error) {
    if (operation !== operationVersion) return;
    addMessage({
      message: error.message || '新对话创建失败，请稍后重试。',
      variant: 'error', recoveryActions: error.recoveryActions || [],
    });
    setStatus('error', '新对话创建失败');
  } finally {
    if (operation !== operationVersion) return;
    setBusy(false);
    refocusComposerOnDesktop();
  }
}

async function retryConnection() {
  if (isBusy) return;
  setStatus('working', '正在恢复会话…');
  await runSessionBootstrap();
}

form.addEventListener('submit', (event) => { event.preventDefault(); sendText(); });
textInput.addEventListener('input', () => { resizeComposer(); updateComposer(); });
textInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    sendText();
  }
});
attach.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); fileInput.click(); }
});
fileInput.addEventListener('change', () => uploadImage(fileInput.files[0]));
heroUpload.addEventListener('click', () => fileInput.click());
menuButton.addEventListener('click', openDrawer);
closeDrawerButton.addEventListener('click', closeDrawer);
drawerBackdrop.addEventListener('click', closeDrawer);
newChatButton.addEventListener('click', resetConversation);
topNewChatButton.addEventListener('click', resetConversation);
lightboxClose.addEventListener('click', closeLightbox);
lightbox.addEventListener('click', (event) => { if (event.target === lightbox) closeLightbox(); });
a3CropBack.addEventListener('click', () => requestCloseA3Crop({ dismiss: true }));
a3Reselect.addEventListener('click', () => openA3Sheet(workflowActionTargetFromControl(a3Reselect)));
a3SheetClose.addEventListener('click', closeA3Sheet);
a3SheetBackdrop.addEventListener('click', (event) => { if (event.target === a3SheetBackdrop) closeA3Sheet(); });
a3Prepare.addEventListener('click', prepareA3Units);
a3SheetOverlay.addEventListener('click', () => {
  if (!a3SheetOverlayImage.src) return;
  openLightbox(a3SheetOverlayImage.src, '自动裁剪标签预览');
});
a3ExampleButton.addEventListener('click', openA3Example);
a3ExampleClose.addEventListener('click', closeA3Example);
a3ExampleBackdrop.addEventListener('click', (event) => { if (event.target === a3ExampleBackdrop) closeA3Example(); });
a3ImageFrame.addEventListener('pointerdown', startA3Selection);
a3ImageFrame.addEventListener('pointermove', moveA3Selection);
a3ImageFrame.addEventListener('pointerup', endA3Selection);
a3ImageFrame.addEventListener('pointercancel', endA3Selection);
a3SourceImage.addEventListener('load', fitA3Image);
a3Submit.addEventListener('click', submitA3Crop);
feedbackClose.addEventListener('click', closeFeedback);
feedbackBackdrop.addEventListener('click', (event) => { if (event.target === feedbackBackdrop) closeFeedback(); });
feedbackCancel.addEventListener('click', cancelFeedback);
feedbackSubmit.addEventListener('click', submitFeedback);
authorContactClose.addEventListener('click', closeAuthorContact);
authorContactBackdrop.addEventListener('click', (event) => { if (event.target === authorContactBackdrop) closeAuthorContact(); });
authorContactCopy.addEventListener('click', copyAuthorContact);
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  if (!a3ExampleBackdrop.hidden) closeA3Example();
  else if (!a3SheetBackdrop.hidden) closeA3Sheet();
  else if (!a3CropWorkspace.hidden) requestCloseA3Crop({ dismiss: true });
  else if (!feedbackBackdrop.hidden) closeFeedback();
  else if (!authorContactBackdrop.hidden) closeAuthorContact();
  else if (!lightbox.hidden) closeLightbox();
  else closeDrawer();
});
window.addEventListener('popstate', (event) => {
  const historyState = event?.state ?? window.history.state;
  const marker = a3CropHistoryMarker(historyState);
  const markerActive = marker.active;
  if (!a3CropHistoryActive) {
    if (!markerActive) return;
    const a3 = a3Current();
    const cropTarget = marker.target || currentA3CropActionTarget(a3);
    if (cropTarget && taskStateAllowsA3Action('submit_crop', cropTarget, a3)) {
      a3CropHistoryActive = true;
      a3CropHistoryKey = marker.key || a3DraftKey(a3, cropTarget);
      openA3Crop(cropTarget, { force: true });
    } else {
      clearA3CropHistoryState();
    }
    return;
  }
  if (markerActive) return;
  const previousHistoryKey = a3CropHistoryKey;
  a3CropHistoryActive = false;
  a3CropHistoryKey = '';
  const currentKey = a3DraftKey(a3Current(), workflowActionTargetFromControl(a3Submit));
  const pendingClose = a3PendingClose || { dismiss: true, key: currentKey || previousHistoryKey };
  a3PendingClose = null;
  if (a3CropWorkspace.hidden || currentKey === pendingClose.key) {
    finishCloseA3Crop({ dismiss: pendingClose.dismiss, dismissKey: pendingClose.key });
    return;
  }
  const reboundTarget = workflowActionTargetFromControl(a3Submit);
  window.history.pushState({
    ...(window.history.state || {}),
    a3Crop: {
      workflowId: reboundTarget.workflowId,
      workflowRevision: reboundTarget.workflowRevision,
      unitId: reboundTarget.unitId,
    },
  }, '');
  a3CropHistoryActive = true;
  a3CropHistoryKey = a3DraftKey(a3Current(), reboundTarget);
});
document.addEventListener('dragenter', (event) => {
  if (!hasDraggedFiles(event)) return;
  event.preventDefault();
  if (isBusy) return;
  dragDepth += 1;
  dropOverlay.classList.add('is-visible');
  dropOverlay.setAttribute('aria-hidden', 'false');
});
document.addEventListener('dragover', (event) => {
  if (!hasDraggedFiles(event)) return;
  event.preventDefault();
  if (event.dataTransfer) event.dataTransfer.dropEffect = isBusy ? 'none' : 'copy';
});
document.addEventListener('dragleave', (event) => {
  if (!hasDraggedFiles(event)) return;
  event.preventDefault();
  if (isBusy) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) hideDropOverlay();
});
document.addEventListener('drop', (event) => {
  if (!hasDraggedFiles(event)) return;
  event.preventDefault();
  const files = Array.from(event.dataTransfer?.files || []);
  hideDropOverlay();
  if (files.length > 1) {
    addMessage({
      message: '当前一次处理一张题图，请先上传其中一张。',
      variant: 'error', recoveryActions: ['reupload'],
    });
    return;
  }
  if (!isBusy) uploadImage(files[0]);
});
window.addEventListener('blur', hideDropOverlay);
window.addEventListener('pagehide', releaseAllObjectUrls);
window.addEventListener('focus', expireHistoryIfNeeded);
window.addEventListener('storage', (event) => {
  if (event.key === SESSION_RESET_EVENT_KEY && event.newValue) {
    retireUnhandledSessionReset(event.newValue);
    return;
  }
  if (
    event.key === HISTORY_KEY
    || event.key === LEGACY_HISTORY_KEY
    || event.key === SESSION_ACTIVITY_KEY
  ) {
    refreshHistoryActivityFromStorage();
  }
});
window.addEventListener('offline', () => {
  setStatus('error', '当前网络已断开');
  showFailureNotice(
    'connection',
    '当前网络已断开。当前对话仍保留在本机，请恢复网络后重新连接。',
    ['retry_connection'],
    { status: 'ERROR', layer: 'network', code: 'NETWORK_UNAVAILABLE', retryable: true, action: 'retry_connection', request_id: createRequestId(), search_id: sessionContext.search_id || '' },
  );
});
window.addEventListener('online', retryConnection);
window.addEventListener('resize', () => {
  syncVisualViewport();
  if (!a3CropWorkspace.hidden) fitA3Image();
}, { passive: true });
window.addEventListener('orientationchange', syncVisualViewport, { passive: true });
window.visualViewport?.addEventListener('resize', syncVisualViewport, { passive: true });
window.visualViewport?.addEventListener('scroll', syncVisualViewport, { passive: true });
document.addEventListener('visibilitychange', () => { if (!document.hidden) expireHistoryIfNeeded(); });

syncVisualViewport();
restoreHistory();
resizeComposer();
updateComposer();
if (pendingHistoryStorageNotice && !sessionResetRequired) flushStartupNotices();
if (history.length || sessionResetRequired) retryConnection();
}
