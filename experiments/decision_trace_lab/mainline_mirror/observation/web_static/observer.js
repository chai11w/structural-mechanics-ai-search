(() => {
'use strict';

const EVENT_LABELS = {
  turn_started: '回合开始', intent_decided: '意图判断', authorization_checked: '权限校验',
  tool_started: '工具开始', tool_completed: '工具结果', state_transition: '状态变化',
  turn_completed: '最终结果'
};
const TOOL_LABELS = {
  analyze_multi_image: '判断单题或多题', prepare_question_units: '拆分并准备多题',
  analyze_image: '识别题图、章节与荷载', route_bank: '选择主库或字母库',
  classify_structure: '识别结构类型', coarse_search: '题库粗筛',
  global_search: '全章节严格搜索', rerank_candidates: '候选视觉复筛',
  answer_candidate: '获取候选答案'
};
const RESULT_OPTIONS = [
  ['correct', '', '正确'],
  ['incorrect', '', '错误'],
  ['uncertain', 'partial_correct', '部分正确'],
  ['uncertain', 'insufficient_evidence', '无法判断']
];
const NODE_VERDICTS = [['incorrect', '判断有误'], ['correct', '判断正确'], ['uncertain', '还不确定']];
const NO_MATCH = [
  ['reasonable_no_match', '合理无结果'],
  ['false_no_match', '错误无结果'],
  ['uncertain_no_match', '暂时无法判断']
];
const ISSUE_TEXT = {
  unknown_event: '出现无法识别的轨迹事件', sequence_not_contiguous: '事件顺序不连续',
  turn_started_cardinality: '回合开始记录数量异常', turn_completed_cardinality: '回合完成记录数量异常',
  authorization_trace_count_mismatch: '权限校验记录数量不一致', tool_pair_mismatch: '工具开始与完成记录不配对',
  embedded_automatic_check_failed: '状态自动检查未通过', privacy_forbidden_key: '轨迹含禁止字段',
  privacy_oversized_string: '轨迹摘要过长', privacy_absolute_path: '轨迹含绝对路径'
};

const panel = document.querySelector('#observer-panel');
const toggle = document.querySelector('#observer-toggle');
let currentTurnId = '';
let currentEvents = [];
let currentIssues = [];
let currentSummary = null;
let currentLabels = new Map();
let refreshTimer;

const labelKey = (targetId, dimension) => `${targetId}::${dimension}`;
const getLabel = (targetId, dimension) => currentLabels.get(labelKey(targetId, dimension));
const putLabel = label => {
  const key = labelKey(label.target_id, label.dimension);
  if (label.label_state === 'withdrawn') currentLabels.delete(key);
  else currentLabels.set(key, label);
};
const eventLabel = value => EVENT_LABELS[value] || `${value || 'unknown'}（未知事件）`;
const toolLabel = value => TOOL_LABELS[value] || `${value || 'unknown'}（未知工具）`;

toggle?.addEventListener('click', () => {
  const open = panel.classList.toggle('is-open');
  toggle.setAttribute('aria-expanded', String(open));
});

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(body.detail || `${url} returned HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function showLoadError(error) {
  const alerts = document.querySelector('#observer-alerts');
  if (!alerts) return;
  const node = document.createElement('p');
  node.className = 'observer-alert';
  node.textContent = `观察面板加载失败：${error?.message || 'unknown error'}`;
  alerts.replaceChildren(node);
}

function textNode(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = text;
  return node;
}

function resultStatus(label) {
  if (!label) return '';
  if (label.verdict === 'correct') return 'correct';
  if (label.verdict === 'incorrect') return 'incorrect';
  if (label.error_category === 'partial_correct') return 'partial_correct';
  return 'insufficient_evidence';
}

function resultOptionStatus(verdict, category) {
  if (verdict === 'correct') return 'correct';
  if (verdict === 'incorrect') return 'incorrect';
  return category;
}

async function postLabel(payload, statusNode) {
  if (statusNode) {
    statusNode.textContent = '正在保存…';
    statusNode.classList.remove('is-error');
  }
  try {
    const label = await fetchJson('/api/observation/labels', {
      method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(payload)
    });
    putLabel(label);
    if (statusNode) statusNode.textContent = label.unchanged ? '已保存' : '已保存';
    return label;
  } catch (error) {
    if (statusNode) {
      statusNode.textContent = error.status === 400
        ? '保存失败：内容可能包含不允许的敏感信息、路径或过长文字'
        : '保存失败，请重试';
      statusNode.classList.add('is-error');
    }
    throw error;
  }
}

function resultLabelPayload(verdict, category, details = {}) {
  const current = getLabel(currentTurnId, 'result_interpretation');
  return {
    target_type: 'turn', target_id: currentTurnId, dimension: 'result_interpretation', verdict,
    error_category: category || details.error_category || '',
    no_match_classification: current?.no_match_classification || '',
    reason: details.reason || current?.reason || '', expected: details.expected || current?.expected || ''
  };
}

function renderResultCard() {
  const host = document.querySelector('#observer-result-card');
  if (!currentSummary || !currentTurnId) {
    host.replaceChildren(textNode('p', 'observer-empty', '完成一次对话后，可在这里评审结果。'));
    document.querySelector('#observer-causal-section').hidden = true;
    return;
  }
  const label = getLabel(currentTurnId, 'result_interpretation');
  const status = resultStatus(label);
  const card = document.createElement('article');
  card.className = 'observer-result-card';
  card.append(
    textNode('p', 'observer-eyebrow', '用户输入（安全摘要）'),
    textNode('p', 'observer-summary-text', currentSummary.input_summary),
    textNode('p', 'observer-eyebrow', 'Agent 最终回答（安全摘要）'),
    textNode('p', 'observer-summary-text observer-final-summary', currentSummary.result_summary)
  );

  const context = textNode('p', 'observer-context', [
    currentSummary.context?.chapter ? `章节：${currentSummary.context.chapter}` : '章节：未记录',
    currentSummary.context?.has_active_image ? '有题图' : '无题图',
    `结束状态：${currentSummary.context?.phase_after || '未知'}`
  ].join(' · '));
  card.append(context);

  const issue = textNode(
    'p', currentSummary.automatic_issue_count ? 'observer-issue-summary has-issues' : 'observer-issue-summary',
    currentSummary.automatic_issue_count
      ? `自动检查：${currentSummary.automatic_issue_count} 个可疑点（仅供核验，不代表错误）`
      : '自动检查：未发现异常（不等于结果正确）'
  );
  card.append(issue, textNode('p', 'observer-question', '这次最终结果怎么样？'));

  const controls = document.createElement('div');
  controls.className = 'observer-result-actions';
  const saveStatus = textNode('p', 'observer-save-status', '');
  for (const [verdict, category, title] of RESULT_OPTIONS) {
    const button = textNode('button', '', title);
    button.type = 'button';
    const selected = status === resultOptionStatus(verdict, category);
    button.classList.toggle('is-selected', selected);
    button.setAttribute('aria-pressed', String(selected));
    button.addEventListener('click', async () => {
      [...controls.querySelectorAll('button')].forEach(item => { item.disabled = true; });
      try {
        await postLabel(resultLabelPayload(verdict, category), saveStatus);
        renderResultCard();
        renderCausalChain();
      } catch (_error) {
        [...controls.querySelectorAll('button')].forEach(item => { item.disabled = false; });
      }
    });
    controls.append(button);
  }
  card.append(controls);

  if (label && status === 'correct') {
    card.append(textNode('p', 'observer-done', '本轮已完成评审，无需继续复核；所有中间节点保持未复核。'));
  }
  if (label && status !== 'correct') {
    card.append(optionalResultDetails(label, saveStatus));
  }
  if (currentSummary.is_no_match) card.append(noMatchControls(label, saveStatus));
  card.append(saveStatus);
  host.replaceChildren(card);
  document.querySelector('#observer-causal-section').hidden = !label || status === 'correct';
}

function optionalResultDetails(label, statusNode) {
  const details = document.createElement('details');
  details.className = 'observer-optional-details';
  details.append(textNode('summary', '', '补充错误原因（可选）'));
  const reason = document.createElement('textarea');
  reason.placeholder = '错误原因（可选）'; reason.value = label.reason || ''; reason.rows = 3;
  const category = document.createElement('input');
  category.type = 'text'; category.placeholder = '错误类别（可选）';
  category.value = ['partial_correct', 'insufficient_evidence'].includes(label.error_category) ? '' : (label.error_category || '');
  const expected = document.createElement('input');
  expected.type = 'text'; expected.placeholder = '期望结果（可选，不填写也能保存）'; expected.value = label.expected || '';
  const save = textNode('button', 'observer-secondary-button', '保存补充说明'); save.type = 'button';
  save.addEventListener('click', async () => {
    const status = resultStatus(label);
    const mapping = {
      incorrect: ['incorrect', category.value.trim()], partial_correct: ['uncertain', 'partial_correct'],
      insufficient_evidence: ['uncertain', 'insufficient_evidence']
    }[status] || ['uncertain', 'insufficient_evidence'];
    save.disabled = true;
    try {
      await postLabel(resultLabelPayload(mapping[0], mapping[1], {
        reason: reason.value.trim(), error_category: mapping[1] || category.value.trim(), expected: expected.value.trim()
      }), statusNode);
      renderResultCard();
    } catch (_error) { save.disabled = false; }
  });
  details.append(reason, category, expected, save);
  return details;
}

function noMatchControls(label, statusNode) {
  const group = document.createElement('fieldset');
  group.className = 'observer-no-match';
  group.append(textNode('legend', '', '这次“没有结果”是否合理？'));
  for (const [value, title] of NO_MATCH) {
    const button = textNode('button', '', title); button.type = 'button';
    button.disabled = !label;
    const selected = label?.no_match_classification === value;
    button.classList.toggle('is-selected', selected);
    button.setAttribute('aria-pressed', String(selected));
    button.addEventListener('click', async () => {
      const active = getLabel(currentTurnId, 'result_interpretation');
      if (!active) return;
      const payload = {
        target_type: 'turn', target_id: currentTurnId, dimension: 'result_interpretation',
        verdict: active.verdict, error_category: active.error_category || '', reason: active.reason || '',
        expected: active.expected || '', no_match_classification: value
      };
      button.disabled = true;
      try { await postLabel(payload, statusNode); renderResultCard(); } catch (_error) { button.disabled = false; }
    });
    group.append(button);
  }
  if (!label) group.append(textNode('p', 'observer-hint', '请先判断本轮最终结果。'));
  return group;
}

function conciseEvent(event) {
  const payload = event.payload || {};
  if (event.event_type === 'turn_started') {
    return `开始处理${payload.kind === 'image' ? '题目图片' : '文字消息'}；此前状态：${payload.phase_before || '未知'}`;
  }
  if (event.event_type === 'intent_decided') {
    return `决定动作：${payload.final_action || '未知'}；来源：${payload.source || '未知'}`;
  }
  if (event.event_type === 'authorization_checked') {
    return `${payload.allowed ? '允许' : '拒绝'}动作 ${payload.requested_action || '未知'}；代码：${payload.authorization_code || '无'}`;
  }
  if (event.event_type === 'tool_started') return `开始调用：${toolLabel(payload.tool_name)}`;
  if (event.event_type === 'tool_completed') {
    const summary = payload.output_summary || {};
    const details = Object.entries(summary).map(([key, value]) => `${key}=${String(value)}`).join('，');
    return `${toolLabel(payload.tool_name)}：${payload.ok ? '完成' : '失败'}${details ? `；${details}` : ''}`;
  }
  if (event.event_type === 'state_transition') {
    const changed = Object.keys(payload.changes || {}).join('、') || '无关键字段';
    return `${payload.phase_before || '未知'} → ${payload.phase_after || '未知'}；变化：${changed}`;
  }
  if (event.event_type === 'turn_completed') return currentSummary?.result_summary || '本轮执行完成';
  return '此事件暂无安全摘要';
}

function eventIssues(event) {
  return currentIssues.filter(issue => issue.event_id && issue.event_id === event.event_id);
}

function renderCausalChain() {
  const section = document.querySelector('#observer-causal-section');
  const result = getLabel(currentTurnId, 'result_interpretation');
  if (!result || resultStatus(result) === 'correct') {
    section.hidden = true;
    document.querySelector('#observer-causal-chain').replaceChildren();
    return;
  }
  section.hidden = false;
  const host = document.querySelector('#observer-causal-chain');
  const usefulEvents = [...currentEvents]
    .sort((left, right) => Number(left.sequence || 0) - Number(right.sequence || 0))
    .filter(isUsefulCausalEvent);
  if (!usefulEvents.length) {
    host.replaceChildren(textNode('p', 'observer-empty', '本轮没有需要展开的关键节点；你仍可只记录最终结果和错误原因。'));
    return;
  }
  host.replaceChildren(...usefulEvents.map((event, index) => causalNode(event, index + 1)));
}

function isUsefulCausalEvent(event) {
  if (eventIssues(event).length || getLabel(event.event_id, 'causal_suspicion')) return true;
  const payload = event.payload || {};
  if (event.event_type === 'intent_decided' || event.event_type === 'tool_completed') return true;
  if (event.event_type === 'authorization_checked') return payload.allowed === false;
  if (event.event_type === 'state_transition') {
    return payload.phase_before !== payload.phase_after || Object.keys(payload.changes || {}).length > 0;
  }
  return false;
}

function causalNode(event, displayIndex) {
  const issues = eventIssues(event);
  const label = getLabel(event.event_id, 'causal_suspicion');
  const selected = Boolean(label);
  const node = document.createElement('article');
  node.className = 'observer-causal-node';
  node.classList.toggle('has-issue', issues.length > 0);
  node.classList.toggle('is-selected', selected);
  node.dataset.eventId = event.event_id;
  node.dataset.sequence = event.sequence;
  const header = document.createElement('label');
  header.className = 'observer-node-select';
  const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = selected;
  header.append(checkbox, textNode('span', '', `${displayIndex}. ${eventLabel(event.event_type)}${event.payload?.tool_name ? ` · ${toolLabel(event.payload.tool_name)}` : ''}`));
  node.append(header, textNode('p', 'observer-node-summary', conciseEvent(event)));
  for (const issue of issues) {
    node.append(textNode('p', 'observer-node-issue', `自动提示：${ISSUE_TEXT[issue.code] || issue.code}`));
  }
  const form = nodeForm(event, label);
  form.hidden = !selected;
  node.append(form);
  checkbox.addEventListener('change', async () => {
    checkbox.disabled = true;
    const status = form.querySelector('.observer-save-status');
    try {
      await postLabel({
        target_type: 'event', target_id: event.event_id, dimension: 'causal_suspicion',
        ...(checkbox.checked
          ? {verdict: 'uncertain', error_category: 'suspected'}
          : {label_state: 'withdrawn'})
      }, status);
      form.hidden = !checkbox.checked;
      renderCausalChain();
    } catch (_error) {
      checkbox.checked = !checkbox.checked; checkbox.disabled = false;
    }
  });
  return node;
}

function nodeForm(event, label) {
  const form = document.createElement('div'); form.className = 'observer-node-form';
  const actions = document.createElement('div'); actions.className = 'observer-node-actions';
  const reason = document.createElement('textarea'); reason.rows = 2; reason.placeholder = '为什么怀疑这一步？（可选）'; reason.value = label?.reason || '';
  const status = textNode('span', 'observer-save-status', '');
  for (const [verdict, title] of NODE_VERDICTS) {
    const button = textNode('button', '', title); button.type = 'button';
    const selected = label?.verdict === verdict;
    button.classList.toggle('is-selected', selected);
    button.setAttribute('aria-pressed', String(selected));
    button.addEventListener('click', async () => {
      [...actions.querySelectorAll('button')].forEach(item => { item.disabled = true; });
      try {
        await postLabel({
          target_type: 'event', target_id: event.event_id, dimension: 'causal_suspicion', verdict,
          error_category: 'suspected', reason: reason.value.trim()
        }, status);
        renderCausalChain();
      } catch (_error) { [...actions.querySelectorAll('button')].forEach(item => { item.disabled = false; }); }
    });
    actions.append(button);
  }
  form.append(actions, reason, status);
  return form;
}

function rawCard(event) {
  const node = document.createElement('article'); node.className = 'observer-technical-card';
  node.append(textNode('h3', '', `${event.sequence}. ${eventLabel(event.event_type)}`));
  const details = document.createElement('details');
  details.append(textNode('summary', '', '查看原始 JSON'));
  const pre = textNode('pre', '', JSON.stringify(event.payload || {}, null, 2));
  details.append(pre); node.append(details); return node;
}

function renderAlerts() {
  const alerts = document.querySelector('#observer-alerts');
  const turnIssues = currentIssues.filter(issue => !issue.event_id);
  alerts.replaceChildren(...turnIssues.map(issue => textNode(
    'p', 'observer-alert', `自动检查：${ISSUE_TEXT[issue.code] || issue.code}`
  )));
  const badge = document.querySelector('#observer-toggle-badge');
  badge.textContent = currentIssues.length ? String(currentIssues.length) : '';
}

async function refreshObserver() {
  try {
    const source = await fetchJson('/api/observation/source');
    document.querySelector('#observer-source').textContent = `${source.source_branch}@${source.source_commit.slice(0, 12)} · 镜像已校验`;
    const turns = await fetchJson('/api/observation/turns');
    const latest = turns.turns?.[0];
    if (!latest) {
      currentTurnId = ''; currentEvents = []; currentIssues = []; currentSummary = null; currentLabels = new Map();
      renderResultCard(); renderCausalChain(); renderAlerts(); return;
    }
    const detail = await fetchJson(`/api/observation/turns/${encodeURIComponent(latest.turn_id)}`);
    currentTurnId = detail.turn_id;
    currentEvents = detail.events || [];
    currentIssues = detail.issues || [];
    currentSummary = detail.review_summary || null;
    currentLabels = new Map((detail.latest_labels || []).map(label => [labelKey(label.target_id, label.dimension), label]));
    renderResultCard(); renderCausalChain(); renderAlerts();
    document.querySelector('#observer-event-count').textContent = `事件 ${currentEvents.length} 条（按实际 sequence 排列）`;
    document.querySelector('#observer-events').replaceChildren(...currentEvents.map(rawCard));
  } catch (error) { showLoadError(error); }
}

const observer = new MutationObserver(() => {
  window.clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(refreshObserver, 150);
});
const chat = document.querySelector('#chat');
if (chat) observer.observe(chat, {childList: true, subtree: true});
refreshObserver();
})();
