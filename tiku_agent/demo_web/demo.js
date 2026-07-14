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
const ALLOWED_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp']);

let history = [];
let isBusy = false;
let dragDepth = 0;
let activeController = null;
let focusBeforeModal = null;
let operationVersion = 0;
let pendingUpload = null;
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
    choose.addEventListener('click', () => sendTextValue(`选择候选 ${index + 1}`));
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
  clearPendingUpload();
  releaseAllObjectUrls();
  localStorage.removeItem(HISTORY_KEY);
  localStorage.removeItem(LEGACY_HISTORY_KEY);
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
  let pending = null;
  setBusy(true);
  setStatus('working', '正在处理…');
  try {
    const data = await requestStream('/api/message/stream', {
      method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ text: clean }),
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
