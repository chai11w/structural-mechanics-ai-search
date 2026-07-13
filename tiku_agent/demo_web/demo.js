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

const TEXT_TIMEOUT_MS = 60000;
const IMAGE_TIMEOUT_MS = 90000;
const MAX_IMAGE_BYTES = 15 * 1024 * 1024;
const HISTORY_TTL_MS = 2 * 60 * 60 * 1000;
const HISTORY_LIMIT = 50;
const HISTORY_KEY = 'tiku-agent-current-chat-v2';
const LEGACY_HISTORY_KEY = 'tiku-agent-current-chat-v1';
const ALLOWED_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/bmp']);

let history = [];
let isBusy = false;
let dragDepth = 0;
let activeController = null;
let focusBeforeModal = null;
let operationVersion = 0;
const objectUrls = new Set();

function isPersistentImage(url) {
  return typeof url === 'string' && (url.startsWith('/api/media/') || url.startsWith('/api/upload/'));
}

function saveHistory() {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify({ savedAt: Date.now(), messages: history.slice(-HISTORY_LIMIT) }));
  } catch (_error) {
    // The demo remains usable when browser storage is unavailable.
  }
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

function remember(item) {
  history.push({
    message: String(item.message || ''),
    me: Boolean(item.me),
    images: (item.images || []).filter(isPersistentImage),
    imageAlt: String(item.imageAlt || '题库图片'),
    intent: String(item.intent || ''),
    variant: String(item.variant || ''),
  });
  history = history.slice(-HISTORY_LIMIT);
  saveHistory();
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
    choose.textContent = '选择这道题';
    choose.addEventListener('click', () => sendTextValue(String(index + 1), `选择候选 ${index + 1}`));
    footer.append(label, choose);
    card.append(footer);
  }
  return card;
}

function addMessage(item, persist = true) {
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
    const savedAt = Number(stored?.savedAt);
    const now = Date.now();
    if (!stored || !Array.isArray(stored.messages) || !Number.isFinite(savedAt) || savedAt > now + 60000 || now - savedAt > HISTORY_TTL_MS) {
      clearHistory();
      return;
    }
    history = stored.messages.slice(-HISTORY_LIMIT);
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
    // An expired server session should not make the restored text unusable.
  }
}

function clearHistory() {
  history = [];
  releaseAllObjectUrls();
  localStorage.removeItem(HISTORY_KEY);
  localStorage.removeItem(LEGACY_HISTORY_KEY);
}

function replacePending(row, item) {
  if (row?.isConnected) row.remove();
  addMessage(item);
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

function setBusy(value) {
  isBusy = value;
  textInput.disabled = value;
  fileInput.disabled = value;
  form.setAttribute('aria-busy', String(value));
  updateComposer();
}

function validateImage(file) {
  if (!file) return '没有读取到图片，请重新选择。';
  const extension = file.name.split('.').pop()?.toLowerCase();
  const allowedExtension = ['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp'].includes(extension);
  if (!ALLOWED_TYPES.has(file.type) || !allowedExtension) return '请上传 PNG、JPG、WEBP、GIF 或 BMP 图片。';
  if (file.size > MAX_IMAGE_BYTES) return '图片太大，请上传不超过 15MB 的图片。';
  return '';
}

function safeHttpError(status, data) {
  const detail = typeof data?.detail === 'string' ? data.detail.toLowerCase() : '';
  if (status === 413 || detail.includes('too large')) return '图片太大，请上传不超过 15MB 的图片。';
  if (status === 400 && detail.includes('invalid image')) return '这个文件不是可读取的图片。';
  if (status >= 500) return '服务暂时异常，请稍后重试。';
  if (status === 400) return '提交的内容无法处理，请检查后重试。';
  if (status === 401 || status === 403) return '当前请求无权处理。';
  return `请求失败（HTTP ${status}），请稍后重试。`;
}

async function request(url, options, timeoutMs, timeoutMessage, track = true) {
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
    if (error instanceof TypeError) throw new Error('无法连接本地服务，请确认 Demo 正在运行后重试。');
    throw error;
  } finally {
    clearTimeout(timer);
    if (activeController === controller) activeController = null;
  }
}

function responseItem(data) {
  return {
    message: data.text || '处理完成。',
    me: false,
    images: data.images || [],
    imageAlt: data.intent === 'select_candidate' || data.intent === 'resend_answer' ? '题库答案' : '相似题候选',
    intent: data.intent || '',
  };
}

async function sendTextValue(value, displayValue = value) {
  const clean = String(value || '').trim();
  if (!clean || isBusy) return;
  addMessage({ message: displayValue, me: true });
  textInput.value = '';
  resizeComposer();
  const operation = ++operationVersion;
  setBusy(true);
  setStatus('working', '正在回答…');
  try {
    const data = await request('/api/message', {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ text: clean }),
    }, TEXT_TIMEOUT_MS, '请求等待时间过长，请稍后重试。');
    if (operation !== operationVersion) return;
    addMessage(responseItem(data));
    setStatus('ready', '准备就绪');
  } catch (error) {
    if (operation !== operationVersion) return;
    if (error.message !== '当前识别已取消。') addMessage({ message: error.message || '暂时无法处理，请再试一次。', variant: 'error' });
    setStatus('error', '处理失败，可重新尝试');
  } finally {
    if (operation !== operationVersion) return;
    setBusy(false);
    textInput.focus();
  }
}

async function sendText() {
  await sendTextValue(textInput.value);
}

async function uploadImage(selected) {
  if (isBusy) return;
  const validationError = validateImage(selected);
  if (validationError) {
    addMessage({ message: validationError, variant: 'error' });
    fileInput.value = '';
    return;
  }
  const preview = URL.createObjectURL(selected);
  objectUrls.add(preview);
  const historyIndex = history.length;
  const uploadRow = addMessage({ message: '我发了一张题图。', me: true, images: [preview], imageAlt: '已上传题图' });
  const pending = addMessage({ message: '正在识别题干并检索相似题', variant: 'pending' }, false);
  fileInput.value = '';
  const operation = ++operationVersion;
  setBusy(true);
  setStatus('working', '正在识别题图…');
  try {
    const data = await request('/api/image', {
      method: 'POST', headers: { 'x-filename': selected.name, 'content-type': selected.type }, body: selected,
    }, IMAGE_TIMEOUT_MS, '题图识别时间过长。原图已保留，你可以直接回复“重试”。');
    if (operation !== operationVersion) return;
    if (isPersistentImage(data.uploaded_image) && history[historyIndex]) {
      history[historyIndex].images = [data.uploaded_image];
      const previewImage = uploadRow.querySelector('img');
      if (previewImage) previewImage.src = data.uploaded_image;
      releaseObjectUrl(preview);
      saveHistory();
    }
    replacePending(pending, responseItem(data));
    setStatus('ready', '准备就绪');
  } catch (error) {
    if (operation !== operationVersion) return;
    replacePending(pending, { message: error.message || '图片暂时无法处理，请再试一次。', variant: 'error' });
    setStatus('error', '识别失败，可重新上传');
  } finally {
    if (operation !== operationVersion) return;
    setBusy(false);
    textInput.focus();
  }
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
    textInput.focus();
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
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  if (!lightbox.hidden) closeLightbox(); else closeDrawer();
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
window.addEventListener('offline', () => setStatus('error', '当前网络已断开'));
window.addEventListener('online', checkHealth);

restoreHistory();
repairUploadedImageHistory();
resizeComposer();
updateComposer();
checkHealth();
