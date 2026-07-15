(() => {
'use strict';

const EVENT_LABELS = {
  turn_started: 'turn_started（回合开始）', intent_decided: 'intent_decided（意图判断）',
  authorization_checked: 'authorization_checked（权限校验）', tool_started: 'tool_started（工具开始）',
  tool_completed: 'tool_completed（工具结果）', state_transition: 'state_transition（状态变化）',
  turn_completed: 'turn_completed（回合完成／最终结果）'
};
const TOOL_LABELS = {
  analyze_multi_image: 'analyze_multi_image（判断单题或多题）', prepare_question_units: 'prepare_question_units（拆分并准备多题）',
  analyze_image: 'analyze_image（识别题图、章节与荷载）', route_bank: 'route_bank（选择主库或字母库）',
  classify_structure: 'classify_structure（识别结构类型）', coarse_search: 'coarse_search（题库粗筛）',
  global_search: 'global_search（全章节严格搜索）', rerank_candidates: 'rerank_candidates（候选视觉复筛）',
  answer_candidate: 'answer_candidate（获取候选答案）'
};
const VERDICTS = [['correct', 'correct（正确）'], ['incorrect', 'incorrect（错误）'], ['uncertain', 'uncertain（不确定）']];
const NO_MATCH = [
  ['reasonable_no_match', 'reasonable_no_match（合理无结果）'],
  ['false_no_match', 'false_no_match（错误无结果）'],
  ['uncertain_no_match', 'uncertain_no_match（暂时无法判断）']
];
const reviewTypes = new Set(['intent_decided', 'tool_completed', 'turn_completed']);
const eventLabel = value => EVENT_LABELS[value] || `${value || 'unknown'}（未知事件）`;
const toolLabel = value => TOOL_LABELS[value] || `${value || 'unknown'}（未知工具）`;
// Fallback examples retained verbatim for acceptance: unknown（未知事件） / unknown（未知工具）.

const panel = document.querySelector('#observer-panel');
const toggle = document.querySelector('#observer-toggle');
let currentReviews = [];
let currentLabels = new Map();
let savedTargetId = '';
let savedMessage = '';
let savedTimer;
let refreshTimer;

toggle?.addEventListener('click', () => {
  const open = panel.classList.toggle('is-open');
  toggle.setAttribute('aria-expanded', String(open));
});

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`${url} returned HTTP ${response.status}`);
  return response.json();
}

function showLoadError(error) {
  const alerts = document.querySelector('#observer-alerts');
  if (!alerts) return;
  const node = document.createElement('p');
  node.className = 'observer-alert';
  node.textContent = `观察面板加载失败：${error?.message || 'unknown error'}`;
  alerts.replaceChildren(node);
  const technical = document.querySelector('#observer-technical');
  if (technical) technical.open = true;
}

function dimensionFor(event) {
  return event.event_type === 'intent_decided' ? 'intent' : event.event_type === 'tool_completed' ? 'tool_output' : 'result_interpretation';
}

function payloadView(event) {
  const payload = { ...event.payload };
  if (event.event_type === 'tool_completed') payload.tool_name_display = toolLabel(payload.tool_name);
  return JSON.stringify(payload, null, 2);
}

function sameLabel(current, verdict, noMatch, details) {
  if (!current || current.verdict !== verdict || (current.no_match_classification || '') !== noMatch) return false;
  return ['expected', 'reason', 'error_category'].every(key => (current[key] || '') === (details[key] || ''));
}

async function saveLabel(event, verdict, noMatch, details, cardNode, clickedButton) {
  const current = currentLabels.get(event.event_id);
  if (sameLabel(current, verdict, noMatch, details)) return;

  const controls = [...cardNode.querySelectorAll('button, input')];
  const originalText = clickedButton.textContent;
  controls.forEach(control => { control.disabled = true; });
  clickedButton.textContent = '正在保存…';
  const status = cardNode.querySelector('.observer-save-status');
  status.textContent = '正在保存…';
  status.classList.remove('is-error');

  try {
    const label = await fetchJson('/api/observation/labels', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        target_type: 'event', target_id: event.event_id, dimension: dimensionFor(event),
        verdict, no_match_classification: noMatch, ...details
      })
    });
    currentLabels.set(event.event_id, label);
    savedTargetId = event.event_id;
    savedMessage = current ? '已保存' : '已保存 · 已移至队列下方';
    renderReviewQueue();
    window.clearTimeout(savedTimer);
    savedTimer = window.setTimeout(() => {
      savedTargetId = '';
      savedMessage = '';
      const saved = document.querySelector(`[data-event-id="${event.event_id}"] .observer-save-status`);
      if (saved) saved.textContent = '';
    }, 1400);
  } catch (_error) {
    controls.forEach(control => { control.disabled = false; });
    clickedButton.textContent = originalText;
    status.textContent = '保存失败，请重试';
    status.classList.add('is-error');
  }
}

function verdictButtons(event, label, node) {
  const buttons = document.createElement('div');
  buttons.className = 'observer-labels';
  let incorrectForm;
  for (const [value, text] of VERDICTS) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = text;
    const selected = label?.verdict === value;
    button.classList.toggle('is-selected', selected);
    button.setAttribute('aria-pressed', String(selected));
    button.addEventListener('click', () => saveLabel(
      event, value, label?.no_match_classification || '',
      value === 'incorrect' ? {
        expected: label?.expected || '', reason: label?.reason || '', error_category: label?.error_category || ''
      } : {},
      node, button
    ));
    buttons.append(button);
  }
  node.append(buttons);

  incorrectForm = document.createElement('div');
  incorrectForm.className = 'observer-incorrect-form';
  incorrectForm.hidden = true;
  const fields = {};
  for (const [key, placeholder] of [['expected', 'expected（期望结果，可选）'], ['reason', 'reason（错误原因，可选）'], ['error_category', 'error_category（可选）']]) {
    const input = document.createElement('input');
    input.type = 'text'; input.placeholder = placeholder; input.value = label?.[key] || '';
    fields[key] = input; incorrectForm.append(input);
  }
  const save = document.createElement('button');
  save.type = 'button'; save.textContent = '保存 incorrect（错误）';
  save.addEventListener('click', () => saveLabel(event, 'incorrect', label?.no_match_classification || '', {
    expected: fields.expected.value.trim(), reason: fields.reason.value.trim(), error_category: fields.error_category.value.trim()
  }, node, save));
  incorrectForm.append(save);
  node.append(incorrectForm);
  if (label?.verdict === 'incorrect') {
    const explain = document.createElement('button');
    explain.type = 'button'; explain.className = 'observer-explanation-toggle';
    explain.textContent = label?.expected || label?.reason || label?.error_category ? '查看／修改说明' : '补充说明（可选）';
    explain.addEventListener('click', () => { incorrectForm.hidden = !incorrectForm.hidden; });
    node.append(explain);
  }
}

function noMatchButtons(event, label, node) {
  if (event.event_type !== 'turn_completed' || event.payload?.response_type !== 'no_match') return;
  const group = document.createElement('div');
  group.className = 'observer-no-match';
  for (const [value, text] of NO_MATCH) {
    const button = document.createElement('button');
    button.type = 'button'; button.textContent = text;
    const selected = label?.no_match_classification === value;
    button.classList.toggle('is-selected', selected);
    button.setAttribute('aria-pressed', String(selected));
    button.addEventListener('click', () => saveLabel(event, label?.verdict || 'uncertain', value, {
      expected: label?.expected || '', reason: label?.reason || '', error_category: label?.error_category || ''
    }, node, button));
    group.append(button);
  }
  node.append(group);
}

function card(event, reviewable) {
  const label = currentLabels.get(event.event_id);
  const node = document.createElement('article');
  node.className = 'observer-card'; node.dataset.eventId = event.event_id;
  const title = document.createElement('h3');
  title.textContent = event.event_type === 'tool_completed' ? `${eventLabel(event.event_type)} · ${toolLabel(event.payload?.tool_name)}` : eventLabel(event.event_type);
  node.append(title);
  const pre = document.createElement('pre'); pre.textContent = payloadView(event); node.append(pre);
  if (reviewable) {
    verdictButtons(event, label, node);
    noMatchButtons(event, label, node);
    const status = document.createElement('span');
    status.className = 'observer-save-status';
    status.setAttribute('role', 'status');
    if (savedTargetId === event.event_id) status.textContent = label?.unchanged ? '' : savedMessage;
    node.append(status);
  }
  return node;
}

function queueGroup(titleText, events) {
  const section = document.createElement('section');
  section.className = 'observer-queue-group';
  const title = document.createElement('h3'); title.textContent = titleText; section.append(title);
  section.append(...events.map(event => card(event, true)));
  return section;
}

function renderReviewQueue() {
  const ordered = [...currentReviews].sort((left, right) => Number(left.sequence || 0) - Number(right.sequence || 0));
  const pending = ordered.filter(event => !currentLabels.has(event.event_id));
  const reviewed = ordered.filter(event => currentLabels.has(event.event_id));
  document.querySelector('#observer-review-count').textContent = `待复核 ${pending.length} · 已复核 ${reviewed.length} · 共 ${ordered.length} 个关键项`;
  document.querySelector('#observer-review-items').replaceChildren(
    queueGroup('待复核', pending), queueGroup('已复核', reviewed)
  );
}

async function refreshObserver() {
  try {
    const source = await fetchJson('/api/observation/source');
    document.querySelector('#observer-source').textContent = `${source.source_branch}@${source.source_commit.slice(0, 12)} · ${source.verified_files} files verified`;
    const turns = await fetchJson('/api/observation/turns');
    const latest = turns.turns?.[0];
    if (!latest) {
      currentReviews = []; currentLabels = new Map(); renderReviewQueue(); return;
    }
    const detail = await fetchJson(`/api/observation/turns/${encodeURIComponent(latest.turn_id)}`);
    const events = detail.events || [];
    currentReviews = events.filter(event => reviewTypes.has(event.event_type));
    currentLabels = new Map((detail.latest_labels || []).map(label => [label.target_id, label]));
    renderReviewQueue();
    document.querySelector('#observer-event-count').textContent = `事件 ${events.length} 条（按 sequence 动态排列）`;
    document.querySelector('#observer-events').replaceChildren(...events.map(event => card(event, false)));
    const alerts = document.querySelector('#observer-alerts');
    alerts.replaceChildren(...(detail.issues || []).map(issue => {
      const node = document.createElement('p'); node.className = 'observer-alert'; node.textContent = `${issue.code}（自动检查异常）`; return node;
    }));
    if ((detail.issues || []).length) document.querySelector('#observer-technical').open = true;
  } catch (error) {
    showLoadError(error);
  }
}

const observer = new MutationObserver(() => {
  window.clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(refreshObserver, 150);
});
const chat = document.querySelector('#chat');
if (chat) observer.observe(chat, { childList: true, subtree: true });
refreshObserver();
})();
