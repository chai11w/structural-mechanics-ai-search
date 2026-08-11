const app = document.querySelector('#app');
let csrfToken = '';

const ICONS = {
  overview: 'layout-dashboard', invitations: 'key-round', feedback: 'message-square',
  settings: 'settings', logout: 'log-out', add: 'plus', view: 'eye', edit: 'pencil',
  reset: 'refresh-cw', pause: 'circle-pause', enable: 'circle-play', archive: 'archive',
  copy: 'copy', close: 'x', menu: 'menu', back: 'chevron-left', next: 'chevron-right',
  up: 'thumbs-up', down: 'thumbs-down', image: 'image', save: 'save', alert: 'alert-circle',
};

const TAG_LABELS = {
  found_answer: '找到了答案', relevant_results: '结果相关', clear_reply: '回复清楚', fast: '速度快',
  not_found: '没有找到', irrelevant_results: '结果不相关', ranking_issue: '排序问题',
  wrong_answer: '答案错误', too_slow: '速度慢', system_error: '系统错误', other: '其他',
};

const AUDIT_LABELS = {
  'admin.initialize': '初始化管理员', 'admin.password_change': '修改管理员密码',
  'invitation.create': '新增邀请码', 'invitation.update': '修改邀请码',
  'invitation.enabled': '启用邀请码', 'invitation.disabled': '停用邀请码',
  'invitation.archived': '归档邀请码', 'invitation.reset': '重置邀请码',
  'invitation.import': '导入邀请码', 'settings.update': '修改全局设置',
};

function icon(name, className = '') {
  const id = ICONS[name] || name;
  return `<svg class="icon ${className}" aria-hidden="true"><use href="/assets/lucide.svg#${id}"></use></svg>`;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[char]);
}

function attr(value) { return escapeHtml(value); }

function formatDateTime(value) {
  if (!value) return '尚无记录';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '尚无记录';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date).replaceAll('/', '-');
}

function formatFullDate(value) {
  if (!value) return '长期有效';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '长期有效';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date);
}

function toLocalInput(value) {
  if (!value) return '';
  const date = new Date(value);
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function statusText(status) {
  return ({ enabled: '启用', disabled: '停用', archived: '已归档', pending: '待处理', resolved: '已处理', no_action: '无需处理' })[status] || status || '未知';
}

function ratingText(rating) { return rating === 'positive' ? '点赞' : '点踩'; }

function statusBadge(status, label = '') {
  return `<span class="status ${attr(status)}">${escapeHtml(label || statusText(status))}</span>`;
}

async function api(url, options = {}, { allow401 = false } = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has('content-type')) headers.set('content-type', 'application/json');
  if (!['GET', 'HEAD'].includes(String(options.method || 'GET').toUpperCase()) && csrfToken) {
    headers.set('x-csrf-token', csrfToken);
  }
  const response = await fetch(url, { ...options, headers, credentials: 'same-origin' });
  let data = {};
  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('application/json')) {
    try { data = await response.json(); } catch (_error) { data = {}; }
  }
  if (!response.ok) {
    if (response.status === 401 && !allow401 && !location.pathname.startsWith('/login')) {
      location.assign('/login');
    }
    throw new Error(data.detail || `请求失败（${response.status}）`);
  }
  return data;
}

function showToast(message, type = 'success') {
  let region = document.querySelector('.toast-region');
  if (!region) {
    region = document.createElement('div');
    region.className = 'toast-region';
    region.setAttribute('aria-live', 'polite');
    document.body.append(region);
  }
  const toast = document.createElement('div');
  toast.className = `toast ${type === 'error' ? 'error' : ''}`;
  toast.innerHTML = `${icon(type === 'error' ? 'alert' : 'check')}<span>${escapeHtml(message)}</span>`;
  region.append(toast);
  setTimeout(() => toast.remove(), 3600);
}

function authPreview() {
  return `<aside class="auth-aside" aria-hidden="true"><div class="auth-preview"><div class="preview-bar"><i></i><i></i><i></i></div><div class="preview-body"><div class="preview-nav"><span></span><span></span><span></span><span></span></div><div class="preview-main"><div class="preview-title"></div><div class="preview-metrics"><i></i><i></i><i></i><i></i></div><div class="preview-table"><i></i><i></i><i></i><i></i></div></div></div></div></aside>`;
}

function renderAuth(mode) {
  const setup = mode === 'setup';
  app.innerHTML = `<main class="auth-layout"><section class="auth-panel"><a class="auth-brand" href="/"><span class="brand-mark">力</span><span class="brand-copy"><strong>力答</strong><span>管理后台</span></span></a><div class="auth-content"><h1>${setup ? '初始化管理员' : '管理员登录'}</h1><p>${setup ? '首次使用请设置独立管理员密码。完成后将进入后台概览。' : '登录后管理邀请码、费用额度和用户反馈。'}</p><form class="form-stack" id="auth-form"><div class="field"><label for="password">${setup ? '管理员密码' : '密码'}</label><input class="input" id="password" name="password" type="password" autocomplete="${setup ? 'new-password' : 'current-password'}" minlength="12" maxlength="256" required autofocus>${setup ? '<span class="field-hint">至少 12 个字符，不要与邀请码或其他账户共用。</span>' : ''}</div>${setup ? '<div class="field"><label for="confirm-password">确认密码</label><input class="input" id="confirm-password" name="confirm_password" type="password" autocomplete="new-password" minlength="12" maxlength="256" required></div>' : ''}<p class="form-error" id="auth-error" role="alert"></p><button class="button button-primary button-full" type="submit">${setup ? '完成初始化' : '登录后台'}</button></form></div><p class="auth-foot">管理员入口与用户邀请码登录完全隔离。公网部署时还应启用 Cloudflare Access。</p></section>${authPreview()}</main>`;
  const form = document.querySelector('#auth-form');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const error = document.querySelector('#auth-error');
    button.disabled = true;
    error.textContent = '';
    const payload = { password: form.password.value };
    if (setup) payload.confirm_password = form.confirm_password.value;
    try {
      await api(`/api/admin/${setup ? 'setup' : 'login'}`, { method: 'POST', body: JSON.stringify(payload) }, { allow401: true });
      location.assign('/overview');
    } catch (err) {
      error.textContent = err.message;
      button.disabled = false;
    }
  });
}

function navLink(key, label, active, badge = '') {
  return `<a class="nav-link" href="/${key}" ${key === active ? 'aria-current="page"' : ''}>${icon(key)}<span>${label}</span>${badge ? `<span class="nav-badge">${escapeHtml(badge)}</span>` : ''}</a>`;
}

function mountShell(active, title, description, actions = '') {
  app.innerHTML = `<div class="shell"><aside class="sidebar" id="sidebar"><a class="brand" href="/overview"><span class="brand-mark">力</span><span class="brand-copy"><strong>力答</strong><span>管理后台</span></span></a><nav class="nav" aria-label="后台导航">${navLink('overview', '概览', active)}${navLink('invitations', '邀请码', active)}${navLink('feedback', '反馈', active)}${navLink('settings', '设置', active)}</nav><div class="sidebar-foot"><button class="button button-quiet logout-button" id="logout" type="button">${icon('logout')}<span>退出登录</span></button></div></aside><div class="main-shell"><header class="mobile-topbar"><button class="icon-button" id="open-sidebar" type="button" aria-label="打开导航">${icon('menu')}</button><a class="brand" href="/overview"><span class="brand-mark">力</span><span class="brand-copy"><strong>力答后台</strong></span></a><span class="mobile-spacer"></span></header><main class="page"><header class="page-head"><div class="page-title"><h1>${escapeHtml(title)}</h1><p>${escapeHtml(description)}</p></div><div class="page-actions">${actions}</div></header><div id="page-body"><div class="loading-screen page-loading"><span>正在加载…</span></div></div></main></div></div>`;
  document.querySelector('#logout').addEventListener('click', async () => {
    try { await api('/api/admin/logout', { method: 'POST' }); } finally { location.assign('/login'); }
  });
  const sidebar = document.querySelector('#sidebar');
  document.querySelector('#open-sidebar')?.addEventListener('click', () => {
    sidebar.classList.add('is-open');
    const backdrop = document.createElement('button');
    backdrop.className = 'sidebar-backdrop';
    backdrop.setAttribute('aria-label', '关闭导航');
    backdrop.addEventListener('click', () => { sidebar.classList.remove('is-open'); backdrop.remove(); });
    document.body.append(backdrop);
  });
  return document.querySelector('#page-body');
}

function progress(cost, budget) {
  const percent = budget > 0 ? Math.min(100, Math.round(cost / budget * 100)) : 0;
  const state = percent >= 100 ? 'is-full' : percent >= 80 ? 'is-high' : '';
  return `<progress class="usage-progress ${state}" title="已使用 ${percent}%" max="100" value="${percent}">${percent}%</progress>`;
}

function inviteUsageRows(items, limit = items.length) {
  const rows = items.slice(0, limit).map((item) => `<tr><td><span class="cell-main">${escapeHtml(item.label)}</span><span class="cell-sub">${escapeHtml(item.invite_id)}</span></td><td>${statusBadge(item.status)}</td><td class="numeric">${Number(item.today_searches || 0)}</td><td class="numeric"><span class="cell-main">¥${escapeHtml(item.today_cost_cny)}</span>${progress(item.today_cost_micros, item.effective_budget_micros)}</td><td class="numeric">¥${escapeHtml(item.remaining_cny)}</td><td><span class="cell-main">${formatDateTime(item.last_activity_at || item.last_used_at)}</span></td></tr>`).join('');
  return rows || '<tr><td class="empty-row" colspan="6">还没有邀请码使用记录</td></tr>';
}

function feedbackSummaryRows(items) {
  const rows = items.map((item) => `<tr><td><div class="feedback-topic"><span class="feedback-preview">${item.preview_image ? `<img src="${attr(item.preview_image)}" alt="题目缩略图">` : icon('image')}</span><span class="feedback-copy"><span class="cell-main">${escapeHtml(item.invite_label || item.identity_key)}</span><span class="cell-sub">${formatDateTime(item.created_at)}</span></span></div></td><td>${statusBadge(item.rating, ratingText(item.rating))}</td><td><div class="tag-list">${item.tags.map((tag) => `<span class="tag">${escapeHtml(TAG_LABELS[tag] || tag)}</span>`).join('') || '<span class="cell-sub">未选择原因</span>'}</div></td><td><div class="feedback-detail-text">${escapeHtml(item.detail || item.preview_text || '未填写补充说明')}</div></td><td class="numeric">¥${escapeHtml(item.cost?.estimated_cost_cny || '0.00')}</td><td>${statusBadge(item.review_status)}</td><td class="numeric"><a class="button button-secondary" href="/feedback/${attr(item.feedback_id)}" data-feedback-link="${attr(item.feedback_id)}">${icon('view')}查看详情</a></td></tr>`).join('');
  return rows || '<tr><td class="empty-row" colspan="7">暂无反馈</td></tr>';
}

async function renderOverview() {
  const body = mountShell('overview', '今日概览', '了解今天的搜题量、估算费用和待处理反馈。');
  try {
    const data = await api('/api/admin/overview');
    body.innerHTML = `<section class="metrics" aria-label="今日运营指标"><div class="metric"><span class="metric-label">${icon('activity')}今日搜题</span><strong class="metric-value">${Number(data.today_searches)}</strong><span class="metric-note">按独立题目计算</span></div><div class="metric"><span class="metric-label">${icon('wallet')}估算总费用</span><strong class="metric-value">¥${escapeHtml(data.today_cost_cny)}</strong><span class="metric-note">全站剩余 ¥${escapeHtml(data.global_remaining_cny)}</span></div><div class="metric"><span class="metric-label">${icon('users')}启用邀请码</span><strong class="metric-value">${Number(data.active_invites)}</strong><span class="metric-note">当前可登录使用</span></div><div class="metric"><span class="metric-label">${icon('down')}待处理点踩</span><strong class="metric-value">${Number(data.pending_negative_feedback)}</strong><span class="metric-note">需要复盘的反馈</span></div></section><section class="section"><div class="section-head"><div class="section-title"><h2>邀请码今日使用</h2><p>额度是模型费用估算，不等同供应商账单实扣。</p></div><a class="text-link" href="/invitations">管理邀请码 ${icon('next')}</a></div><div class="table-tool"><div class="table-scroll"><table class="data-table"><thead><tr><th>邀请码</th><th>状态</th><th class="numeric">搜题数</th><th class="numeric">今日费用</th><th class="numeric">剩余额度</th><th>最后活动</th></tr></thead><tbody>${inviteUsageRows(data.invites, 10)}</tbody></table></div></div></section><section class="section"><div class="section-head"><div class="section-title"><h2>最近反馈</h2><p>点开详情可查看截至反馈时的完整对话。</p></div><a class="text-link" href="/feedback">查看全部 ${icon('next')}</a></div><div class="table-tool"><div class="table-scroll"><table class="data-table"><thead><tr><th>反馈来源</th><th>评价</th><th>原因</th><th>说明</th><th class="numeric">本题费用</th><th>状态</th><th></th></tr></thead><tbody>${feedbackSummaryRows(data.recent_feedback)}</tbody></table></div></div></section>`;
    bindFeedbackLinks();
  } catch (error) { body.innerHTML = errorState(error.message); }
}

function errorState(message) {
  return `<div class="case-expired">${icon('alert')}<br>${escapeHtml(message)}</div>`;
}

function modal(content) {
  closeModal();
  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.id = 'modal-backdrop';
  backdrop.innerHTML = content;
  backdrop.addEventListener('mousedown', (event) => { if (event.target === backdrop) closeModal(); });
  document.body.append(backdrop);
  document.body.dataset.modal = 'open';
  backdrop.querySelectorAll('[data-close-modal]').forEach((button) => button.addEventListener('click', closeModal));
  return backdrop;
}

function closeModal() {
  document.querySelector('#modal-backdrop')?.remove();
  delete document.body.dataset.modal;
}

function confirmAction(title, message, confirmLabel = '确认') {
  return new Promise((resolve) => {
    const root = modal(`<section class="modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title"><header class="modal-head"><div><h2 id="confirm-title">${escapeHtml(title)}</h2><p>此操作会立即生效</p></div><button class="icon-button" type="button" data-close-modal aria-label="关闭">${icon('close')}</button></header><div class="modal-body"><p class="confirm-copy">${escapeHtml(message)}</p></div><footer class="modal-actions"><button class="button button-secondary" type="button" data-close-modal>取消</button><button class="button button-danger" id="confirm-action" type="button">${escapeHtml(confirmLabel)}</button></footer></section>`);
    root.querySelector('#confirm-action').addEventListener('click', () => { closeModal(); resolve(true); });
    root.querySelectorAll('[data-close-modal]').forEach((button) => button.addEventListener('click', () => resolve(false), { once: true }));
  });
}

function showCode(code, title = '邀请码已创建') {
  const root = modal(`<section class="modal" role="dialog" aria-modal="true" aria-labelledby="code-title"><header class="modal-head"><div><h2 id="code-title">${escapeHtml(title)}</h2><p>明文只显示这一次</p></div><button class="icon-button" type="button" data-close-modal aria-label="关闭">${icon('close')}</button></header><div class="modal-body"><div class="code-reveal"><span class="code-reveal-label">请立即复制并安全发放</span><div class="code-row"><code class="code-value" id="invite-code">${escapeHtml(code)}</code><button class="icon-button" id="copy-code" type="button" title="复制邀请码" aria-label="复制邀请码">${icon('copy')}</button></div><p class="code-warning">关闭后后台无法再次查看。需要新明文时只能重置邀请码，旧登录状态也会同时失效。</p></div></div><footer class="modal-actions"><button class="button button-primary" type="button" data-close-modal>我已保存</button></footer></section>`);
  root.querySelector('#copy-code').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(code); showToast('邀请码已复制'); }
    catch (_error) { window.getSelection()?.selectAllChildren(root.querySelector('#invite-code')); showToast('请手动复制选中的邀请码'); }
  });
}

function invitationForm(item = null) {
  const edit = Boolean(item);
  const root = modal(`<section class="modal" role="dialog" aria-modal="true" aria-labelledby="invite-form-title"><header class="modal-head"><div><h2 id="invite-form-title">${edit ? '编辑邀请码' : '新增邀请码'}</h2><p>${edit ? '修改备注、额度或有效期' : '创建后明文仅显示一次'}</p></div><button class="icon-button" type="button" data-close-modal aria-label="关闭">${icon('close')}</button></header><form id="invite-form"><div class="modal-body form-stack"><div class="field"><label for="invite-label">备注名称</label><input class="input" id="invite-label" name="label" maxlength="80" required value="${attr(item?.label || '')}" placeholder="例如：张三内测"></div><div class="field"><label for="invite-budget">每日估算费用上限</label><input class="input" id="invite-budget" name="daily_budget_cny" type="number" min="0.01" max="10000" step="0.01" value="${item?.daily_budget_micros == null ? '' : attr((item.daily_budget_micros / 1000000).toFixed(2))}" placeholder="留空则继承默认额度"><span class="field-hint">留空后跟随设置页的默认单码额度。</span></div><div class="field"><label for="invite-expiry">有效期</label><input class="input" id="invite-expiry" name="expires_at" type="datetime-local" value="${attr(toLocalInput(item?.expires_at || ''))}"><span class="field-hint">留空表示长期有效。</span></div><p class="form-error" id="invite-error" role="alert"></p></div><footer class="modal-actions"><button class="button button-secondary" type="button" data-close-modal>取消</button><button class="button button-primary" type="submit">${edit ? '保存修改' : '创建邀请码'}</button></footer></form></section>`);
  const form = root.querySelector('#invite-form');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const submit = form.querySelector('[type="submit"]');
    const error = form.querySelector('#invite-error');
    const expires = form.expires_at.value;
    const payload = {
      label: form.label.value.trim(),
      daily_budget_cny: form.daily_budget_cny.value || null,
      expires_at: expires ? new Date(expires).toISOString() : '',
    };
    submit.disabled = true;
    error.textContent = '';
    try {
      const data = await api(edit ? `/api/admin/invitations/${encodeURIComponent(item.invite_id)}` : '/api/admin/invitations', {
        method: edit ? 'PATCH' : 'POST', body: JSON.stringify(payload),
      });
      closeModal();
      if (data.code) showCode(data.code);
      else showToast('邀请码设置已保存');
      await renderInvitations();
    } catch (err) { error.textContent = err.message; submit.disabled = false; }
  });
}

async function invitationAction(item, action) {
  if (action === 'edit') return invitationForm(item);
  if (action === 'reset') {
    if (!await confirmAction('重置邀请码', '旧邀请码和已经签发的登录状态都会立即失效。', '确认重置')) return;
    try { const data = await api(`/api/admin/invitations/${encodeURIComponent(item.invite_id)}/reset`, { method: 'POST' }); showCode(data.code, '邀请码已重置'); await renderInvitations(); } catch (error) { showToast(error.message, 'error'); }
    return;
  }
  const nextStatus = action === 'enable' ? 'enabled' : action === 'archive' ? 'archived' : 'disabled';
  const actionText = nextStatus === 'archived' ? '归档' : statusText(nextStatus);
  const message = nextStatus === 'enabled' ? '启用后该邀请码可以重新登录；此前登录状态仍保持失效。' : nextStatus === 'archived' ? '归档后不再出现在默认列表中，历史费用和反馈会继续保留。' : '停用后该邀请码与现有登录状态会立即失效。';
  if (!await confirmAction(`${actionText}邀请码`, message, `确认${actionText}`)) return;
  try { await api(`/api/admin/invitations/${encodeURIComponent(item.invite_id)}/status`, { method: 'POST', body: JSON.stringify({ status: nextStatus }) }); showToast(nextStatus === 'archived' ? '邀请码已归档' : `邀请码已${statusText(nextStatus)}`); await renderInvitations(); } catch (error) { showToast(error.message, 'error'); }
}

function invitationRows(items, query = '') {
  const clean = query.trim().toLowerCase();
  const visible = clean ? items.filter((item) => `${item.label} ${item.invite_id}`.toLowerCase().includes(clean)) : items;
  return visible.map((item) => `<tr><td><span class="cell-main">${escapeHtml(item.label)}</span><span class="cell-sub">${escapeHtml(item.invite_id)}</span></td><td>${statusBadge(item.status)}</td><td><span class="cell-main">¥${escapeHtml(item.effective_budget_cny || '0.00')} / 天</span><span class="cell-sub">${item.daily_budget_micros == null ? '继承默认额度' : '独立额度'}</span></td><td class="numeric"><span class="cell-main">${Number(item.today_searches || 0)} 次 · ¥${escapeHtml(item.today_cost_cny || '0.00')}</span>${progress(item.today_cost_micros || 0, item.effective_budget_micros || 0)}</td><td><span class="cell-main">${formatFullDate(item.expires_at)}</span><span class="cell-sub">最近登录 ${formatDateTime(item.last_used_at)}</span></td><td><div class="cell-actions"><button class="icon-button" data-action="edit" data-id="${attr(item.invite_id)}" type="button" title="编辑" aria-label="编辑邀请码">${icon('edit')}</button>${item.status === 'enabled' ? `<button class="icon-button" data-action="disable" data-id="${attr(item.invite_id)}" type="button" title="停用" aria-label="停用邀请码">${icon('pause')}</button>` : item.status === 'disabled' ? `<button class="icon-button" data-action="enable" data-id="${attr(item.invite_id)}" type="button" title="启用" aria-label="启用邀请码">${icon('enable')}</button>` : ''}${item.status !== 'archived' ? `<button class="icon-button" data-action="reset" data-id="${attr(item.invite_id)}" type="button" title="重置" aria-label="重置邀请码">${icon('reset')}</button><button class="icon-button danger" data-action="archive" data-id="${attr(item.invite_id)}" type="button" title="归档" aria-label="归档邀请码">${icon('archive')}</button>` : ''}</div></td></tr>`).join('') || '<tr><td class="empty-row" colspan="6">没有符合条件的邀请码</td></tr>';
}

async function renderInvitations() {
  const includeArchived = new URLSearchParams(location.search).get('archived') === '1';
  const body = mountShell('invitations', '邀请码管理', '创建、停用、重置和调整每个邀请码的每日额度。', `<button class="button button-primary" id="create-invite" type="button">${icon('add')}新增邀请码</button>`);
  document.querySelector('#create-invite').addEventListener('click', () => invitationForm());
  try {
    const data = await api(`/api/admin/invitations?include_archived=${includeArchived ? 'true' : 'false'}`);
    const items = data.items;
    body.innerHTML = `<div class="toolbar"><div class="field search-field"><label class="field-label" for="invite-search">搜索邀请码</label><div class="search-wrap">${icon('search')}<input class="input" id="invite-search" type="search" placeholder="按备注或 ID 搜索"></div></div><div class="toolbar-spacer"></div><label class="toggle-row"><span class="switch"><input id="show-archived" type="checkbox" ${includeArchived ? 'checked' : ''}><span></span></span>显示已归档</label></div><div class="table-tool"><div class="table-scroll"><table class="data-table"><thead><tr><th>邀请码</th><th>状态</th><th>每日额度</th><th class="numeric">今日使用</th><th>有效期</th><th></th></tr></thead><tbody id="invite-rows">${invitationRows(items)}</tbody></table></div></div>`;
    const bindRows = () => document.querySelectorAll('[data-action][data-id]').forEach((button) => button.addEventListener('click', () => invitationAction(items.find((item) => item.invite_id === button.dataset.id), button.dataset.action)));
    bindRows();
    document.querySelector('#invite-search').addEventListener('input', (event) => { document.querySelector('#invite-rows').innerHTML = invitationRows(items, event.target.value); bindRows(); });
    document.querySelector('#show-archived').addEventListener('change', (event) => { location.search = event.target.checked ? '?archived=1' : ''; });
  } catch (error) { body.innerHTML = errorState(error.message); }
}

function bindFeedbackLinks() {
  document.querySelectorAll('[data-feedback-link]').forEach((link) => link.addEventListener('click', () => {
    sessionStorage.setItem('tiku-admin-feedback-return', `${location.pathname}${location.search}`);
  }));
}

async function renderFeedback() {
  const params = new URLSearchParams(location.search);
  const filters = {
    rating: params.get('rating') || '', identity_key: params.get('identity_key') || '',
    chapter: params.get('chapter') || '', review_status: params.get('review_status') || '',
    tag: params.get('tag') || '', date: params.get('date') || '',
    offset: Math.max(0, Number(params.get('offset') || 0)), limit: 50,
  };
  const body = mountShell('feedback', '用户反馈', '筛选反馈并打开完整对话，判断问题是否需要改进。');
  try {
    const requestParams = new URLSearchParams({offset: String(filters.offset), limit: String(filters.limit)});
    for (const key of ['rating', 'identity_key', 'chapter', 'review_status', 'tag', 'date']) {
      if (filters[key]) requestParams.set(key, filters[key]);
    }
    const [data, invites] = await Promise.all([
      api(`/api/admin/feedback?${requestParams.toString()}`),
      api('/api/admin/invitations?include_archived=true'),
    ]);
    const inviteOptions = invites.items.map((item) => `<option value="${attr(item.invite_id)}" ${filters.identity_key === item.invite_id ? 'selected' : ''}>${escapeHtml(item.label)} · ${escapeHtml(item.invite_id)}</option>`).join('');
    const tagOptions = Object.entries(TAG_LABELS).map(([value, label]) => `<option value="${attr(value)}" ${filters.tag === value ? 'selected' : ''}>${escapeHtml(label)}</option>`).join('');
    body.innerHTML = `<form class="toolbar feedback-filters" id="feedback-filters"><div class="field"><label class="field-label" for="filter-date">日期</label><input class="input" id="filter-date" name="date" type="date" value="${attr(filters.date)}"></div><div class="field"><label class="field-label" for="filter-rating">评价</label><select class="select" id="filter-rating" name="rating"><option value="">全部</option><option value="negative" ${filters.rating === 'negative' ? 'selected' : ''}>点踩</option><option value="positive" ${filters.rating === 'positive' ? 'selected' : ''}>点赞</option></select></div><div class="field"><label class="field-label" for="filter-tag">反馈原因</label><select class="select" id="filter-tag" name="tag"><option value="">全部原因</option>${tagOptions}</select></div><div class="field"><label class="field-label" for="filter-invite">邀请码</label><select class="select" id="filter-invite" name="identity_key"><option value="">全部邀请码</option>${inviteOptions}</select></div><div class="field"><label class="field-label" for="filter-chapter">章节</label><input class="input" id="filter-chapter" name="chapter" value="${attr(filters.chapter)}" placeholder="全部章节"></div><div class="field"><label class="field-label" for="filter-status">处理状态</label><select class="select" id="filter-status" name="review_status"><option value="">全部</option><option value="pending" ${filters.review_status === 'pending' ? 'selected' : ''}>待处理</option><option value="resolved" ${filters.review_status === 'resolved' ? 'selected' : ''}>已处理</option><option value="no_action" ${filters.review_status === 'no_action' ? 'selected' : ''}>无需处理</option></select></div><button class="button button-secondary" type="submit">${icon('search')}筛选</button></form><div class="table-tool"><div class="table-scroll"><table class="data-table"><thead><tr><th>反馈来源</th><th>评价</th><th>原因</th><th>说明</th><th class="numeric">本题费用</th><th>状态</th><th></th></tr></thead><tbody>${feedbackSummaryRows(data.items)}</tbody></table></div><div class="pagination"><span>共 ${Number(data.total)} 条</span><button class="icon-button" id="prev-page" type="button" aria-label="上一页" ${filters.offset <= 0 ? 'disabled' : ''}>${icon('back')}</button><button class="icon-button" id="next-page" type="button" aria-label="下一页" ${filters.offset + filters.limit >= data.total ? 'disabled' : ''}>${icon('next')}</button></div></div>`;
    document.querySelector('#feedback-filters').addEventListener('submit', (event) => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      const next = new URLSearchParams();
      for (const [key, value] of form.entries()) if (String(value).trim()) next.set(key, String(value).trim());
      location.search = next.toString();
    });
    const page = (offset) => { const next = new URLSearchParams(location.search); next.set('offset', String(Math.max(0, offset))); location.search = next.toString(); };
    document.querySelector('#prev-page').addEventListener('click', () => page(filters.offset - filters.limit));
    document.querySelector('#next-page').addEventListener('click', () => page(filters.offset + filters.limit));
    bindFeedbackLinks();
  } catch (error) { body.innerHTML = errorState(error.message); }
}

function conversationHtml(data) {
  if (!data.conversation?.length) return `<div class="case-expired">${icon('image')}<br>${data.case_purged_at ? '对话案例已按保留期限自动清理。' : '这条反馈没有可用的对话快照。'}</div>`;
  return data.conversation.map((message) => {
    const target = message.message_id === data.message_id;
    const images = (message.images || []).map((url) => `<a href="${attr(url)}" target="_blank" rel="noreferrer"><img src="${attr(url)}" alt="${attr(message.image_alt || '反馈案例图片')}"></a>`).join('');
    return `<article class="message ${message.role === 'user' ? 'user' : ''} ${target ? 'target' : ''}" ${target ? 'id="feedback-target"' : ''}>${message.role === 'assistant' ? '<div class="message-avatar" aria-hidden="true">力</div>' : ''}<div class="message-body">${target ? `<span class="target-label">${icon(data.rating === 'negative' ? 'down' : 'up')}被反馈的回复</span>` : ''}<p class="message-text">${escapeHtml(message.message)}</p>${images ? `<div class="message-media">${images}</div>` : ''}<time class="message-time">${message.created_at ? formatDateTime(message.created_at) : ''}</time></div></article>`;
  }).join('');
}

async function renderFeedbackDetail(feedbackId) {
  const body = mountShell('feedback', '反馈详情', '查看用户反馈时的完整对话和本题运行信息。');
  try {
    const data = await api(`/api/admin/feedback/${encodeURIComponent(feedbackId)}`);
    const back = sessionStorage.getItem('tiku-admin-feedback-return') || '/feedback';
    body.innerHTML = `<a class="back-link" href="${attr(back)}">${icon('back')}返回反馈列表</a><div class="detail-layout"><section><header class="conversation-head"><h2>完整对话</h2>${statusBadge(data.rating, ratingText(data.rating))}</header><div class="conversation">${conversationHtml(data)}</div></section><aside class="detail-side"><section class="detail-section"><h2>反馈信息</h2><dl class="facts"><dt>邀请码</dt><dd>${escapeHtml(data.invite_label)}</dd><dt>提交时间</dt><dd>${formatFullDate(data.created_at)}</dd><dt>章节</dt><dd>${escapeHtml(data.chapter || '未确定')}</dd><dt>反馈原因</dt><dd>${data.tags.map((tag) => escapeHtml(TAG_LABELS[tag] || tag)).join('、') || '未选择'}</dd><dt>补充说明</dt><dd>${escapeHtml(data.detail || '未填写')}</dd></dl></section><section class="detail-section"><h2>本题运行</h2><dl class="facts"><dt>估算费用</dt><dd>¥${escapeHtml(data.cost.estimated_cost_cny)}</dd><dt>模型调用</dt><dd>${Number(data.cost.model_call_count)} 次</dd><dt>开始时间</dt><dd>${formatDateTime(data.cost.started_at)}</dd><dt>结束时间</dt><dd>${formatDateTime(data.cost.finished_at)}</dd><dt>候选数量</dt><dd>${Number(data.candidate_count)} 个</dd></dl></section><section class="detail-section"><h2>处理记录</h2><form class="review-form" id="review-form"><div class="field"><label for="review-status">处理状态</label><select class="select" id="review-status" name="review_status"><option value="pending" ${data.review_status === 'pending' ? 'selected' : ''}>待处理</option><option value="resolved" ${data.review_status === 'resolved' ? 'selected' : ''}>已处理</option><option value="no_action" ${data.review_status === 'no_action' ? 'selected' : ''}>无需处理</option></select></div><div class="field"><label for="admin-note">内部备注</label><textarea class="textarea" id="admin-note" name="admin_note" maxlength="2000" placeholder="记录判断、原因或后续动作…">${escapeHtml(data.admin_note || '')}</textarea></div><button class="button button-primary" type="submit">${icon('save')}保存处理结果</button></form></section></aside></div>`;
    document.querySelector('#review-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const button = form.querySelector('[type="submit"]');
      button.disabled = true;
      try { await api(`/api/admin/feedback/${encodeURIComponent(feedbackId)}/review`, { method: 'PATCH', body: JSON.stringify({ review_status: form.review_status.value, admin_note: form.admin_note.value.trim() }) }); showToast('处理结果已保存'); }
      catch (error) { showToast(error.message, 'error'); }
      finally { button.disabled = false; }
    });
    setTimeout(() => document.querySelector('#feedback-target')?.scrollIntoView({ block: 'center', behavior: 'smooth' }), 100);
  } catch (error) { body.innerHTML = errorState(error.message); }
}

function auditHtml(items) {
  return items.map((item) => `<div class="audit-item"><strong>${escapeHtml(AUDIT_LABELS[item.action] || item.action)}</strong><span>${escapeHtml(item.target_id)} · ${formatFullDate(item.created_at)}</span></div>`).join('') || '<p class="cell-sub">暂无操作记录</p>';
}

async function renderSettings() {
  const body = mountShell('settings', '后台设置', '调整全站费用保护、默认单码额度和反馈案例保留期限。');
  try {
    const data = await api('/api/admin/settings');
    body.innerHTML = `<div class="settings-grid"><div><section class="settings-section"><h2>费用与反馈</h2><p>8790 接入控制库后，会在每次请求前读取这里的额度。降低到今日已用金额以下时，新任务会立即停止。</p><form class="form-stack" id="settings-form"><div class="field-row"><div class="field"><label for="global-budget">全站每日费用上限</label><input class="input" id="global-budget" name="global_daily_budget_cny" type="number" min="0.01" max="10000" step="0.01" value="${attr(data.global_daily_budget_cny)}"><span class="field-hint">人民币估算费用，不是供应商余额。</span></div><div class="field"><label for="invite-budget-default">默认单码每日上限</label><input class="input" id="invite-budget-default" name="default_invite_daily_budget_cny" type="number" min="0.01" max="10000" step="0.01" value="${attr(data.default_invite_daily_budget_cny)}"><span class="field-hint">未单独设置的邀请码继承此值。</span></div></div><div class="field"><label for="retention">反馈案例保留天数</label><input class="input" id="retention" name="feedback_retention_days" type="number" min="1" max="365" step="1" value="${Number(data.feedback_retention_days)}"><span class="field-hint">到期后清理对话和图片，反馈评分与处理状态继续保留。</span></div><div class="settings-actions"><button class="button button-primary" type="submit">${icon('save')}保存设置</button></div></form></section><section class="settings-section"><h2>修改管理员密码</h2><p>修改后所有现有后台登录状态都会立即失效。</p><form class="form-stack" id="password-form"><div class="field"><label for="current-password">当前密码</label><input class="input" id="current-password" name="current_password" type="password" autocomplete="current-password" required></div><div class="field-row"><div class="field"><label for="new-password">新密码</label><input class="input" id="new-password" name="new_password" type="password" autocomplete="new-password" minlength="12" maxlength="256" required></div><div class="field"><label for="confirm-password">确认新密码</label><input class="input" id="confirm-password" name="confirm_password" type="password" autocomplete="new-password" minlength="12" maxlength="256" required></div></div><div class="settings-actions"><button class="button button-secondary" type="submit">修改密码</button></div></form></section></div><aside><div class="section-title settings-aside-title"><h2>最近操作</h2><p>保留后台关键管理动作。</p></div><div class="audit-list">${auditHtml(data.audit)}</div></aside></div>`;
    document.querySelector('#settings-form').addEventListener('submit', async (event) => {
      event.preventDefault(); const form = event.currentTarget; const button = form.querySelector('[type="submit"]'); button.disabled = true;
      try { await api('/api/admin/settings', { method: 'PATCH', body: JSON.stringify({ global_daily_budget_cny: form.global_daily_budget_cny.value, default_invite_daily_budget_cny: form.default_invite_daily_budget_cny.value, feedback_retention_days: Number(form.feedback_retention_days.value) }) }); showToast('设置已保存'); await renderSettings(); }
      catch (error) { showToast(error.message, 'error'); } finally { button.disabled = false; }
    });
    document.querySelector('#password-form').addEventListener('submit', async (event) => {
      event.preventDefault(); const form = event.currentTarget; const button = form.querySelector('[type="submit"]'); button.disabled = true;
      try { await api('/api/admin/password', { method: 'PATCH', body: JSON.stringify({ current_password: form.current_password.value, new_password: form.new_password.value, confirm_password: form.confirm_password.value }) }); showToast('密码已修改，请重新登录'); setTimeout(() => location.assign('/login'), 800); }
      catch (error) { showToast(error.message, 'error'); button.disabled = false; }
    });
  } catch (error) { body.innerHTML = errorState(error.message); }
}

async function bootstrap() {
  app.innerHTML = '<div class="loading-screen"><span class="loading-mark">正在载入管理后台</span></div>';
  let session;
  try { session = await api('/api/admin/session', {}, { allow401: true }); }
  catch (error) { app.innerHTML = errorState(error.message); return; }
  csrfToken = session.csrf_token || '';
  const path = location.pathname.replace(/\/+$/, '') || '/';
  if (path === '/setup') {
    if (!session.setup_required) { location.replace(session.authenticated ? '/overview' : '/login'); return; }
    if (!session.local_setup_allowed) { app.innerHTML = '<div class="loading-screen"><span>管理员尚未初始化，请先在服务所在电脑上访问此页面。</span></div>'; return; }
    renderAuth('setup'); return;
  }
  if (!session.authenticated) {
    if (session.setup_required && session.local_setup_allowed) { location.replace('/setup'); return; }
    renderAuth('login'); return;
  }
  if (path === '/' || path === '/login') { location.replace('/overview'); return; }
  if (path === '/overview') return renderOverview();
  if (path === '/invitations') return renderInvitations();
  if (path === '/feedback') return renderFeedback();
  if (path.startsWith('/feedback/')) return renderFeedbackDetail(path.split('/')[2] || '');
  if (path === '/settings') return renderSettings();
  location.replace('/overview');
}

bootstrap();
