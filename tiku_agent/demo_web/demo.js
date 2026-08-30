const $ = (selector) => document.querySelector(selector);
const taskStateV1 = globalThis.TikuTaskStateV1;
if (!taskStateV1) throw new Error('Task-state frontend model unavailable.');
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
const activeFailureNotices = new Set();
let pendingSessionExpiredNotice = false;
let pendingHistoryStorageNotice = '';
let sessionBootstrap = null;
let sessionContext = {
  session_valid: false, phase: 'IDLE', has_active_image: false,
  task_revision: 0, candidate_generation: '', candidate_count: 0, search_id: '',
};
const taskStateConsumer = taskStateV1.createTaskStateConsumer();
let taskStateContext = taskStateConsumer.current();
let a3SourceUrl = '';
let a3Bounds = null;
let a3Pointer = null;
let a3CropHistoryActive = false;
let a3PendingDismiss = true;
let a3DismissedKey = '';
let a3KnownRevision = 0;
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

function taskStateApiPath(url) {
  return String(url || '').split('?', 1)[0];
}

function beginTaskStateRequest(url, responseMode) {
  const paths = responseMode === 'stream' ? TASK_STATE_STREAM_PATHS : TASK_STATE_JSON_PATHS;
  if (!paths.has(taskStateApiPath(url))) return null;
  const request = taskStateConsumer.begin();
  taskStateContext = taskStateConsumer.current();
  return request;
}

function isTaskStateQueueNoUpdate(envelope) {
  return envelope?.layer === 'queue' && TASK_STATE_QUEUE_CODES.has(envelope?.code);
}

function consumeTaskStateResponse(request, envelope, { error = false } = {}) {
  if (request === null || (error && isTaskStateQueueNoUpdate(envelope))) return;
  taskStateContext = taskStateConsumer.consume(request, envelope);
}

function finishTaskStateRequest(request) {
  if (request === null) return;
  taskStateContext = taskStateConsumer.finish(request);
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
    historyLastActivityAt = Date.now();
  }
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify({
      savedAt: historyLastActivityAt,
      lastActivityAt: historyLastActivityAt,
      messages: history.slice(-HISTORY_LIMIT),
    }));
  } catch (_error) {
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
  const context = action.actionContext && typeof action.actionContext === 'object'
    ? {
        type: String(action.actionContext.type || ''),
        rank: Number(action.actionContext.rank || 0),
        task_revision: Number(action.actionContext.task_revision || 0),
        candidate_generation: String(action.actionContext.candidate_generation || ''),
      }
    : null;
  return {
    type: 'text', value, displayValue: String(action.displayValue || value),
    actionContext: context?.type === 'select_candidate' ? context : null,
  };
}

function mergeRecoveryActions(...groups) {
  return normalizeRecoveryActions(groups.flat());
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
  activeFailureNotices.add(noticeKey);
  return addMessage({
    message, variant: 'error', recoveryActions, noticeKey, ...protocolFields(protocol),
  });
}

function resolveFailureNotice(key) {
  activeFailureNotices.delete(String(key || '').trim());
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
    button.addEventListener('click', () => {
      if (action === 'relogin') window.location.assign('/invite');
      else if (action === 'reupload') fileInput.click();
      else if (action === 'new_chat') resetConversation();
      else if (action === 'retry_connection') retryConnection();
      else if (action === 'retry_request') retryTextAction(retryAction);
      else if (action === 'retry_search') sendTextValue('重试');
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

function createA3UnitActions(rawA3) {
  const a3 = normalizeA3Snapshot(rawA3);
  if (!a3 || !a3.units.length) return null;
  const current = normalizeA3Snapshot(sessionContext.a3);
  const currentRevision = Number(current?.task_revision || 0);
  const isCurrentList = current && currentRevision === Number(a3.task_revision || 0);
  if (a3.phase === 'A2_ACTIVE') {
    const remaining = current?.units.filter((unit) => !unit.completed && !unit.searched) || [];
    if (!isCurrentList || current?.phase !== 'A2_ACTIVE' || remaining.length <= 1) return null;
    const host = document.createElement('div');
    host.className = 'a3-unit-actions';
    host.dataset.a3Revision = String(a3.task_revision || 0);
    const switchButton = document.createElement('button');
    switchButton.type = 'button';
    switchButton.className = 'a3-unit-choice a3-switch-question';
    switchButton.dataset.a3Revision = String(a3.task_revision || 0);
    switchButton.textContent = '换题重新搜';
    switchButton.addEventListener('click', openA3Sheet);
    host.append(switchButton);
    return host;
  }
  if (a3.auto_crop_enabled && a3.phase === 'WAIT_UNIT_SELECTION') {
    const host = document.createElement('div');
    host.className = 'a3-unit-actions';
    host.dataset.a3Revision = String(a3.task_revision || 0);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'a3-unit-choice a3-open-auto-selection';
    const prepared = a3.units.filter((unit) => unit.requested && !unit.completed && !unit.searched).length;
    button.textContent = prepared ? `查看已准备题目（${prepared}）` : '选择要查询的题目';
    button.addEventListener('click', openA3Sheet);
    host.append(button);
    return host;
  }
  if (!['WAIT_UNIT_SELECTION', 'CROP_REQUIRED', 'COMPLETE'].includes(a3.phase)) return null;
  const host = document.createElement('div');
  host.className = 'a3-unit-actions';
  host.dataset.a3Revision = String(a3.task_revision || 0);
  const selectionAllowed = ['WAIT_UNIT_SELECTION', 'CROP_REQUIRED'].includes(current?.phase || '');
  a3.units.forEach((unit) => {
    const currentUnit = current?.units.find((item) => item.unit_id === unit.unit_id);
    const completed = Boolean(currentUnit?.completed || unit.completed);
    const searched = Boolean(currentUnit?.searched || unit.searched);
    const closed = completed || searched;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `a3-unit-choice${completed ? ' is-complete' : ''}`;
    button.dataset.a3UnitId = unit.unit_id;
    button.dataset.a3Revision = String(a3.task_revision || 0);
    button.disabled = !isCurrentList || !selectionAllowed || closed || !currentUnit;
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
    button.addEventListener('click', () => selectA3Unit(unit.unit_id));
    host.append(button);
  });
  if (current?.phase === 'CROP_REQUIRED' && current.selected_unit?.unit_id) {
    host.append(createA3ContinueCropButton());
  }
  return host;
}

function createA3ContinueCropButton() {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'a3-unit-choice a3-continue-crop';
  button.textContent = '继续裁剪';
  button.addEventListener('click', () => openA3Crop({ force: true }));
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
    const actionContext = {
      type: 'select_candidate', rank: index + 1,
      task_revision: Number(item.taskRevision || 0),
      candidate_generation: String(item.candidateGeneration || ''),
    };
    const isCurrent = sessionContext.session_valid
      && ['WAIT_CANDIDATE_CHOICE', 'ANSWERED'].includes(sessionContext.phase)
      && actionContext.task_revision === Number(sessionContext.task_revision || 0)
      && actionContext.candidate_generation
      && actionContext.candidate_generation === String(sessionContext.candidate_generation || '');
    choose.disabled = !isCurrent;
    choose.textContent = isCurrent ? '选择' : '候选已失效';
    choose.addEventListener('click', () => sendTextValue(`选择候选 ${index + 1}`, `选择候选 ${index + 1}`, actionContext));
    footer.append(label, choose);
    card.append(footer);
  }
  return card;
}

function addMessage(item, persist = true) {
  item = { ...item, createdAt: Number(item.createdAt || Date.now()) };
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
  empty.hidden = true;
  const article = document.createElement('article');
  article.className = `message${item.me ? ' user' : ''}${item.variant ? ` ${item.variant}` : ''}`;
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
  const a3Actions = createA3UnitActions(item.a3);
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
  syncA3ActionButtons();
  if (persist) remember(item);
  scrollToLatest();
  return article;
}

function renderHistory() {
  chat.replaceChildren();
  empty.hidden = history.length > 0;
  history.forEach((item) => addMessage({ ...item, images: (item.images || []).filter(isPersistentImage) }, false));
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
  try {
    const raw = localStorage.getItem(HISTORY_KEY) || localStorage.getItem(LEGACY_HISTORY_KEY);
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
      clearHistory();
      pendingSessionExpiredNotice = true;
      return;
    }
    historyLastActivityAt = activityAt;
    const storedMessages = stored.messages.slice(-HISTORY_LIMIT);
    const restoredMessages = storedMessages.filter((item, index) => (
      !isLegacyInlineOnlyMessage(item, index, storedMessages)
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
    history.forEach((item) => {
      const noticeKey = String(item.noticeKey || '').trim();
      if (noticeKey) activeFailureNotices.add(noticeKey);
    });
    localStorage.removeItem(LEGACY_HISTORY_KEY);
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
    const data = await request('/api/session', {}, 5000, '会话恢复超时。', false);
    updateSessionContext(data);
    resolveFailureNotice('connection');
    renderHistory();
    if (!data.session?.session_valid) {
      if (history.length) {
        clearHistory();
        renderHistory();
        showSessionExpiredNotice();
      } else {
        flushStartupNotices();
      }
      return;
    }
    flushStartupNotices();
    if (!isPersistentImage(data.uploaded_image)) return;
    for (let index = history.length - 1; index >= 0; index -= 1) {
      const item = history[index];
      if (item.me && item.message === '我发了一张题图。' && (!Array.isArray(item.images) || !item.images.length)) {
        item.images = [data.uploaded_image];
        saveHistory();
        renderHistory();
        return;
      }
    }
  } catch (_error) {
    flushStartupNotices();
    showFailureNotice(
      'connection',
      '暂时无法连接服务。当前对话仍保留在本机，请检查网络后重新连接。',
      ['retry_connection'],
      { status: 'ERROR', layer: 'network', code: 'NETWORK_UNAVAILABLE', retryable: true, action: 'retry_request', request_id: createRequestId(), search_id: sessionContext.search_id || '' },
    );
  }
}

function clearHistory() {
  history = [];
  activeFailureNotices.clear();
  historyLastActivityAt = 0;
  if (historyExpiryTimer !== null) clearTimeout(historyExpiryTimer);
  historyExpiryTimer = null;
  clearPendingUpload();
  releaseAllObjectUrls();
  a3SourceUrl = '';
  a3Bounds = null;
  a3LocalDrafts.clear();
  a3DismissedKey = '';
  a3KnownRevision = 0;
  localStorage.removeItem(HISTORY_KEY);
  localStorage.removeItem(LEGACY_HISTORY_KEY);
}

function expireHistoryIfNeeded() {
  if (!history.length || !Number.isFinite(historyLastActivityAt) || historyLastActivityAt <= 0) return false;
  if (Date.now() - historyLastActivityAt < HISTORY_TTL_MS) {
    scheduleHistoryExpiry();
    return false;
  }
  clearHistory();
  renderHistory();
  sessionContext = {
    session_valid: false, phase: 'IDLE', has_active_image: false,
    task_revision: 0, candidate_generation: '', candidate_count: 0,
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

async function request(url, options, timeoutMs, timeoutMessage, track = true, networkMessage = '') {
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
      throw safeHttpError(response.status, data, requestId);
    }
    if (!contentType.includes('application/json')) throw clientProtocolError('服务返回格式异常，请稍后重试。', 'RESPONSE_INVALID', requestId);
    consumeTaskStateResponse(taskStateRequest, data);
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
) {
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
          clearTimeout(timer);
          try { await reader.cancel(); } catch (_error) { /* terminal result already won */ }
          if (!terminalResult) throw clientProtocolError('服务返回格式异常，请稍后重试。', 'RESPONSE_INVALID', requestId);
          return terminalResult;
        }
        if (event.type === 'error') {
          consumeTaskStateResponse(taskStateRequest, event, { error: true });
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
  updateSessionContext(data);
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
    variant: protocol.status === 'ERROR' || failure
      ? 'error'
      : protocol.status === 'PARTIAL' ? 'partial' : '',
    recoveryActions: recoveryAction ? [recoveryAction] : [],
    authorContact: normalizeAuthorContact(data?.author_contact),
    messageId: createMessageId(),
    responseId: String(data.response_id || ''),
    createdAt: Date.now(),
    a3: normalizeA3Snapshot(data.session?.a3),
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
  if (response.intent === 'a3_units_prepared' && response.a3?.auto_prepare_all_units) {
    openA3Sheet();
  }
}

function updateSessionContext(data) {
  if (!data?.session) return;
  sessionContext = {
    ...sessionContext,
    ...data.session,
    a3: normalizeA3Snapshot(data.session.a3),
  };
  if (isPersistentImage(data.uploaded_image)) a3SourceUrl = data.uploaded_image;
  syncA3Interface();
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

function a3CropReviewMessage(a3 = a3Current()) {
  const code = String(a3?.crop_review_code || '');
  return A3_CROP_REVIEW_MESSAGES[code]
    || '裁剪结果未通过，请重新选择区域裁剪。';
}

function a3DraftKey(a3 = a3Current()) {
  const unitId = a3?.selected_unit?.unit_id || '';
  return unitId ? `${Number(a3.task_revision || 0)}:${unitId}` : '';
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
  const a3 = a3Current();
  document.querySelectorAll('.a3-unit-choice[data-a3-unit-id]').forEach((button) => {
    const unit = a3?.units.find((item) => item.unit_id === button.dataset.a3UnitId);
    const sameRevision = Number(button.dataset.a3Revision || 0) === Number(a3?.task_revision || 0);
    const completed = Boolean(unit?.completed);
    const searched = Boolean(unit?.searched);
    const closed = completed || searched;
    const selected = Boolean(unit?.selected);
    const selectionAllowed = ['WAIT_UNIT_SELECTION', 'CROP_REQUIRED'].includes(a3?.phase || '');
    if (unit && searched) button.textContent = `${unit.display_label || '未标号题目'} · 已检索`;
    else if (unit && !completed) button.textContent = unit.display_label || '未标号题目';
    else if (unit && completed) {
      const label = button.querySelector('span');
      if (label) label.textContent = `${unit.display_label || '未标号题目'} · 已完成`;
    }
    button.disabled = !unit || !sameRevision || !selectionAllowed || closed || selected;
    button.classList.toggle('is-complete', completed);
  });
  const actionGroups = Array.from(document.querySelectorAll('.a3-unit-actions'));
  const currentGroups = actionGroups.filter((host) => (
    Number(host.dataset.a3Revision || 0) === Number(a3?.task_revision || 0)
  ));
  let latestGroup = currentGroups.at(-1) || null;
  const canContinueCrop = a3?.phase === 'CROP_REQUIRED' && Boolean(a3.selected_unit?.unit_id);
  if (canContinueCrop && !latestGroup) {
    const latestAssistantContent = Array.from(document.querySelectorAll('.message:not(.user) .message-content')).at(-1);
    if (latestAssistantContent) {
      latestGroup = document.createElement('div');
      latestGroup.className = 'a3-unit-actions';
      latestGroup.dataset.a3Revision = String(a3.task_revision || 0);
      latestAssistantContent.append(latestGroup);
      actionGroups.push(latestGroup);
    }
  }
  if (canContinueCrop && latestGroup && !latestGroup.querySelector('.a3-continue-crop')) {
    latestGroup.append(createA3ContinueCropButton());
  }
  actionGroups.forEach((host) => {
    host.hidden = host !== latestGroup;
  });
  document.querySelectorAll('.a3-switch-question').forEach((button) => {
    const host = button.closest('.a3-unit-actions');
    const available = Boolean(host && !host.hidden && a3?.phase === 'A2_ACTIVE');
    button.hidden = !available;
    button.disabled = !available;
  });
  document.querySelectorAll('.a3-continue-crop').forEach((button) => {
    const host = button.closest('.a3-unit-actions');
    const available = Boolean(host && !host.hidden && canContinueCrop);
    button.hidden = !available;
    button.disabled = !available;
  });
}

function syncA3Interface() {
  const a3 = a3Current();
  syncA3ActionButtons();
  if (!a3) {
    if (!a3CropWorkspace.hidden) requestCloseA3Crop({ dismiss: false });
    return;
  }
  if (a3KnownRevision && a3KnownRevision !== a3.task_revision) {
    a3LocalDrafts.clear();
    a3PrepareSelection.clear();
    a3DismissedKey = '';
  }
  a3KnownRevision = a3.task_revision;
  renderA3SheetUnits(a3);
  if (a3.phase === 'CROP_REQUIRED') {
    const key = a3DraftKey(a3);
    if (key && key !== a3DismissedKey) openA3Crop();
  } else if (!a3CropWorkspace.hidden) {
    requestCloseA3Crop({ dismiss: false });
  }
}

async function selectA3Unit(unitId) {
  if (!unitId || isBusy) return;
  a3DismissedKey = '';
  closeA3Sheet();
  const operation = ++operationVersion;
  const pending = addMessage({ message: '正在打开裁剪页', variant: 'pending' }, false);
  setBusy(true);
  setStatus('working', '正在准备裁剪…');
  try {
    const data = await requestStream('/api/a3/select/stream', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ unit_id: unitId, task_revision: Number(a3Current()?.task_revision || 0) }),
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
      variant: 'error', recoveryActions: error.recoveryActions || [],
      ...protocolFields(error),
    });
    setStatus('error', '选题失败');
  } finally {
    if (operation !== operationVersion) return;
    setBusy(false);
  }
}

function openA3Crop({ force = false } = {}) {
  const a3 = a3Current();
  const selected = a3?.selected_unit;
  if (!a3 || a3.phase !== 'CROP_REQUIRED' || !selected?.unit_id) return;
  if (!a3SourceUrl) {
    setStatus('error', '原始题图未能恢复');
    return;
  }
  const key = a3DraftKey(a3);
  if (force) a3DismissedKey = '';
  if (!force && key === a3DismissedKey) return;
  a3CropLabel.textContent = selected.display_label || '未标号题目';
  const contextText = String(selected.context_text || '').trim();
  a3Context.hidden = !contextText;
  a3ContextText.textContent = contextText;
  a3Reselect.hidden = a3.units.filter((unit) => !unit.completed && !unit.searched).length <= 1;
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
    window.history.pushState({ ...(window.history.state || {}), a3Crop: true }, '');
    a3CropHistoryActive = true;
  }
  a3CropBack.focus();
}

function finishCloseA3Crop({ dismiss = true } = {}) {
  if (a3CropWorkspace.hidden) return;
  if (dismiss) a3DismissedKey = a3DraftKey();
  a3CropWorkspace.hidden = true;
  a3CropWorkspace.setAttribute('aria-hidden', 'true');
  a3Pointer = null;
  closeA3Sheet();
  closeA3Example();
  delete document.body.dataset.modal;
}

function requestCloseA3Crop({ dismiss = true } = {}) {
  a3PendingDismiss = dismiss;
  if (a3CropHistoryActive) {
    window.history.back();
    return;
  }
  finishCloseA3Crop({ dismiss });
}

function renderA3Selection() {
  const bounds = validA3Bounds(a3Bounds);
  a3Selection.hidden = !bounds;
  a3ImageHint.hidden = Boolean(bounds);
  a3Submit.disabled = !bounds || isBusy;
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
  if (isBusy || a3SourceImage.complete === false || a3Pointer) return;
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
  else a3LocalDrafts.set(a3DraftKey(), { ...a3Bounds });
  renderA3Selection();
}

async function submitA3Crop() {
  const bounds = validA3Bounds(a3Bounds);
  if (!bounds || isBusy) return;
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
        bounds,
        unit_id: String(a3Current()?.selected_unit?.unit_id || ''),
        task_revision: Number(a3Current()?.task_revision || 0),
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
      variant: 'error', recoveryActions: error.recoveryActions || [],
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
  a3SheetUnits.replaceChildren();
  if (a3.auto_crop_enabled) {
    renderA3AutoSheetUnits(a3);
    return;
  }
  a3SheetSubtitle.hidden = false;
  a3SheetSubtitle.textContent = '选择其他题目后会重新裁剪并搜索';
  a3SheetOverlay.hidden = !a3.auto_crop_overlay_available;
  if (a3.auto_crop_overlay_available) {
    a3SheetOverlayImage.src = `/api/a3/overlay?revision=${encodeURIComponent(a3.task_revision)}`;
  }
  a3SheetFooter.hidden = true;
  a3.units.forEach((unit) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'a3-sheet-unit';
    const isCurrent = unit.unit_id === a3.selected_unit?.unit_id;
    button.disabled = a3.page_finished || unit.completed || unit.searched || isCurrent;
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
    button.addEventListener('click', () => selectA3Unit(unit.unit_id));
    a3SheetUnits.append(button);
  });
}

function renderA3AutoSheetUnits(a3) {
  const currentIds = new Set(a3.units.filter((unit) => !unit.completed && !unit.searched && !unit.requested).map((unit) => unit.unit_id));
  Array.from(a3PrepareSelection).forEach((unitId) => {
    if (!currentIds.has(unitId)) a3PrepareSelection.delete(unitId);
  });
  a3SheetSubtitle.hidden = a3.auto_prepare_all_units;
  a3SheetSubtitle.textContent = a3.auto_prepare_all_units
    ? ''
    : '可多选；只校验你准备查询的裁图';
  a3SheetOverlay.hidden = !a3.auto_crop_overlay_available;
  if (a3.auto_crop_overlay_available) {
    a3SheetOverlayImage.src = `/api/a3/overlay?revision=${encodeURIComponent(a3.task_revision)}`;
  }
  a3SheetFooter.hidden = a3.page_finished
    || !a3.units.some((unit) => !unit.completed && !unit.searched && !unit.requested);
  a3.units.forEach((unit) => {
    const prepared = unit.requested;
    const host = document.createElement(prepared ? 'button' : 'label');
    if (prepared) host.type = 'button';
    const closed = a3.page_finished || unit.completed || unit.searched;
    host.className = `a3-auto-unit${closed ? ' is-closed' : ''}${prepared ? ' is-prepared' : ''}`;
    const visual = document.createElement('span');
    visual.className = 'a3-auto-unit-visual';
    if (unit.crop_available) {
      const image = document.createElement('img');
      image.src = `/api/a3/crop/${encodeURIComponent(unit.unit_id)}?revision=${encodeURIComponent(a3.task_revision)}`;
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
    if (prepared) {
      const arrow = document.createElement('span');
      arrow.className = 'a3-auto-unit-arrow';
      arrow.textContent = unit.completed ? '已完成' : unit.searched ? '已检索' : '继续';
      host.append(arrow);
      host.disabled = closed || unit.selected;
      if (!host.disabled) host.addEventListener('click', () => selectA3Unit(unit.unit_id));
    } else {
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = a3PrepareSelection.has(unit.unit_id);
      input.disabled = closed;
      input.addEventListener('change', () => {
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
  a3SheetCount.textContent = count ? `已选择 ${count} 道` : '尚未选择';
  a3Prepare.disabled = isBusy || count === 0;
  a3Prepare.textContent = count ? `校验所选 ${count} 道题` : '校验所选题目';
}

async function prepareA3Units() {
  const a3 = a3Current();
  const unitIds = Array.from(a3PrepareSelection);
  if (!a3?.auto_crop_enabled || !unitIds.length || isBusy) return;
  const operation = ++operationVersion;
  setBusy(true);
  updateA3PrepareFooter();
  setStatus('working', '正在并发校验所选裁图…');
  try {
    const data = await requestStream('/api/a3/prepare/stream', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ unit_ids: unitIds, task_revision: Number(a3.task_revision || 0) }),
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
      variant: 'error', recoveryActions: error.recoveryActions || [],
      ...protocolFields(error),
    });
    setStatus('error', '裁图校验失败');
  } finally {
    if (operation !== operationVersion) return;
    setBusy(false);
    updateA3PrepareFooter();
  }
}

function openA3Sheet() {
  const a3 = a3Current();
  if (!a3 || (!a3.auto_crop_enabled && a3.units.length <= 1)) return;
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

async function sendTextValue(value, displayValue = value, actionContext = null) {
  const clean = String(value || '').trim();
  if (!clean || isBusy) return;
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
      clearHistory();
      if (!a3CropWorkspace.hidden) finishCloseA3Crop({ dismiss: false });
      a3CropHistoryActive = false;
      sessionContext = {
        session_valid: false, phase: 'IDLE', has_active_image: false,
        task_revision: 0, candidate_generation: '', candidate_count: 0, search_id: '',
      };
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

async function retryTextAction(action) {
  const retry = normalizeRetryAction(action);
  if (!retry || isBusy) return;
  await sendTextValue(retry.value, retry.displayValue, retry.actionContext);
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
  await sessionBootstrap;
  if (isBusy) return;
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
    await request('/api/reset', { method: 'POST' }, TEXT_TIMEOUT_MS, '新对话创建超时，请稍后重试。');
    if (operation !== operationVersion) return;
    clearHistory();
    if (!a3CropWorkspace.hidden) finishCloseA3Crop({ dismiss: false });
    a3CropHistoryActive = false;
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

async function checkHealth() {
  try {
    await request('/health', {}, 5000, '服务连接超时。', false);
    resolveFailureNotice('connection');
    if (!isBusy) setStatus('ready', '准备就绪');
    return true;
  } catch (_error) {
    setStatus('error', '本地服务未连接');
    showFailureNotice(
      'connection',
      '暂时无法连接服务。当前对话仍保留在本机，请检查网络后重新连接。',
      ['retry_connection'],
      { status: 'ERROR', layer: 'network', code: 'NETWORK_UNAVAILABLE', retryable: true, action: 'retry_request', request_id: createRequestId(), search_id: sessionContext.search_id || '' },
    );
    return false;
  }
}

async function retryConnection() {
  if (isBusy) return;
  const healthy = await checkHealth();
  if (healthy && !isBusy) await repairUploadedImageHistory();
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
a3Reselect.addEventListener('click', openA3Sheet);
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
window.addEventListener('popstate', () => {
  if (!a3CropHistoryActive) return;
  a3CropHistoryActive = false;
  finishCloseA3Crop({ dismiss: a3PendingDismiss });
  a3PendingDismiss = true;
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
window.addEventListener('offline', () => {
  setStatus('error', '当前网络已断开');
  showFailureNotice(
    'connection',
    '当前网络已断开。当前对话仍保留在本机，请恢复网络后重新连接。',
    ['retry_connection'],
    { status: 'ERROR', layer: 'network', code: 'NETWORK_UNAVAILABLE', retryable: true, action: 'retry_request', request_id: createRequestId(), search_id: sessionContext.search_id || '' },
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
sessionBootstrap = repairUploadedImageHistory();
resizeComposer();
updateComposer();
checkHealth();
