(() => {
'use strict';

const EVENT_LABELS = {
  turn_started: '开始处理', intent_decided: '意图判断', authorization_checked: '处理规则',
  tool_started: '开始执行', tool_completed: '工具调用', state_transition: '状态变化',
  turn_completed: '回答生成'
};
const TOOL_LABELS = {
  analyze_multi_image: '识别题图', prepare_question_units: '拆分多道题',
  analyze_image: '识别题图信息', route_bank: '判断荷载形式', classify_structure: '识别结构类型',
  coarse_search: '搜索相似题', global_search: '全章节搜索',
  rerank_candidates: '复筛候选题', answer_candidate: '查找答案'
};
const RESULT_OPTIONS = [
  ['correct', '', '正确'],
  ['incorrect', '', '错误'],
  ['uncertain', 'partial_correct', '部分正确'],
  ['uncertain', 'insufficient_evidence', '无法判断']
];
const NO_MATCH = [
  ['reasonable_no_match', '合理无结果'],
  ['false_no_match', '错误无结果'],
  ['uncertain_no_match', '暂时无法判断']
];
const INTENT_TEXT = {
  greeting: '用户正在问候', small_talk: '用户想进行日常交流', capability_help: '用户在询问 Agent 能做什么',
  out_of_scope: '用户提出了题库范围外的问题', search_image: '用户想搜索当前题图', global_search: '用户想搜索全部章节',
  set_chapter: '用户想设置题目章节', select_question: '用户想选择多题中的一道题',
  select_candidate: '用户想选择一个候选题', reject_candidates: '用户认为当前候选题都不匹配',
  continue_search: '用户想继续搜索其他候选题', show_candidates: '用户想重新查看候选题',
  report_answer_mismatch: '用户反馈答案不匹配', resend_answer: '用户想重新查看答案',
  explain_failure: '用户在询问失败原因', retry_search: '用户想重新搜索', cancel: '用户想取消当前任务',
  reject: '用户请求了不允许执行的操作'
};
const PHASE_TEXT = {
  IDLE: '等待用户上传题图或输入要求',
  WAIT_CHAPTER: '等待用户补充题目章节',
  WAIT_QUESTION_CHOICE: '等待用户选择其中一道题',
  WAIT_CANDIDATE_CHOICE: '等待用户选择候选题',
  ANSWERED: '答案已经返回',
  NO_MATCH: '搜索完成，但没有找到可靠结果',
  ERROR: '处理失败，等待用户重试',
  CANCELLED: '当前任务已经取消'
};
const TOOL_OUTCOME_TEXT = {
  SUCCESS: '成功', NO_MATCH: '正常未命中', NEEDS_INPUT: '需要补充信息',
  PARTIAL: '部分完成', TOOL_ERROR: '工具故障'
};
const REPLY_KIND_TEXT = {
  greeting: '问候', small_talk: '日常交流', capability_help: '能力说明',
  out_of_scope: '范围说明', clarification: '追问确认', reject: '安全拒绝',
  safe_answer_fallback: '安全回答兜底',
  exception: '异常提示'
};
const CLARIFICATION_TEXT = {
  ambiguous_reference: '需要确认你指的是哪一道题或候选题',
  ambiguous_number_namespace: '需要确认这个编号是题号还是候选编号',
  ambiguous_action: '需要确认你想进行什么操作',
  missing_question_index: '需要你选择一道题', missing_candidate_rank: '需要你选择一个候选题',
  missing_chapter: '需要你补充章节', missing_image: '需要你上传题图',
  out_of_range: '你选择的编号超出范围', no_more_candidates: '当前没有更多候选题'
};
const ISSUE_TEXT = {
  unknown_event: '出现无法识别的轨迹事件', sequence_not_contiguous: '事件顺序不连续',
  turn_started_cardinality: '回合开始记录数量异常', turn_completed_cardinality: '回合完成记录数量异常',
  authorization_trace_count_mismatch: '安全校验记录数量不一致', tool_pair_mismatch: '工具执行记录不完整',
  embedded_automatic_check_failed: '状态检查未通过', privacy_forbidden_key: '轨迹含禁止字段',
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
let resultNotice = '';

const labelKey = (targetId, dimension) => `${targetId}::${dimension}`;
const getLabel = (targetId, dimension) => currentLabels.get(labelKey(targetId, dimension));
const putLabel = label => {
  const key = labelKey(label.target_id, label.dimension);
  if (label.label_state === 'withdrawn') currentLabels.delete(key);
  else currentLabels.set(key, label);
};
const eventLabel = value => EVENT_LABELS[value] || '其他步骤';
const toolLabel = value => TOOL_LABELS[value] || '执行内部工具';

toggle?.addEventListener('click', () => {
  const open = panel.classList.toggle('is-open');
  toggle.setAttribute('aria-expanded', String(open));
  toggle.textContent = open ? '关闭' : '评审';
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

function textNode(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = text;
  return node;
}

function showLoadError(error) {
  const alerts = document.querySelector('#observer-alerts');
  if (!alerts) return;
  console.error('observer refresh failed', error);
  alerts.replaceChildren(textNode('p', 'observer-alert', '评审信息暂时没有加载出来，请稍后重试。'));
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
    if (statusNode) statusNode.textContent = '已保存';
    return label;
  } catch (error) {
    if (statusNode) {
      statusNode.textContent = error.status === 400
        ? '保存失败：文字可能包含本地路径、敏感信息或内容过长'
        : '保存失败，请重试';
      statusNode.classList.add('is-error');
    }
    throw error;
  }
}

async function withdrawTurnReview(statusNode) {
  if (statusNode) statusNode.textContent = '正在取消…';
  const payload = await fetchJson(`/api/observation/turns/${encodeURIComponent(currentTurnId)}/withdraw-review`, {method: 'POST'});
  for (const label of payload.withdrawn_labels || []) putLabel(label);
  resultNotice = '已取消，本轮现在是未评审状态';
}

function resultLabelPayload(verdict, category, details = {}) {
  const current = getLabel(currentTurnId, 'result_interpretation');
  const hasReason = Object.prototype.hasOwnProperty.call(details, 'reason');
  return {
    target_type: 'turn', target_id: currentTurnId, dimension: 'result_interpretation', verdict,
    error_category: category || '', no_match_classification: current?.no_match_classification || '',
    reason: hasReason ? details.reason : (current?.reason || '')
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
  if (currentSummary.input_summary) {
    card.append(textNode('p', 'observer-eyebrow', '你的问题'), textNode('p', 'observer-summary-text', currentSummary.input_summary));
  }
  if (currentSummary.result_summary) {
    card.append(textNode('p', 'observer-eyebrow', 'Agent 的回答'), textNode('p', 'observer-summary-text observer-final-summary', currentSummary.result_summary));
  }
  card.append(textNode('p', 'observer-question', '这次回答对吗？'));
  const controls = document.createElement('div');
  controls.className = 'observer-result-actions';
  const saveStatus = textNode('p', 'observer-save-status', label
    ? '已保存 · 再点一次当前选项可取消'
    : (resultNotice || '选择后立即保存，不需要再提交'));
  for (const [verdict, category, title] of RESULT_OPTIONS) {
    const button = textNode('button', '', title);
    button.type = 'button';
    const selected = status === resultOptionStatus(verdict, category);
    button.classList.toggle('is-selected', selected);
    button.setAttribute('aria-pressed', String(selected));
    button.addEventListener('click', async () => {
      [...controls.querySelectorAll('button')].forEach(item => { item.disabled = true; });
      try {
        resultNotice = '';
        if (selected) {
          await withdrawTurnReview(saveStatus);
        } else {
          if (status && resultOptionStatus(verdict, category) === 'correct') {
            await withdrawTurnReview(saveStatus);
            resultNotice = '';
          }
          await postLabel(resultLabelPayload(verdict, category), saveStatus);
        }
        renderResultCard();
        renderCausalChain();
      } catch (_error) {
        saveStatus.textContent = selected ? '取消失败，请重试' : '保存失败，请重试';
        saveStatus.classList.add('is-error');
        [...controls.querySelectorAll('button')].forEach(item => { item.disabled = false; });
      }
    });
    controls.append(button);
  }
  card.append(controls, saveStatus);
  if (label && status !== 'correct') card.append(optionalResultDetails(label));
  if (currentSummary.is_no_match) card.append(noMatchControls(label));
  host.replaceChildren(card);
  document.querySelector('#observer-causal-section').hidden = !label || status === 'correct';
}

function optionalResultDetails(label) {
  const details = document.createElement('details');
  details.className = 'observer-optional-details';
  details.append(textNode('summary', '', '补充错误原因（可选）'));
  const reason = document.createElement('textarea');
  reason.placeholder = '例如：把“第一个候选题”理解成了“第一道题”';
  reason.value = label.reason || '';
  reason.rows = 3;
  const saveStatus = textNode('p', 'observer-save-status', label.reason ? '原因已保存' : '');
  const save = textNode('button', 'observer-secondary-button', '保存原因');
  save.type = 'button';
  save.addEventListener('click', async () => {
    const active = getLabel(currentTurnId, 'result_interpretation');
    if (!active) return;
    save.disabled = true;
    try {
      await postLabel(resultLabelPayload(active.verdict, active.error_category || '', {reason: reason.value.trim()}), saveStatus);
      saveStatus.textContent = '原因已保存';
      save.disabled = false;
    } catch (_error) { save.disabled = false; }
  });
  details.append(reason, save, saveStatus);
  return details;
}

function noMatchControls(label) {
  const group = document.createElement('fieldset');
  group.className = 'observer-no-match';
  group.append(textNode('legend', '', '这次“没有结果”是否合理？'));
  const saveStatus = textNode('p', 'observer-save-status', '');
  for (const [value, title] of NO_MATCH) {
    const button = textNode('button', '', title);
    button.type = 'button';
    button.disabled = !label;
    const selected = label?.no_match_classification === value;
    button.classList.toggle('is-selected', selected);
    button.setAttribute('aria-pressed', String(selected));
    button.addEventListener('click', async () => {
      const active = getLabel(currentTurnId, 'result_interpretation');
      if (!active) return;
      button.disabled = true;
      try {
        await postLabel({
          target_type: 'turn', target_id: currentTurnId, dimension: 'result_interpretation',
          verdict: active.verdict, error_category: active.error_category || '', reason: active.reason || '',
          no_match_classification: selected ? '' : value
        }, saveStatus);
        renderResultCard();
      } catch (_error) { button.disabled = false; }
    });
    group.append(button);
  }
  if (!label) group.append(textNode('p', 'observer-hint', '请先判断本轮回答。'));
  group.append(saveStatus);
  return group;
}

function humanIntent(payload) {
  const action = payload.final_action || '';
  if (action === 'clarification') return CLARIFICATION_TEXT[payload.clarification_reason] || '需要向你确认更多信息';
  if (action === 'set_chapter' && payload.chapter) return `用户想把题目章节设置为“${payload.chapter}”`;
  if (action === 'select_question' && payload.question_index != null) return `用户想选择第 ${Number(payload.question_index) + 1} 道题`;
  if (action === 'select_candidate' && payload.candidate_rank != null) return `用户想选择第 ${Number(payload.candidate_rank)} 个候选题`;
  return INTENT_TEXT[action] || '无法识别这句话的意图';
}

function humanStateResult(payload) {
  const before = String(payload.phase_before || '');
  const after = String(payload.phase_after || '');
  const beforeText = PHASE_TEXT[before] || '';
  const afterText = PHASE_TEXT[after] || '当前对话进度已更新';
  if (before !== after && beforeText) return `从“${beforeText}”进入“${afterText}”`;
  return afterText;
}

function humanReplyResult(payload) {
  const mode = String(payload.reply_mode || '');
  const kind = REPLY_KIND_TEXT[payload.reply_kind] || '当前任务';
  if (mode === 'fixed_shell') return `使用“${kind}”固定回复`;
  if (mode === 'llm_safe_reply') return '模型在安全边界内自由回答';
  if (mode === 'tool_result') return '根据工具结果组织回答';
  if (mode === 'error_reply') return '使用错误或异常提示';
  if (mode === 'business_renderer') return '根据当前任务状态组织业务回复';
  return '回答方式未记录';
}

function normalizedToolOutcome(payload) {
  const value = String(payload.outcome || '');
  if (TOOL_OUTCOME_TEXT[value]) return value;
  return payload.ok ? 'SUCCESS' : 'TOOL_ERROR';
}

function humanLoads(loads) {
  if (!Array.isArray(loads) || !loads.length) return '';
  return loads.map(load => {
    const type = String(load?.type || '荷载');
    const value = String(load?.value || '').trim();
    return value ? `${type}荷载：${value}` : `${type}荷载`;
  }).join('，');
}

function toolResultDetail(name, summary, outcome, code) {
  if (outcome === 'TOOL_ERROR') return `${toolLabel(name)}执行失败`;
  if (outcome === 'NEEDS_INPUT') return `${toolLabel(name)}需要用户补充信息`;
  if (outcome === 'NO_MATCH') {
    if (name === 'answer_candidate') return '没有找到所选候选题的答案文件';
    if (name === 'rerank_candidates') return '没有候选题可以复筛';
    if (name === 'coarse_search' || name === 'global_search') return '没有找到可靠候选题';
    return `${toolLabel(name)}已完成，但没有得到结果`;
  }
  if (name === 'analyze_multi_image') {
    const parts = [summary.is_multi ? '识别为多道题' : '识别为一道题'];
    const loads = humanLoads(summary.loads);
    if (loads) parts.push(loads);
    if (summary.chapter) parts.push(`章节：${summary.chapter}`);
    return parts.join('；');
  }
  if (name === 'prepare_question_units') return `识别出 ${summary.questions_count ?? 0} 道题`;
  if (name === 'analyze_image') {
    const parts = [];
    const loads = humanLoads(summary.loads);
    if (loads) parts.push(loads);
    else if (summary.loads_count != null) parts.push(`识别到 ${summary.loads_count} 个荷载`);
    if (summary.chapter) parts.push(`章节：${summary.chapter}`);
    return parts.length ? parts.join('，') : '题图信息识别完成';
  }
  if (name === 'route_bank') return summary.route === 'symbolic' ? '按字母荷载题检索' : '按数值荷载题检索';
  if (name === 'classify_structure') return summary.structure_type ? `识别为“${summary.structure_type}”` : '本次不需要结构类型筛选';
  if (name === 'coarse_search' || name === 'global_search') return `找到 ${summary.candidates_count ?? 0} 个候选题`;
  if (name === 'rerank_candidates') {
    if (code === 'RERANK_SKIPPED_NO_IMAGE') return '缺少题图，已直接使用粗筛排序';
    if (code === 'RERANK_INCOMPLETE_COARSE_FALLBACK' || code === 'RERANK_EMPTY_COARSE_FALLBACK') {
      return '复筛未完成，已回退使用粗筛排序';
    }
    return outcome === 'PARTIAL' || summary.rerank_complete === false ? '复筛未完整完成' : '复筛完成';
  }
  if (name === 'answer_candidate') return `找到 ${summary.answer_paths_count ?? summary.copied_paths_count ?? 0} 张答案`;
  return `${toolLabel(name)}完成`;
}

function humanToolResult(payload) {
  const name = payload.tool_name || '';
  const summary = payload.output_summary || {};
  const outcome = normalizedToolOutcome(payload);
  const detail = toolResultDetail(name, summary, outcome, String(payload.code || ''));
  if (outcome === 'SUCCESS') return detail;
  return `${TOOL_OUTCOME_TEXT[outcome]}：${detail}${payload.retryable ? '（可以重试）' : ''}`;
}

function conciseEvent(event) {
  const payload = event.payload || {};
  if (event.event_type === 'intent_decided') return `判断结果：${humanIntent(payload)}`;
  if (event.event_type === 'authorization_checked') return payload.allowed
    ? `当前条件允许继续执行“${INTENT_TEXT[payload.requested_action] || '这个操作'}”`
    : `原本理解为“${INTENT_TEXT[payload.requested_action] || '这个操作'}”，但当前条件不满足，已改为追问`;
  if (event.event_type === 'tool_completed') return humanToolResult(payload);
  if (event.event_type === 'state_transition') return `处理结果：${humanStateResult(payload)}`;
  if (event.event_type === 'turn_started') return `开始处理${payload.kind === 'image' ? '题目图片' : '文字消息'}`;
  if (event.event_type === 'tool_started') return `开始${toolLabel(payload.tool_name)}`;
  if (event.event_type === 'turn_completed') return `回答方式：${humanReplyResult(payload)}`;
  return '记录了一个内部步骤';
}

function eventIssues(event) {
  return currentIssues.filter(issue => issue.event_id && issue.event_id === event.event_id);
}

function isUsefulCausalEvent(event) {
  if (eventIssues(event).length || getLabel(event.event_id, 'causal_suspicion')) return true;
  const payload = event.payload || {};
  if (event.event_type === 'intent_decided') return true;
  if (event.event_type === 'tool_completed') {
    const outcome = normalizedToolOutcome(payload);
    if (outcome !== 'SUCCESS') return true;
    if (payload.tool_name === 'coarse_search' || payload.tool_name === 'global_search') return false;
    if (payload.tool_name === 'classify_structure' && payload.code === 'STRUCTURE_FILTER_NOT_APPLICABLE') return false;
    return true;
  }
  if (event.event_type === 'authorization_checked') return payload.allowed === false;
  if (event.event_type === 'state_transition') {
    return payload.phase_before !== payload.phase_after && Boolean(PHASE_TEXT[payload.phase_after]);
  }
  return false;
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
    host.replaceChildren(textNode('p', 'observer-empty', '本轮没有可继续定位的步骤，可以只填写错误原因。'));
    return;
  }
  host.replaceChildren(...usefulEvents.map((event, index) => causalNode(event, index + 1)));
}

function causalNode(event, displayIndex) {
  const issues = eventIssues(event);
  const label = getLabel(event.event_id, 'causal_suspicion');
  const selected = Boolean(label);
  const node = document.createElement('article');
  node.className = 'observer-causal-node';
  node.classList.toggle('has-issue', issues.length > 0);
  node.classList.toggle('is-selected', selected);
  const header = document.createElement('label');
  header.className = 'observer-node-select';
  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.checked = selected;
  const title = event.event_type === 'tool_completed'
    ? `${displayIndex}. ${toolLabel(event.payload?.tool_name || '')}`
    : `${displayIndex}. ${eventLabel(event.event_type)}${event.payload?.tool_name ? ` · ${toolLabel(event.payload.tool_name)}` : ''}`;
  header.append(checkbox, textNode('span', '', title));
  node.append(header, textNode('p', 'observer-node-summary', conciseEvent(event)));
  for (const issue of issues) node.append(textNode('p', 'observer-node-issue', `系统发现：${ISSUE_TEXT[issue.code] || '这一步的记录异常'}`));
  const form = nodeForm(event, label);
  form.hidden = !selected;
  node.append(form);
  checkbox.addEventListener('change', async () => {
    checkbox.disabled = true;
    const statusNode = form.querySelector('.observer-save-status');
    try {
      await postLabel({
        target_type: 'event', target_id: event.event_id, dimension: 'causal_suspicion',
        ...(checkbox.checked ? {verdict: 'incorrect', error_category: 'suspected'} : {label_state: 'withdrawn'})
      }, statusNode);
      renderCausalChain();
    } catch (_error) {
      checkbox.checked = !checkbox.checked;
      checkbox.disabled = false;
    }
  });
  return node;
}

function nodeForm(event, label) {
  const form = document.createElement('div');
  form.className = 'observer-node-form';
  const saved = textNode('p', 'observer-node-saved', '已保存为可能出错的步骤 · 取消勾选即可撤销');
  const reason = document.createElement('textarea');
  reason.rows = 2;
  reason.placeholder = '错误原因（可选）';
  reason.value = label?.reason || '';
  const save = textNode('button', 'observer-secondary-button', '保存原因');
  save.type = 'button';
  const status = textNode('p', 'observer-save-status', label?.reason ? '原因已保存' : '');
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      await postLabel({
        target_type: 'event', target_id: event.event_id, dimension: 'causal_suspicion',
        verdict: 'incorrect', error_category: 'suspected', reason: reason.value.trim()
      }, status);
      status.textContent = '原因已保存';
      save.disabled = false;
    } catch (_error) { save.disabled = false; }
  });
  form.append(saved, reason, save, status);
  return form;
}

function rawCard(event) {
  const node = document.createElement('article');
  node.className = 'observer-technical-card';
  node.append(textNode('h3', '', `${event.sequence}. ${eventLabel(event.event_type)}`));
  const details = document.createElement('details');
  details.append(textNode('summary', '', '查看原始 JSON'));
  details.append(textNode('pre', '', JSON.stringify(event.payload || {}, null, 2)));
  node.append(details);
  return node;
}

function renderTechnicalAlerts() {
  const alerts = document.querySelector('#observer-technical-alerts');
  alerts.replaceChildren(...currentIssues.map(issue => textNode(
    'p', 'observer-alert', `系统检查：${ISSUE_TEXT[issue.code] || issue.code}`
  )));
}

async function refreshObserver() {
  try {
    const source = await fetchJson('/api/observation/source');
    const sourceNode = document.querySelector('#observer-source');
    if (sourceNode) sourceNode.textContent = `来源：${source.source_branch}@${source.source_commit.slice(0, 12)} · 镜像已校验`;
    const turns = await fetchJson('/api/observation/turns');
    const latest = turns.turns?.[0];
    if (!latest) {
      currentTurnId = ''; currentEvents = []; currentIssues = []; currentSummary = null; currentLabels = new Map();
      renderResultCard(); renderCausalChain(); renderTechnicalAlerts(); return;
    }
    const detail = await fetchJson(`/api/observation/turns/${encodeURIComponent(latest.turn_id)}`);
    const turnChanged = currentTurnId && currentTurnId !== detail.turn_id;
    currentTurnId = detail.turn_id;
    currentEvents = detail.events || [];
    currentIssues = detail.issues || [];
    currentSummary = detail.review_summary || null;
    currentLabels = new Map((detail.latest_labels || []).map(label => [labelKey(label.target_id, label.dimension), label]));
    if (turnChanged) resultNotice = '';
    renderResultCard(); renderCausalChain(); renderTechnicalAlerts();
    const eventCountNode = document.querySelector('#observer-event-count');
    if (eventCountNode) eventCountNode.textContent = `事件 ${currentEvents.length} 条`;
    const eventHost = document.querySelector('#observer-events');
    if (eventHost) eventHost.replaceChildren(...currentEvents.map(rawCard));
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
