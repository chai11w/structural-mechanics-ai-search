/* Detached jobs: durable intent before POST; all recovery is read-only. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TikuBackgroundJobs = api;
})(globalThis, function () {
  'use strict';
  const PREFIX = 'tiku-agent-background-job-v1:';
  const ID = /^[0-9a-f]{32}$/;
  const KEY = /^[A-Za-z0-9:_.-]{8,128}$/;
  const KINDS = new Set(['handle_text', 'handle_image', 'select_unit', 'prepare_units', 'handle_crop']);
  const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED', 'UNKNOWN']);
  function failure(code, message, status = 0) {
    return Object.assign(new Error(message), { code, status, background: true });
  }
  function invalid() { return failure('RESPONSE_INVALID', '任务记录无法核验，请重新连接。'); }
  function validContext(context) {
    return context && ID.test(context.epoch) && Number.isSafeInteger(context.state_version) && context.state_version >= 0;
  }
  function parseRecord(raw) {
    let value;
    try { value = JSON.parse(raw); } catch (_) { throw invalid(); }
    if (!value || value.schema !== 1 || typeof value.key !== 'string' || !KEY.test(value.key) || !validContext(value)
        || Object.keys(value).sort().join(',') !== 'cursor,done,epoch,id,key,kind,schema,state_version'
        || !KINDS.has(value.kind) || (value.id !== '' && !ID.test(value.id))
        || !Number.isSafeInteger(value.cursor) || value.cursor < 0 || typeof value.done !== 'boolean') throw invalid();
    return value;
  }
  function command(path, options) {
    if (path === '/api/image/stream') {
      const image = options.body?.get('file');
      if (!(image instanceof Blob)) throw invalid();
      return { kind: 'handle_image', path: '/api/jobs/image', body: image };
    }
    const kinds = { '/api/message/stream': 'handle_text', '/api/a3/select/stream': 'select_unit',
      '/api/a3/prepare/stream': 'prepare_units', '/api/a3/crop/stream': 'handle_crop' };
    const kind = kinds[path];
    if (!kind) throw invalid();
    const parameters = JSON.parse(options.body);
    if (kind !== 'handle_text') {
      parameters.workflow_search_id = parameters.workflow_id;
      delete parameters.workflow_id;
    }
    return { kind, path: '/api/jobs', body: JSON.stringify({ kind, parameters }) };
  }
  function createClient(host) {
    const storage = host.storage;
    const observing = new Map();
    const sleep = host.sleep || (ms => new Promise(resolve => setTimeout(resolve, ms)));
    function records() {
      const result = [];
      for (let i = 0; i < storage.length; i++) {
        const key = storage.key(i);
        if (!key?.startsWith(PREFIX)) continue;
        const value = parseRecord(storage.getItem(key));
        if (key !== PREFIX + value.key) throw invalid();
        result.push(value);
      }
      if (result.length > 64) throw failure('STORAGE_FULL', '任务恢复记录已满，请先核对原任务。');
      return result.sort((left, right) => left.key.localeCompare(right.key));
    }
    function save(record) {
      const existing = storage.getItem(PREFIX + record.key);
      if (existing) {
        const old = parseRecord(existing);
        if (old.epoch !== record.epoch || old.kind !== record.kind || (old.id && record.id && old.id !== record.id)) throw invalid();
        record.id = record.id || old.id;
        record.cursor = Math.max(record.cursor, old.cursor);
        record.done = record.done || old.done;
      }
      // Whitelist metadata: never persist the upload, body, Cookie or grant.
      const value = { schema: 1, key: record.key, epoch: record.epoch, state_version: record.state_version,
        kind: record.kind, id: record.id, cursor: record.cursor, done: record.done };
      const raw = JSON.stringify(value);
      if (existing === raw) return;
      storage.setItem(PREFIX + value.key, raw);
      if (storage.getItem(PREFIX + value.key) !== raw) throw invalid();
    }
    async function http(path, options = {}) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 15000);
      try {
        const response = await host.fetch(path, { ...options, cache: 'no-store', credentials: 'same-origin', signal: controller.signal });
        const data = await response.json();
        if (!response.ok) throw failure(data.code || 'EXECUTION_UNAVAILABLE',
          response.status === 401 ? '登录已失效，请重新登录。' : '原任务暂时无法读取，请核对任务状态。', response.status);
        if (data.schema_version !== 1) throw invalid();
        return data;
      } finally { clearTimeout(timer); }
    }
    function accept(record, data) {
      const job = data?.job;
      if (data?.schema_version !== 1 || !job || !ID.test(job.operation_id)
          || (record.id && job.operation_id !== record.id) || job.kind !== record.kind
          || !['REGISTERED', 'RUNNING', ...TERMINAL].includes(job.status)
          || !Number.isSafeInteger(job.progress_version) || job.progress_version < 0) throw invalid();
      record.id = job.operation_id;
      record.cursor = Math.max(record.cursor, job.progress_version);
      save(record);
      return job;
    }
    function pending(epoch) { return records().filter(record => !record.done && record.epoch === epoch); }
    async function submit(path, options, context, fence, headers) {
      if (!host.locks?.request) throw failure('WEB_LOCK_REQUIRED', '此浏览器无法安全协调任务，请更换浏览器。');
      if (!validContext(context) || !KEY.test(fence?.id)) throw invalid();
      if (pending(context.epoch).length) throw failure('PENDING_JOB', '上次任务尚待确认，请重新连接查看原任务。');
      const value = command(path, options);
      // Caller owns the shared session Web Lock for this entire admission only.
      const bound = await http('/api/jobs/session', { method: 'POST', headers: { 'X-Tiku-Background': '1' } });
      if (!validContext(bound.execution) || bound.execution.epoch !== context.epoch
          || bound.execution.state_version !== context.state_version) throw failure('EXECUTION_STALE', '会话已更新，请重新连接。');
      const previous = records();
      // Completed records are only transport receipts; visible history owns its retention.
      for (const old of previous.filter(r => r.done).slice(0, Math.max(0, previous.length - 49))) storage.removeItem(PREFIX + old.key);
      if (records().length >= 64) throw failure('STORAGE_FULL', '任务恢复记录已满，请先核对原任务。');
      const record = { schema: 1, key: fence.id, ...context, kind: value.kind, id: '', cursor: 0, done: false };
      save(record); // Required before any business POST, including before ACK loss.
      const requestHeaders = new Headers(headers);
      requestHeaders.set('X-Tiku-Background', '1');
      requestHeaders.set('X-Tiku-Operation', JSON.stringify({ key: record.key, ...context }));
      requestHeaders.set('content-type', value.kind === 'handle_image' ? (value.body.type || 'application/octet-stream') : 'application/json');
      try {
        accept(record, await http(value.path, { method: 'POST', headers: requestHeaders, body: value.body }));
      } catch (_) {
        // Even a rejection may have followed admission. Keep intent and discover.
        // No automatic POST retry, no regenerated key, no retained private body.
      }
      return record;
    }
    async function query(record) {
      const path = record.id ? '/api/jobs/' + record.id
        : '/api/jobs/lookup?key=' + encodeURIComponent(record.key) + '&epoch=' + record.epoch;
      return accept(record, await http(path));
    }
    async function queueRecovery(operationId, context) {
      if (!ID.test(operationId) || !validContext(context)) throw invalid();
      // Only an explicitly acknowledged recovery calls this function. Persist
      // its read-only delivery before the control journal may be discarded.
      const data = await http('/api/jobs/' + operationId);
      if (!KINDS.has(data.job?.kind) || data.job.status !== 'SUCCEEDED') throw invalid();
      const key = 'recovery:' + operationId;
      const previous = records();
      if (!previous.some(record => record.key === key) && previous.length >= 64) {
        const old = previous.find(record => record.done);
        if (!old) throw failure('STORAGE_FULL', '任务恢复记录已满，请先核对原任务。');
        storage.removeItem(PREFIX + old.key);
      }
      const record = { schema: 1, key, ...context, kind: data.job.kind, id: operationId, cursor: 0, done: false };
      accept(record, data);
      return record;
    }
    async function observe(record, onProgress = () => {}) {
      if (observing.has(record.key)) return observing.get(record.key);
      const work = (async () => {
        let errors = 0;
        while (host.currentEpoch() === record.epoch) {
          let job;
          try {
            job = await query(record);
            errors = 0;
          } catch (error) {
            if ([401, 404, 409].includes(error.status) || error.code === 'RESPONSE_INVALID') throw error;
            onProgress({ message: '连接暂时中断，正在重新读取原任务…' });
            if (++errors >= 5) throw failure('OBSERVATION_INTERRUPTED', '连接暂时中断，请重新连接查看原任务。');
            await sleep(Math.min(1000 * 2 ** (errors - 1), 10000));
            continue;
          }
          if (host.currentEpoch() !== record.epoch) break;
          if (!['NOT_READY', 'PENDING', 'READY', 'FAILED', 'UNAVAILABLE'].includes(job.publication?.status)) throw invalid();
          if (TERMINAL.has(job.status)) {
            const publication = job.publication;
            if (job.status === 'SUCCEEDED' && !['READY', 'FAILED', 'UNAVAILABLE'].includes(publication?.status)) {
              onProgress({ message: '任务已处理，正在准备结果…' });
            } else {
              if (job.status === 'SUCCEEDED' && publication.status === 'READY') {
                const result = publication.result;
                if (!result || result.snapshot_role !== 'historical' || result.origin?.operation_id !== record.id
                    || result.origin?.epoch !== record.epoch || typeof result.response_id !== 'string' || !result.response_id) throw invalid();
              }
              await host.deliver(job, record);
              if (host.currentEpoch() !== record.epoch) break;
              record.done = true;
              save(record);
              return job;
            }
          } else onProgress({ message: job.status === 'REGISTERED' ? '任务已接收，正在排队…' : '任务正在后台处理…' });
          // Authoritative latest-snapshot polling is bounded and works with any
          // number of tabs; it does not consume the two NDJSON subscription slots.
          await sleep(1000);
        }
        throw failure('EXECUTION_STALE', '会话已更新，原任务结果不会写入当前对话。');
      })();
      observing.set(record.key, work);
      try { return await work; } finally { observing.delete(record.key); }
    }
    return { records, pending, submit, query, queueRecovery, observe, prefix: PREFIX };
  }
  return { createClient, command, parseRecord, prefix: PREFIX };
});
