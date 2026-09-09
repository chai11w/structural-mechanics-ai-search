/* Explicit phase-five controls. No timers, automatic recovery, or model replay. */
(function (root) {
  'use strict';
  const JOURNAL_KEY = 'tiku-agent-execution-command-v1';
  const LOCK_NAME = 'tiku-agent-execution-command-v1';
  const ACTIONS = new Set(['reset_session', 'stop_child', 'finish_page', 'recover_operation']);
  const TERMINAL_REJECTIONS = new Set([
    'EXECUTION_STALE', 'EXECUTION_INPUT_CONFLICT', 'EXECUTION_CONTROL_INVALID',
    'EXECUTION_RECOVERY_INVALID', 'EXECUTION_UNKNOWN', 'EXECUTION_RESULT_UNAVAILABLE',
    'EXECUTION_BUSY', 'EXECUTION_CAPACITY',
  ]);
  const fail = (text = '无法确认任务状态，请刷新任务状态后重试。') => new Error(text);
  const object = (value) => value && typeof value === 'object' && !Array.isArray(value);
  const exact = (value, keys) => object(value) && Object.keys(value).sort().join(',') === [...keys].sort().join(',');
  const id = (value) => typeof value === 'string' && value.length > 0 && value.length <= 128;
  const version = (value) => Number.isSafeInteger(value) && value >= 0;
  function validTarget(action, target) {
    if (!object(target)) return false;
    if (action === 'reset_session') return exact(target, []);
    if (action === 'recover_operation') return exact(target, ['source_operation_id']) && /^[0-9a-f]{32}$/.test(target.source_operation_id);
    if (!version(target.task_revision)) return false;
    if (action === 'finish_page') return exact(target, ['workflow_id', 'task_revision']) && id(target.workflow_id);
    return (exact(target, ['workflow_id', 'task_revision', 'unit_id']) && id(target.workflow_id) && id(target.unit_id))
      || (exact(target, ['task_id', 'task_revision']) && id(target.task_id));
  }
  function command(action, target) {
    if (action === 'reset_session') return { path: '/api/reset', body: {} };
    if (action === 'recover_operation') return { path: '/api/execution/recover', body: target };
    return { path: '/api/execution/control', body: { scope: action === 'stop_child' ? 'child' : 'workflow', target } };
  }
  function parseView(envelope, taskStateV1) {
    const model = taskStateV1.createTaskStateModelFromEnvelope(envelope);
    const view = envelope?.execution_control;
    const context = envelope?.execution;
    // The parent may be durably A2_ACTIVE before the child first saves. V1
    // correctly disables business actions in this window. Explicit controls
    // use their separate owned, version-bound offers and must remain usable.
    if (!model.available || !exact(view, ['schema', 'epoch', 'state_version', 'controls', 'pending', 'has_more'])
        || view.schema !== 1 || !/^[0-9a-f]{32}$/.test(view.epoch) || !version(view.state_version)
        || context?.schema !== 1 || context.epoch !== view.epoch || context.state_version !== view.state_version
        || !Array.isArray(view.controls) || view.controls.length > 23 || !Array.isArray(view.pending)
        || view.pending.length > 20 || typeof view.has_more !== 'boolean') throw fail();
    const seen = new Set();
    for (const offer of view.controls) {
      if (!exact(offer, ['action', 'target']) || !ACTIONS.has(offer.action) || !validTarget(offer.action, offer.target)) throw fail();
      const key = JSON.stringify(offer);
      if (seen.has(key)) throw fail();
      seen.add(key);
    }
    for (const item of view.pending) {
      if (!exact(item, ['operation_id', 'status', 'recovery']) || !/^[0-9a-f]{32}$/.test(item.operation_id)
          || !['REGISTERED', 'RUNNING', 'UNKNOWN'].includes(item.status)
          || !['WAIT', 'NO_RECEIPT', 'CHECK_RECEIPT', 'UNCONFIRMED_EFFECT'].includes(item.recovery)) throw fail();
    }
    // Every recovery offer must refer to an UNKNOWN record in this read-set.
    for (const offer of view.controls.filter((item) => item.action === 'recover_operation')) {
      if (!view.pending.some((item) => item.operation_id === offer.target.source_operation_id
          && item.status === 'UNKNOWN' && item.recovery === 'CHECK_RECEIPT')) throw fail();
    }
    const frozen = JSON.parse(JSON.stringify(view));
    frozen.controls.forEach((item) => { Object.freeze(item.target); Object.freeze(item); });
    frozen.pending.forEach(Object.freeze);
    Object.freeze(frozen.controls); Object.freeze(frozen.pending);
    return Object.freeze(frozen);
  }
  function createClient(host) {
    const views = new WeakSet();
    const storage = host.storage;
    let generation = 0;
    async function json(path, options = {}) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 15000);
      try {
        const response = await host.fetch(path, { ...options, signal: controller.signal, cache: 'no-store' });
        if (!(response.headers.get('content-type') || '').includes('application/json')) throw fail();
        return { response, data: await response.json() };
      } catch (error) {
        if (error instanceof TypeError || error.name === 'AbortError') {
          throw fail('连接中断或等待超时，请刷新任务状态；控制请求若已发出，请核对上次操作。');
        }
        throw error;
      } finally { clearTimeout(timer); }
    }
    function saved() {
      const raw = storage.getItem(JOURNAL_KEY);
      if (raw === null) return null;
      let entry;
      try { entry = JSON.parse(raw); } catch (_error) { throw fail('上次控制请求记录损坏，暂时不能发起新的控制操作。'); }
      if (!exact(entry, ['schema', 'action', 'target', 'operation', 'fence']) || entry.schema !== 1
          || !ACTIONS.has(entry.action) || !validTarget(entry.action, entry.target)
          || !exact(entry.operation, ['key', 'epoch', 'state_version'])
          || !/^[A-Za-z0-9:_.-]{8,128}$/.test(entry.operation.key)
          || !/^[0-9a-f]{32}$/.test(entry.operation.epoch) || !version(entry.operation.state_version)
          || !host.validFence(entry.fence) || entry.fence.id !== entry.operation.key) throw fail();
      return entry;
    }
    async function inspect() {
      const mine = ++generation;
      const { response, data } = await json('/api/execution');
      if (!response.ok || mine !== generation) throw fail();
      const view = parseView(data, host.taskStateV1);
      views.add(view);
      return { view, envelope: data };
    }
    async function locked(callback) {
      if (!host.locks?.request) throw fail('当前浏览器不支持安全任务控制，请使用新版 Chrome 或 Edge。');
      return host.locks.request(LOCK_NAME, { mode: 'exclusive', ifAvailable: true }, async (lock) => {
        if (!lock) throw fail('另一页面正在提交控制操作，请稍后刷新任务状态。');
        return callback();
      });
    }
    function erase(entry, ownOnly = false) {
      host.clearFence(entry.fence, ownOnly);
      const raw = storage.getItem(JOURNAL_KEY);
      if (raw !== JSON.stringify(entry)) throw fail();
      storage.removeItem(JOURNAL_KEY);
      if (storage.getItem(JOURNAL_KEY) !== null) throw fail();
    }
    async function send(entry) {
      const spec = command(entry.action, entry.target);
      const headers = host.headers(entry.fence);
      headers.set('Content-Type', 'application/json');
      headers.set('X-Tiku-Operation', JSON.stringify(entry.operation));
      host.retire();
      const { response, data } = await json(spec.path, { method: 'POST', headers, body: JSON.stringify(spec.body) });
      if (!response.ok) {
        // These command errors roll the SQLite command transaction back.
        // Never retire inherited business fences on a rejected command.
        if (response.status === 409 && TERMINAL_REJECTIONS.has(data?.code)) erase(entry, true);
        throw fail(data?.code === 'EXECUTION_STALE' ? '任务已更新，请刷新任务状态后重新选择操作。'
          : '此次操作未完成；请刷新任务状态核对，不会自动重新识别。');
      }
      const model = host.taskStateV1.createTaskStateModelFromEnvelope(data);
      if (!model.available || !model.consistent || !host.acknowledged(data, entry.fence)) throw fail();
      if (entry.action !== 'reset_session' && (data.operation?.status !== 'SUCCEEDED'
          || !/^[0-9a-f]{32}$/.test(data.operation.operation_id))) throw fail();
      erase(entry);
      host.publish();
      // A lost response can be replayed after a later state change. Only a new
      // read may authorize current UI actions; the historical receipt cannot.
      const current = await inspect();
      host.committed(data, current.envelope, entry.action);
      return current;
    }
    return Object.freeze({
      inspect,
      hasPending() { return saved() !== null; },
      execute(view, index) {
        return locked(async () => {
          if (!views.has(view) || !Number.isSafeInteger(index) || !view.controls[index]) throw fail();
          if (saved()) throw fail('上次控制请求尚未确认，请先核对上次操作。');
          const offer = view.controls[index];
          const fence = host.createFence();
          if (!host.validFence(fence)) throw fail();
          const entry = { schema: 1, action: offer.action, target: offer.target,
            operation: { key: fence.id, epoch: view.epoch, state_version: view.state_version }, fence };
          const raw = JSON.stringify(entry);
          storage.setItem(JOURNAL_KEY, raw);
          if (storage.getItem(JOURNAL_KEY) !== raw) throw fail();
          views.delete(view);
          return send(entry);
        });
      },
      retry() { return locked(() => { const entry = saved(); if (!entry) throw fail(); return send(entry); }); },
    });
  }
  const api = Object.freeze({ createClient, parseView, JOURNAL_KEY });
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TikuExecutionControl = api;
}(typeof globalThis === 'object' ? globalThis : this));
