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

const TEXT_TIMEOUT_MS = 60000;
const IMAGE_TIMEOUT_MS = 90000;
const MAX_IMAGE_BYTES = 15 * 1024 * 1024;
const HISTORY_TTL_MS = 2 * 60 * 60 * 1000;
const HISTORY_LIMIT = 50;
const HISTORY_KEY = 'tiku-agent-current-chat-v2';
const LEGACY_HISTORY_KEY = 'tiku-agent-current-chat-v1';
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
let sessionContext = {
  session_valid: false, phase: 'IDLE', has_active_image: false,
  task_revision: 0, candidate_generation: '', candidate_count: 0,
};
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
    // The demo remains usable when browser storage is unavailable.
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
    candidateGeneration: String(item.candidateGeneration || ''),
    messageId: String(item.messageId || ''),
    createdAt: Number(item.createdAt || 0),
    feedback: item.feedback || null,
  });
  history = history.slice(-HISTORY_LIMIT);
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
  const visible = target >= 0 ? history.slice(0, target + 1) : history.slice();
  return visible.map((item) => ({
    message: String(item.message || ''),
    me: Boolean(item.me),
    images: (item.images || []).filter(isPersistentImage),
    imageAlt: String(item.imageAlt || '题目图片'),
    intent: String(item.intent || ''),
    variant: String(item.variant || ''),
    taskRevision: Number(item.taskRevision || 0),
    messageId: String(item.messageId || ''),
    createdAt: Number(item.createdAt || 0),
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

async function submitFeedback() {
  if (!activeFeedback || feedbackRequestPending) return;
  const context = activeFeedback;
  setFeedbackPending(true);
  feedbackSubmit.textContent = '正在提交…';
  feedbackError.hidden = true;
  try {
    const payload = {
      message_id: context.item.messageId,
      rating: context.rating,
      tags: Array.from(context.tags),
      detail: feedbackDetail.value.trim(),
      conversation: feedbackConversation(context.item.messageId),
    };
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
  }
}

async function cancelFeedback() {
  if (!activeFeedback || feedbackRequestPending || feedbackCancel.hidden) return;
  const context = activeFeedback;
  setFeedbackPending(true);
  feedbackCancel.textContent = '正在取消…';
  feedbackError.hidden = true;
  try {
    await request(`/api/feedback/${encodeURIComponent(context.item.messageId)}`, {
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
    label.textContent = `候选 ${index + 1}`;
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
    choose.textContent = isCurrent ? '选择这道题' : '候选已失效';
    choose.addEventListener('click', () => sendTextValue(`选择候选 ${index + 1}`, `选择候选 ${index + 1}`, actionContext));
    footer.append(label, choose);
    card.append(footer);
  }
  return card;
}

function addMessage(item, persist = true) {
  item = { ...item, createdAt: Number(item.createdAt || Date.now()) };
  const feedbackEligible = !item.me && !item.variant;
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
      answerLabel.textContent = '题库答案';
      content.append(answerLabel);
    }
    const grid = document.createElement('div');
    grid.className = 'media-grid';
    images.forEach((url, index) => grid.append(createMediaCard(url, index, { ...item, images })));
    content.append(grid);
  }
  if (feedbackEligible) content.append(createMessageActions(item, article));
  article.append(content);
  chat.append(article);
  if (persist) remember(item);
  scrollToLatest();
  return article;
}

function renderHistory() {
  chat.replaceChildren();
  empty.hidden = history.length > 0;
  history.forEach((item) => addMessage({ ...item, images: (item.images || []).filter(isPersistentImage) }, false));
}

function restoreHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_KEY) || localStorage.getItem(LEGACY_HISTORY_KEY);
    const stored = JSON.parse(raw || 'null');
    const activityAt = Number(stored?.lastActivityAt ?? stored?.savedAt);
    const now = Date.now();
    if (!stored || !Array.isArray(stored.messages) || !Number.isFinite(activityAt) || activityAt > now + 60000 || now - activityAt >= HISTORY_TTL_MS) {
      clearHistory();
      return;
    }
    historyLastActivityAt = activityAt;
    history = stored.messages.slice(-HISTORY_LIMIT).map((item) => {
      if (item.me || item.variant) return item;
      return {
        ...item,
        messageId: item.messageId || createMessageId(),
        createdAt: Number(item.createdAt || activityAt),
      };
    });
    localStorage.removeItem(LEGACY_HISTORY_KEY);
    saveHistory();
    renderHistory();
  } catch (_error) {
    clearHistory();
  }
}

async function repairUploadedImageHistory() {
  try {
    const data = await request('/api/session', {}, 5000, '会话恢复超时。', false);
    updateSessionContext(data);
    renderHistory();
    if (!data.session?.session_valid) {
      if (history.length) {
        clearHistory();
        renderHistory();
      }
      return;
    }
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
    // A temporary network failure should not discard unexpired local history.
  }
}

function clearHistory() {
  history = [];
  historyLastActivityAt = 0;
  if (historyExpiryTimer !== null) clearTimeout(historyExpiryTimer);
  historyExpiryTimer = null;
  clearPendingUpload();
  releaseAllObjectUrls();
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
  setStatus('ready', '临时会话已清理');
  return true;
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
  const canvas = document.createElement('canvas');
  canvas.width = image.naturalWidth;
  canvas.height = image.naturalHeight;
  const context = canvas.getContext('2d');
  if (!context) throw new Error('裁剪处理失败，请重新选择图片。');
  context.fillStyle = '#fff';
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.drawImage(image, 0, 0);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.92));
  if (!blob) throw new Error('裁剪处理失败，请重新选择图片。');
  if (blob.size > MAX_IMAGE_BYTES) throw new Error('图片太大，请上传不超过 15MB 的图片。');
  const filename = `cropped_${Date.now()}.jpg`;
  const preview = URL.createObjectURL(blob);
  objectUrls.add(preview);
  debugUploadMetadata('normalized', blob, filename);
  return { blob, filename, preview };
}

function safeHttpError(status, data) {
  const detail = typeof data?.detail === 'string' ? data.detail.toLowerCase() : '';
  if (status === 413 || detail.includes('too large')) return '图片太大，请上传不超过 15MB 的图片。';
  if (status === 415 || detail.includes('unsupported image')) return '图片格式不支持，请上传 PNG、JPG、WEBP、GIF 或 BMP 图片。';
  if (status === 400 && detail.includes('invalid image')) return '服务端无法读取该图片，请检查图片后重试。';
  if (status >= 500) return '服务端处理失败，请稍后重试。';
  if (status === 400) return '提交的内容无法处理，请检查后重试。';
  if (status === 401 || status === 403) return '当前请求无权处理。';
  if (status === 429) return data?.detail || '当前请求较多，请稍后再试。';
  if (status === 503 && detail.includes('额度')) return data?.detail || '今日服务额度已用完，请稍后再试。';
  return `请求失败（HTTP ${status}），请稍后重试。`;
}

async function request(url, options, timeoutMs, timeoutMessage, track = true, networkMessage = '') {
  const controller = new AbortController();
  if (track) activeController = controller;
  const timer = setTimeout(() => controller.abort('timeout'), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const contentType = response.headers.get('content-type') || '';
    let data = {};
    if (contentType.includes('application/json')) {
      try { data = await response.json(); } catch (_error) { data = {}; }
    } else {
      await response.text();
    }
    if (!response.ok) throw new Error(safeHttpError(response.status, data));
    if (!contentType.includes('application/json')) throw new Error('服务返回格式异常，请稍后重试。');
    return data;
  } catch (error) {
    if (error.name === 'AbortError') {
      if (controller.signal.reason === 'new-chat') throw new Error('当前识别已取消。');
      throw new Error(timeoutMessage);
    }
    if (error instanceof TypeError) throw new Error(networkMessage || '无法连接本地服务，请确认 Demo 正在运行后重试。');
    throw error;
  } finally {
    clearTimeout(timer);
    if (activeController === controller) activeController = null;
  }
}

async function requestStream(url, options, timeoutMs, timeoutMessage, onProgress, networkMessage = '') {
  const controller = new AbortController();
  activeController = controller;
  const timer = setTimeout(() => controller.abort('timeout'), timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    if (!response.ok) {
      let data = {};
      try { data = await response.json(); } catch (_error) { data = {}; }
      throw new Error(safeHttpError(response.status, data));
    }
    if (!response.body) throw new Error('服务返回格式异常，请稍后重试。');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let result = null;
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === 'progress') onProgress?.(event);
        if (event.type === 'result') result = event.data;
        if (event.type === 'error') throw new Error(event.message || '服务端处理失败，请稍后重试。');
      }
      if (done) break;
    }
    if (!result) throw new Error('服务返回格式异常，请稍后重试。');
    return result;
  } catch (error) {
    if (error.name === 'AbortError') {
      if (controller.signal.reason === 'new-chat') throw new Error('当前识别已取消。');
      throw new Error(timeoutMessage);
    }
    if (error instanceof TypeError) throw new Error(networkMessage || '无法连接本地服务，请确认 Demo 正在运行后重试。');
    throw error;
  } finally {
    clearTimeout(timer);
    if (activeController === controller) activeController = null;
  }
}

function responseItem(data) {
  updateSessionContext(data);
  return {
    message: data.text || '处理完成。',
    me: false,
    images: data.images || [],
    imageAlt: data.intent === 'select_candidate' || data.intent === 'resend_answer' ? '题库答案' : '相似题候选',
    intent: data.intent || '',
    taskRevision: Number(data.session?.task_revision || 0),
    candidateGeneration: String(data.session?.candidate_generation || ''),
    messageId: createMessageId(),
    createdAt: Date.now(),
  };
}

function updateSessionContext(data) {
  if (!data?.session) return;
  sessionContext = { ...sessionContext, ...data.session };
}

function invalidateCandidateActions() {
  sessionContext = { ...sessionContext, session_valid: false, phase: 'PROCESSING', candidate_generation: '', candidate_count: 0 };
  document.querySelectorAll('.select-candidate').forEach((button) => {
    button.disabled = true;
    button.textContent = '候选已失效';
  });
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
  try {
    const data = await requestStream('/api/message/stream', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text: clean, ...(actionContext ? { action_context: actionContext } : {}) }),
    }, TEXT_TIMEOUT_MS, '请求等待时间过长，请稍后重试。', (event) => {
      if (operation !== operationVersion) return;
      if (!pending) pending = addMessage({ message: event.message, variant: 'pending' }, false);
      else updatePendingMessage(pending, event.message);
      setStatus('working', event.message);
    });
    if (operation !== operationVersion) return;
    pending?.remove();
    addMessage(responseItem(data));
    setStatus('ready', '准备就绪');
  } catch (error) {
    if (operation !== operationVersion) return;
    pending?.remove();
    if (error.message !== '当前识别已取消。') addMessage({ message: error.message || '暂时无法处理，请再试一次。', variant: 'error' });
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
  return addMessage({
    message: '我发了一张题图。',
    me: true,
    images: [preview],
    imageAlt: '待上传的题图',
  }, false);
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

function addUploadFailure(row, message, prepared) {
  setUploadRowStatus(row, `${message} 裁剪后的图片已保留，可直接重新上传。`, 'error');
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.className = 'retry-upload';
  retry.textContent = '重新上传';
  retry.addEventListener('click', () => retryUpload(row, prepared));
  row.querySelector('.message-content')?.append(retry);
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
    const data = await requestStream('/api/image/stream', {
      method: 'POST', body: formData,
    }, IMAGE_TIMEOUT_MS, '网络上传或题图识别超时，请直接重新上传。', (event) => {
      if (operation !== operationVersion) return;
      updatePendingMessage(pending, event.message);
      setStatus('working', event.message);
    }, '网络上传失败，请检查网络后重试。');
    if (operation !== operationVersion) return;
    if (!isPersistentImage(data.uploaded_image)) throw new Error('服务端处理失败，未返回已上传的题图。');
    pending.remove();
    setUploadRowPreview(uploadRow, data.uploaded_image);
    setUploadRowStatus(uploadRow, '我发了一张题图。');
    remember({ message: '我发了一张题图。', me: true, images: [data.uploaded_image], imageAlt: '已上传题图' });
    releaseObjectUrl(prepared.preview);
    clearPendingUpload({ releasePreview: false });
    addMessage(responseItem(data));
    setStatus('ready', '准备就绪');
  } catch (error) {
    if (operation !== operationVersion) return;
    pending.remove();
    addUploadFailure(uploadRow, error.message || '服务端处理失败，请稍后重试。', prepared);
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
  const validationError = validateImage(selected);
  fileInput.value = '';
  if (validationError) {
    addMessage({ message: validationError, variant: 'error' });
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
    chat.replaceChildren();
    empty.hidden = false;
    setStatus('ready', '已开始新对话');
  } catch (error) {
    if (operation !== operationVersion) return;
    addMessage({ message: error.message || '新对话创建失败，请稍后重试。', variant: 'error' });
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
    if (!isBusy) setStatus('ready', '准备就绪');
  } catch (_error) {
    setStatus('error', '本地服务未连接');
  }
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
feedbackClose.addEventListener('click', closeFeedback);
feedbackBackdrop.addEventListener('click', (event) => { if (event.target === feedbackBackdrop) closeFeedback(); });
feedbackCancel.addEventListener('click', cancelFeedback);
feedbackSubmit.addEventListener('click', submitFeedback);
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  if (!feedbackBackdrop.hidden) closeFeedback();
  else if (!lightbox.hidden) closeLightbox();
  else closeDrawer();
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
    addMessage({ message: '当前一次处理一张题图，请先上传其中一张。', variant: 'error' });
    return;
  }
  if (!isBusy) uploadImage(files[0]);
});
window.addEventListener('blur', hideDropOverlay);
window.addEventListener('pagehide', releaseAllObjectUrls);
window.addEventListener('focus', expireHistoryIfNeeded);
window.addEventListener('offline', () => setStatus('error', '当前网络已断开'));
window.addEventListener('online', checkHealth);
window.addEventListener('resize', syncVisualViewport, { passive: true });
window.addEventListener('orientationchange', syncVisualViewport, { passive: true });
window.visualViewport?.addEventListener('resize', syncVisualViewport, { passive: true });
window.visualViewport?.addEventListener('scroll', syncVisualViewport, { passive: true });
document.addEventListener('visibilitychange', () => { if (!document.hidden) expireHistoryIfNeeded(); });

syncVisualViewport();
restoreHistory();
repairUploadedImageHistory();
resizeComposer();
updateComposer();
checkHealth();
