const assert = require('node:assert/strict');
const api = require('../tiku_agent/demo_web/background_jobs.js');
const epoch = 'a'.repeat(32), id = 'b'.repeat(32);
function fixture() {
  const entries = new Map(), calls = [], delivered = [];
  let mode = 'ready', current = epoch, postError = false, status = 'SUCCEEDED';
  const storage = { get length() { return entries.size; }, key: i => [...entries.keys()][i],
    getItem: k => entries.get(k) ?? null, setItem: (k,v) => entries.set(k,v), removeItem: k => entries.delete(k) };
  const context = { epoch, state_version: 4 };
  const result = { origin: { epoch, operation_id: id }, response_id: 'response-stable', snapshot_role: 'historical' };
  const job = () => ({ operation_id: id, kind: 'handle_text', status, progress_version: 3,
    publication: { status: 'READY', result } });
  const host = { storage, locks: { request() {} }, currentEpoch: () => current,
    sleep: async () => {}, deliver: async (value, record) => delivered.push({ value, record }),
    fetch: async (path, options) => {
      calls.push({ path, method: options.method || 'GET' });
      assert.equal(options.cache, 'no-store');
      if (path === '/api/jobs/session') return Response.json({ schema_version: 1, execution: context });
      if (options.method === 'POST') {
        assert.equal(entries.size, 1, 'intent durable before POST');
        assert(![...entries.values()][0].includes('private body'), 'body not persisted');
        if (postError) throw new TypeError('lost ACK');
        return Response.json({ schema_version: 1, job: job() }, { status: 202 });
      }
      if (mode === 'offline') throw new TypeError('offline');
      if (mode === 'missing') return Response.json({ code: 'EXECUTION_NOT_FOUND' }, { status: 404 });
      if (mode === 'revoked') return Response.json({ code: 'EXECUTION_AUTH_REQUIRED' }, { status: 401 });
      return Response.json({ schema_version: 1, job: job() });
    } };
  return { host, context, calls, delivered, entries, result, job,
    mode: value => { mode = value; }, epoch: value => { current = value; },
    status: value => { status = value; }, loseAck: () => { postError = true; } };
}
const options = { body: JSON.stringify({ text: 'private body' }) };
const fence = { id: '12345678:original-key' };
const diagnosticKey = 'tiku-agent-bind-diagnostics-v1';
const diagnosticFields = ['code', 'elapsed_ms', 'phase', 'request_id', 'schema', 'status'];
function diagnosticFixture() {
  const f = fixture(), saved = new Map();
  let sequence = 0, clock = 100;
  f.host.diagnosticStorage = { getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value) };
  f.host.newRequestId = () => 'req_' + (++sequence).toString(16).padStart(32, '0');
  f.host.clock = () => (clock += 7.4);
  return Object.assign(f, { saved, diagnostic: () => JSON.parse(saved.get(diagnosticKey)) });
}
async function submit(f, client) {
  return client.submit('/api/message/stream', options, f.context, fence, new Headers());
}
async function run() {
  let count = 0;
  async function test(name, action) { await action(); count++; console.log('PASS ' + name); }
  await test('explicit admission rejection releases intent and allows a manual retry', async () => {
    for (const [code, status] of [['EXECUTION_QUEUE_FULL', 429], ['EXECUTION_INPUT_INVALID', 409],
      ['EXECUTION_INPUT_TOO_LARGE', 413], ['EXECUTION_CAPACITY', 503], ['EXECUTION_COST_PENDING', 409],
      ['INVITE_DAILY_QUOTA_EXCEEDED', 409], ['GLOBAL_DAILY_QUOTA_EXCEEDED', 409]]) {
      const f = fixture(), client = api.createClient(f.host), original = f.host.fetch;
      let posts = 0;
      f.host.fetch = async (path, options) => {
        if (path !== '/api/jobs') return original(path, options);
        posts++;
        return Response.json({schema_version:1, code, admission:'rejected', message:'private provider text'}, {status});
      };
      await assert.rejects(submit(f, client), e => e.admissionRejected && e.code === code && !e.message.includes('private'));
      assert.equal(client.pending(epoch).length, 0);
      assert.equal(posts, 1, 'no automatic resubmission');
      f.host.fetch = original;
      await client.observe(await submit(f, client));
      assert.equal(f.delivered.length, 1);
    }
  });
  await test('unmarked or inconsistent rejection never discards uncertain admission', async () => {
    for (const [data,status] of [
      [{schema_version:1,code:'EXECUTION_QUEUE_FULL'},429],
      [{schema_version:2,code:'EXECUTION_QUEUE_FULL',admission:'rejected'},429],
      [{schema_version:1,code:'EXECUTION_QUEUE_FULL',admission:'rejected'},503],
      [{schema_version:1,code:'EXECUTION_UNAVAILABLE',admission:'rejected'},503],
    ]) {
      const f = fixture(), client = api.createClient(f.host), original = f.host.fetch;
      f.host.fetch = (path, options) => path === '/api/jobs' ? Response.json(data, {status}) : original(path, options);
      const record = await submit(f, client);
      assert.equal(record.id, ''); assert.equal(client.pending(epoch).length, 1);
      await assert.rejects(submit(f, client), e => e.code === 'PENDING_JOB');
    }
  });
  function retired(f, count = 64) {
    const records = [];
    for (let i = 0; i < count; i++) {
      const record = {schema:1,key:'retired-'+String(i).padStart(4,'0'),epoch:'c'.repeat(32),
        state_version:0,kind:'handle_text',id,cursor:0,done:false};
      f.entries.set(api.prefix+record.key, JSON.stringify(record)); records.push(record);
    }
    return records;
  }
  await test('fresh binding retires 64 old epoch receipts without reviving late observers', async () => {
    const f = fixture(), client = api.createClient(f.host), old = retired(f);
    let release;
    const original = f.host.fetch;
    f.host.fetch = async (path, options) => path === '/api/jobs/'+id
      ? new Promise(resolve => { release = () => resolve(Response.json({schema_version:1,job:f.job()})); })
      : original(path, options);
    const late = client.query(old[0]);
    const rejection = assert.rejects(late, e => e.code === 'EXECUTION_STALE');
    const current = await submit(f, client);
    release(); await rejection;
    assert.equal(client.records().length, 1);
    assert.equal(client.records()[0].epoch, epoch);
    f.host.fetch = original;
    await client.observe(current);
    assert.equal(f.delivered.length, 1);
  });
  await test('stale or unauthorized binding cannot erase another epochs recovery records', async () => {
    for (const unauthorized of [false,true]) {
      const f = fixture(), client = api.createClient(f.host); retired(f);
      const before = [...f.entries];
      f.host.fetch = async path => {
        assert.equal(path, '/api/jobs/session');
        return unauthorized ? Response.json({code:'EXECUTION_AUTH_REQUIRED'},{status:401})
          : Response.json({schema_version:1,execution:{epoch:'d'.repeat(32),state_version:0}});
      };
      await assert.rejects(submit(f, client));
      assert.deepEqual([...f.entries], before);
    }
  });
  await test('current unresolved receipt remains protected when old records fill capacity', async () => {
    const f = fixture(), client = api.createClient(f.host);
    retired(f,63); const record={schema:1,key:'current-pending',epoch,state_version:4,kind:'handle_text',id:'',cursor:0,done:false};
    f.entries.set(api.prefix+record.key,JSON.stringify(record));
    await assert.rejects(submit(f,client), e=>e.code==='PENDING_JOB');
    assert.equal(f.entries.size,64); assert.equal(f.calls.length,0);
  });
  await test('failed rejection cleanup remains visible instead of authorizing retry', async () => {
    const f=fixture(), client=api.createClient(f.host), original=f.host.fetch;
    f.host.storage.removeItem=()=>{};
    f.host.fetch=(path,options)=>path==='/api/jobs'
      ? Response.json({schema_version:1,code:'EXECUTION_QUEUE_FULL',admission:'rejected'},{status:429}) : original(path,options);
    await assert.rejects(submit(f,client),e=>e.code==='RESPONSE_INVALID' && !e.admissionRejected);
    assert.equal(client.pending(epoch).length,1);
  });
  await test('original progress changes by step and counter without another POST', async () => {
    const f = fixture(), client = api.createClient(f.host), record = await submit(f, client);
    const messages = ['正在理解整页题目和图形关系…', '已完成 1/2 张自动裁图校验…', '已完成 2/2 张自动裁图校验…'];
    let index = 0;
    f.host.fetch = async (_path, options) => {
      assert.notEqual(options.method, 'POST');
      const job = f.job();
      if (index < messages.length) {
        job.status = 'RUNNING'; job.publication = {status:'NOT_READY'};
        job.progress = {type:'progress', stage:index ? 'a3_auto_validating' : 'a3_understanding', message:messages[index++]};
      }
      return Response.json({schema_version:1,job});
    };
    const seen = [];
    await client.observe(record, event => seen.push(event.message));
    assert.deepEqual(seen, messages);
    assert.equal(f.delivered.length, 1);
  });
  await test('ACK loss -> fresh client original-key discovery, one POST', async () => {
    const f = fixture(); f.loseAck();
    await submit(f, api.createClient(f.host));
    const resumed = api.createClient(f.host), record = resumed.pending(epoch)[0];
    assert.equal(record.id, '');
    await resumed.observe(record);
    assert(f.calls.some(c => c.path.includes('/lookup?key=12345678%3Aoriginal-key')));
    assert.equal(f.calls.filter(c => c.path === '/api/jobs').length, 1);
    assert.equal(resumed.pending(epoch).length, 0);
  });
  await test('offline recovery preserves pending and never resends', async () => {
    const f = fixture(), client = api.createClient(f.host), record = await submit(f, client);
    f.mode('offline'); await assert.rejects(client.observe(record), /重新连接/);
    assert.equal(client.pending(epoch).length, 1);
    f.mode('ready'); await client.observe(record);
    assert.equal(f.calls.filter(c => c.path === '/api/jobs').length, 1);
  });
  await test('not found after lost ACK cannot silently create another operation', async () => {
    const f = fixture(); f.loseAck(); const client = api.createClient(f.host);
    const record = await submit(f, client); f.mode('missing');
    await assert.rejects(client.observe(record), e => e.code === 'EXECUTION_NOT_FOUND');
    await assert.rejects(submit(f, client), e => e.code === 'PENDING_JOB');
    assert.equal(client.pending(epoch).length, 1);
  });
  await test('login revoked: stop observation, retain receipt, zero replay', async () => {
    const f = fixture(), client = api.createClient(f.host), record = await submit(f, client);
    f.mode('revoked'); await assert.rejects(client.observe(record), e => e.status === 401);
    assert.equal(f.delivered.length, 0); assert.equal(client.pending(epoch).length, 1);
  });
  await test('epoch switch fences late results', async () => {
    const f = fixture(), client = api.createClient(f.host), record = await submit(f, client);
    f.epoch('c'.repeat(32)); await assert.rejects(client.observe(record), e => e.code === 'EXECUTION_STALE');
    assert.equal(f.delivered.length, 0);
  });
  await test('mismatched frozen result fails closed', async () => {
    const f = fixture(), client = api.createClient(f.host), record = await submit(f, client);
    f.result.origin.operation_id = 'c'.repeat(32);
    await assert.rejects(client.observe(record), e => e.code === 'RESPONSE_INVALID');
    assert.equal(f.delivered.length, 0);
  });
  await test('storage failure prevents all business POSTs', async () => {
    const f = fixture(); f.host.storage.setItem = () => { throw new Error('full'); };
    await assert.rejects(submit(f, api.createClient(f.host)));
    assert.equal(f.calls.filter(c => c.path === '/api/jobs').length, 0);
  });
  await test('Web Lock absence refuses submission', async () => {
    const f = fixture(); f.host.locks = null;
    await assert.rejects(submit(f, api.createClient(f.host)), e => e.code === 'WEB_LOCK_REQUIRED');
    assert.equal(f.calls.length, 0);
  });
  await test('delivery must persist before receipt is marked done', async () => {
    const f = fixture(), client = api.createClient(f.host), record = await submit(f, client);
    f.host.deliver = async () => { throw new Error('history disk full'); };
    await assert.rejects(client.observe(record)); assert.equal(client.pending(epoch).length, 1);
  });
  await test('two observers share one in-tab observation and monotone record', async () => {
    const f = fixture(), client = api.createClient(f.host), record = await submit(f, client);
    await Promise.all([client.observe(record), client.observe(record)]);
    assert.equal(f.delivered.length, 1);
    const old = { ...record, done: false, cursor: 0 };
    await api.createClient(f.host).query(old);
    assert.equal(client.records()[0].done, true); assert.equal(client.records()[0].cursor, 3);
  });
  await test('UNKNOWN is a terminal receipt, never a retry', async () => {
    const f = fixture(), client = api.createClient(f.host), record = await submit(f, client);
    f.status('UNKNOWN'); await client.observe(record);
    assert.equal(f.delivered[0].value.status, 'UNKNOWN');
    assert.equal(f.calls.filter(c => c.path === '/api/jobs').length, 1);
  });
  await test('all five business commands preserve targets and raw upload', async () => {
    for (const [path,kind] of [['select','select_unit'],['prepare','prepare_units'],['crop','handle_crop']]) {
      const value = api.command('/api/a3/'+path+'/stream', { body: JSON.stringify({ workflow_id:'workflow', task_revision:7, unit_id:'u1' }) });
      const body = JSON.parse(value.body);
      assert.equal(body.kind, kind); assert.equal(body.parameters.workflow_search_id, 'workflow');
      assert.equal(body.parameters.task_revision, 7); assert.equal(body.parameters.workflow_id, undefined);
    }
    const data = new FormData(); data.append('file', new Blob(['image'],{type:'image/png'}),'test.png');
    const value = api.command('/api/image/stream', { body:data });
    assert.equal(value.kind,'handle_image'); assert.equal(await value.body.text(),'image');
  });
  await test('explicit recovery survives reload and observes original job without POST', async () => {
    const f = fixture(), client = api.createClient(f.host), original = await submit(f, client);
    f.status('UNKNOWN'); await client.observe(original);
    f.status('SUCCEEDED'); await client.queueRecovery(id, f.context);
    const restored = api.createClient(f.host);
    assert.equal(restored.pending(epoch).length, 1);
    await restored.observe(restored.pending(epoch)[0]);
    assert.equal(f.delivered[1].value.publication.result.response_id, 'response-stable');
    assert.equal(f.calls.filter(c => c.path === '/api/jobs').length, 1);
    assert.equal(restored.pending(epoch).length, 0);
  });
  await test('recovery without an old receipt remains read-only and storage failure is visible', async () => {
    const f = fixture(), client = api.createClient(f.host);
    const record = await client.queueRecovery(id, f.context);
    await client.observe(record);
    assert.equal(f.calls.filter(c => c.method === 'POST').length, 0);
    const broken = fixture(); broken.host.storage.setItem = () => { throw Error('full'); };
    await assert.rejects(api.createClient(broken.host).queueRecovery(id, broken.context), /full/);
    assert.equal(broken.delivered.length, 0);
  });
  await test('session-binding abort reconnects once before the only business POST', async () => {
    const f = fixture(), original = f.host.fetch;
    let bindings = 0;
    f.host.fetch = async (path, options) => {
      if (path === '/api/jobs/session' && ++bindings === 1) throw new DOMException('signal is aborted without reason', 'AbortError');
      return original(path, options);
    };
    const client = api.createClient(f.host), record = await submit(f, client);
    await client.observe(record);
    assert.equal(bindings, 2);
    assert.equal(f.calls.filter(c => c.path === '/api/jobs').length, 1);
    assert.equal(f.delivered.length, 1);
  });
  await test('persistent binding failure is Chinese and proves no crop submission', async () => {
    for (const error of [new DOMException('signal is aborted without reason', 'AbortError'), new TypeError('Failed to fetch')]) {
      const f = fixture(); let bindings = 0;
      f.host.fetch = async path => { assert.equal(path, '/api/jobs/session'); bindings++; throw error; };
      await assert.rejects(submit(f, api.createClient(f.host)), e => e.submissionNotSent === true
        && e.publicMessage === e.message && /连接/.test(e.message) && !/aborted|fetch/.test(e.message));
      assert.equal(bindings, 2);
      assert.equal(f.entries.size, 0);
    }
  });
  await test('lost business response never repeats the POST and observes original result', async () => {
    const f = fixture(), original = f.host.fetch; let posts = 0;
    f.host.fetch = async (path, options) => {
      const response = await original(path, options);
      if (path === '/api/jobs') { posts++; throw new DOMException('signal is aborted without reason', 'AbortError'); }
      return response;
    };
    const client = api.createClient(f.host), record = await submit(f, client);
    assert.equal(record.id, '');
    await client.observe(record);
    assert.equal(posts, 1);
    assert.equal(f.delivered.length, 1);
  });
  await test('invalid JSON uses a registered message without retrying session binding', async () => {
    const f = fixture(); let bindings = 0;
    f.host.fetch = async () => { bindings++; return new Response('<html>proxy failure</html>'); };
    await assert.rejects(submit(f, api.createClient(f.host)), e => e.code === 'RESPONSE_INVALID'
      && e.submissionNotSent && !e.message.includes('JSON'));
    assert.equal(bindings, 1);
  });
  await test('JSON non-object responses never masquerade as a lost connection', async () => {
    for (const status of [200, 503]) {
      for (const data of [null, [], 0, true, 'invalid']) {
        const f = fixture(), progress = []; let bindings = 0;
        f.host.fetch = async path => {
          assert.equal(path, '/api/jobs/session'); bindings++;
          return Response.json(data, { status });
        };
        await assert.rejects(api.createClient(f.host).submit('/api/message/stream', options,
          f.context, fence, new Headers(), event => progress.push(event)),
        error => error.code === 'RESPONSE_INVALID' && error.submissionNotSent === true);
        assert.equal(bindings, 1);
        assert.deepEqual(progress, []);
        assert.equal(f.entries.size, 0);
      }
    }
  });
  await test('body transfer failure can reconnect but invalid JSON and HTTP errors cannot', async () => {
    for (const [response, code, attempts] of [
      [() => ({ ok: true, json: async () => { throw new TypeError('body transfer failed'); } }), 'NETWORK_UNAVAILABLE', 2],
      [() => ({ ok: true, json: async () => { throw new SyntaxError('invalid JSON'); } }), 'RESPONSE_INVALID', 1],
      [() => Response.json({ schema_version: 1, code: 'EXECUTION_UNAVAILABLE' }, { status: 502 }), 'EXECUTION_UNAVAILABLE', 1],
      [() => new Response('<html>upstream failure</html>', { status: 502 }), 'RESPONSE_INVALID', 1],
      [() => new Response('<html>login</html>'), 'RESPONSE_INVALID', 1],
    ]) {
      const f = fixture(); let bindings = 0;
      f.host.fetch = async path => { assert.equal(path, '/api/jobs/session'); bindings++; return response(); };
      await assert.rejects(submit(f, api.createClient(f.host)), error => error.code === code && error.submissionNotSent);
      assert.equal(bindings, attempts);
      assert.equal(f.entries.size, 0);
    }
  });
  await test('successive question selections bind and submit once with one isolated binding retry', async () => {
    const f = fixture(), client = api.createClient(f.host), jobs = new Map(), counts = [];
    const progress = [];
    let selection = 0, current;
    f.host.fetch = async (path, request) => {
      assert.equal(request.credentials, 'same-origin');
      assert.equal(request.cache, 'no-store');
      const count = counts[selection];
      if (path === '/api/jobs/session') {
        count.bindings++;
        if (selection === 1 && count.bindings === 1) throw new TypeError('controlled binding loss');
        return Response.json({ schema_version: 1, execution: current });
      }
      if (path === '/api/jobs') {
        count.posts++;
        const operation = JSON.parse(request.headers.get('X-Tiku-Operation'));
        const command = JSON.parse(request.body);
        assert.equal(operation.state_version, current.state_version);
        assert.equal(command.kind, 'select_unit');
        assert.equal(command.parameters.unit_id, 'unit-' + selection);
        assert.equal(command.parameters.workflow_search_id, 'workflow-fixture');
        assert.equal(f.entries.size, selection + 1, 'new intent is saved before submission');
        const operationId = (selection + 1).toString(16).padStart(32, '0');
        const job = { operation_id: operationId, kind: 'select_unit', status: 'SUCCEEDED', progress_version: 1,
          publication: { status: 'READY', result: { snapshot_role: 'historical',
            origin: { operation_id: operationId, epoch }, response_id: 'selection-' + selection } } };
        jobs.set(operationId, job);
        return Response.json({ schema_version: 1, job }, { status: 202 });
      }
      const job = jobs.get(path.slice('/api/jobs/'.length));
      assert(job, 'observation must refer to an already submitted operation');
      count.reads++;
      return Response.json({ schema_version: 1, job });
    };
    for (selection = 0; selection < 3; selection++) {
      current = { epoch, state_version: 4 + selection };
      counts.push({ bindings: 0, posts: 0, reads: 0 });
      const record = await client.submit('/api/a3/select/stream', {
        body: JSON.stringify({ workflow_id: 'workflow-fixture', unit_id: 'unit-' + selection, task_revision: selection }),
      }, current, { id: 'selection-fixture-' + selection }, new Headers(), event => progress.push(event));
      await client.observe(record);
      assert.equal(client.pending(epoch).length, 0);
      assert.equal(f.delivered.length, selection + 1);
    }
    assert.deepEqual(counts, [{ bindings: 1, posts: 1, reads: 1 },
      { bindings: 2, posts: 1, reads: 1 }, { bindings: 1, posts: 1, reads: 1 }]);
    assert.equal(progress.length, 1);
    assert.equal(client.records().length, 3);
    assert(client.records().every(record => record.done));
  });
  await test('two failed selection bindings leave no submitted intent or delivery', async () => {
    const f = fixture(), progress = [];
    let bindings = 0;
    f.host.fetch = async path => {
      assert.equal(path, '/api/jobs/session'); bindings++;
      throw new DOMException('controlled timeout', 'TimeoutError');
    };
    await assert.rejects(api.createClient(f.host).submit('/api/a3/select/stream', {
      body: JSON.stringify({ workflow_id: 'workflow-fixture', unit_id: 'unit-2', task_revision: 2 }),
    }, f.context, fence, new Headers(), event => progress.push(event)),
    error => error.code === 'REQUEST_TIMEOUT' && error.submissionNotSent === true);
    assert.equal(bindings, 2);
    assert.equal(progress.length, 1);
    assert.equal(f.entries.size, 0);
    assert.equal(f.delivered.length, 0);
  });
  await test('binding diagnostics distinguish fetch, body and protocol without leaking payloads', async () => {
    const privateText = 'private-body https://private.invalid/image?token=secret Cookie=image-data';
    const cases = [
      [async () => { throw new TypeError(privateText); }, 'fetch', 'NETWORK_UNAVAILABLE', 0, 2],
      [async () => { throw new DOMException(privateText, 'AbortError'); }, 'fetch', 'REQUEST_TIMEOUT', 0, 2],
      [async () => ({ status: 200, ok: true, json: async () => { throw new TypeError(privateText); } }), 'body', 'NETWORK_UNAVAILABLE', 200, 2],
      [async () => ({ status: 200, ok: true, json: async () => { throw new SyntaxError(privateText); } }), 'body', 'RESPONSE_INVALID', 200, 1],
      [async () => Response.json(null, { status: 503 }), 'protocol', 'RESPONSE_INVALID', 503, 1],
      [async () => Response.json({ schema_version: 1, code: 'EXECUTION_AUTH_REQUIRED', message: privateText }, { status: 401 }), 'protocol', 'HTTP_ERROR', 401, 1],
    ];
    for (const [response, phase, code, status, attempts] of cases) {
      const f = diagnosticFixture(), seen = [];
      f.host.fetch = async (path, request) => {
        assert.equal(path, '/api/jobs/session');
        seen.push(request.headers);
        return response();
      };
      await assert.rejects(submit(f, api.createClient(f.host)), error => {
        assert.equal(error.submissionNotSent, true);
        assert.equal(error.code, code === 'HTTP_ERROR' ? 'EXECUTION_AUTH_REQUIRED' : code);
        assert.deepEqual(error.transportDiagnostic, f.diagnostic().pending);
        return true;
      });
      const saved = f.diagnostic();
      assert.equal(seen.length, attempts);
      assert.equal(saved.entries.length, attempts);
      assert.equal(seen[0].get('X-Tiku-Bind-Diagnostic'), null);
      for (let index = 0; index < attempts; index++) {
        const entry = saved.entries[index];
        assert.deepEqual(Object.keys(entry).sort(), diagnosticFields);
        assert.deepEqual(entry, { schema: 1, request_id: seen[index].get('X-Request-ID'), phase, code, elapsed_ms: 7, status });
        assert.match(entry.request_id, /^req_[0-9a-f]{32}$/);
        if (index) {
          assert.notEqual(entry.request_id, saved.entries[index - 1].request_id);
          assert.deepEqual(JSON.parse(seen[index].get('X-Tiku-Bind-Diagnostic')), saved.entries[index - 1]);
        }
      }
      assert(!JSON.stringify(saved).includes(privateText));
      assert(!JSON.stringify(saved).includes('private'));
      assert.equal(f.entries.size, 0, 'diagnostic storage is independent of business receipts');
    }
  });
  await test('one recovered binding reports its failure once and leaves business headers untouched', async () => {
    const f = diagnosticFixture(), original = f.host.fetch, headers = [];
    f.host.fetch = async (path, request) => {
      headers.push({ path, value: new Headers(request.headers) });
      if (headers.length === 1) throw new TypeError('private exception text');
      return original(path, request);
    };
    const client = api.createClient(f.host);
    await client.observe(await submit(f, client));
    assert.equal(headers.length, 4, 'two binds, one business POST and one observation');
    const carried = headers[1].value.get('X-Tiku-Bind-Diagnostic');
    assert(carried.length <= 512 && /^[\x20-\x7e]+$/.test(carried));
    assert.deepEqual(JSON.parse(carried), f.diagnostic().entries[0]);
    assert.notEqual(headers[0].value.get('X-Request-ID'), headers[1].value.get('X-Request-ID'));
    for (const entry of headers.slice(2)) {
      assert.equal(entry.value.get('X-Request-ID'), null);
      assert.equal(entry.value.get('X-Tiku-Bind-Diagnostic'), null);
    }
    assert.equal(f.diagnostic().pending, null);
    assert.equal(f.diagnostic().entries.length, 1);
    assert.equal(f.delivered.length, 1);
    assert(![...f.saved.values()].join('').includes('private'));
  });
  await test('reload carries the latest failed binding and success keeps history but clears pending', async () => {
    const f = diagnosticFixture(), original = f.host.fetch, ids = [];
    f.host.fetch = async (_path, request) => {
      ids.push(request.headers.get('X-Request-ID'));
      throw new TypeError('offline');
    };
    await assert.rejects(submit(f, api.createClient(f.host)), error => error.submissionNotSent);
    const pending = f.diagnostic().pending;
    let carried;
    f.host.fetch = async (path, request) => {
      if (path === '/api/jobs/session') {
        ids.push(request.headers.get('X-Request-ID'));
        carried = request.headers.get('X-Tiku-Bind-Diagnostic');
      }
      return original(path, request);
    };
    const reloaded = api.createClient(f.host);
    await reloaded.observe(await submit(f, reloaded));
    assert.deepEqual(JSON.parse(carried), pending);
    assert.equal(new Set(ids).size, 3);
    assert.equal(f.diagnostic().pending, null);
    assert.equal(f.diagnostic().entries.length, 2);
    assert.equal(f.calls.filter(call => call.path === '/api/jobs').length, 1);
  });
  await test('diagnostic history has a fixed capacity while repeated failed submissions remain unsubmitted', async () => {
    const f = diagnosticFixture(); let bindings = 0;
    f.host.fetch = async path => { assert.equal(path, '/api/jobs/session'); bindings++; throw new TypeError('offline'); };
    const client = api.createClient(f.host);
    for (let attempt = 0; attempt < 10; attempt++) await assert.rejects(submit(f, client));
    const saved = f.diagnostic();
    assert.equal(bindings, 20);
    assert.equal(saved.entries.length, 16);
    assert.equal(saved.entries[0].request_id, 'req_' + (5).toString(16).padStart(32, '0'));
    assert.deepEqual(saved.pending, saved.entries.at(-1));
    assert([...f.saved.values()][0].length <= 8192);
    assert.deepEqual([...f.saved.keys()], [diagnosticKey]);
    assert.equal(f.entries.size, 0);
  });
  await test('corrupt, oversized and non-whitelisted stored diagnostics fail open without forwarding', async () => {
    const valid = { schema: 1, request_id: 'req_' + 'f'.repeat(32), phase: 'fetch', code: 'NETWORK_UNAVAILABLE', elapsed_ms: 1, status: 0 };
    const envelope = entry => JSON.stringify({ schema: 1, entries: [entry], pending: entry });
    for (const raw of ['{', 'x'.repeat(8193), 'null', '[]',
      envelope({ ...valid, secret: 'must-not-forward' }), envelope({ ...valid, request_id: 'foreign-id' }),
      envelope({ ...valid, phase: 'private-phase' }), envelope({ ...valid, elapsed_ms: 120001 }),
      envelope({ ...valid, status: 600 }), envelope({ ...valid, code: 'UNREGISTERED_CODE' }),
      JSON.stringify({ schema: 1, entries: Array(17).fill(valid), pending: valid }),
      JSON.stringify({ schema: 1, entries: [], pending: valid }),
      JSON.stringify({ schema: 1, entries: [valid], pending: valid, body: 'must-not-forward' }),
    ]) {
      const f = diagnosticFixture(), original = f.host.fetch;
      f.saved.set(diagnosticKey, raw);
      f.host.fetch = async (path, request) => {
        assert.equal(new Headers(request.headers).get('X-Tiku-Bind-Diagnostic'), null);
        return original(path, request);
      };
      const client = api.createClient(f.host);
      await client.observe(await submit(f, client));
      assert.equal(f.delivered.length, 1);
      assert.equal(f.calls.filter(call => call.path === '/api/jobs').length, 1);
    }
  });
  await test('unavailable diagnostic storage still forwards an in-memory retry and allows one business POST', async () => {
    for (const mode of ['absent', 'read-error', 'write-error', 'getter-error']) {
      const f = diagnosticFixture(), original = f.host.fetch;
      if (mode === 'absent') delete f.host.diagnosticStorage;
      if (mode === 'read-error') f.host.diagnosticStorage.getItem = () => { throw new Error('storage denied'); };
      if (mode === 'write-error') f.host.diagnosticStorage.setItem = () => { throw new Error('quota exceeded'); };
      if (mode === 'getter-error') Object.defineProperty(f.host, 'diagnosticStorage', { get() { throw new Error('denied getter'); } });
      let attempts = 0, carried;
      f.host.fetch = async (path, request) => {
        if (path === '/api/jobs/session') {
          if (++attempts === 1) throw new TypeError('offline');
          carried = request.headers.get('X-Tiku-Bind-Diagnostic');
        }
        return original(path, request);
      };
      const client = api.createClient(f.host);
      await client.observe(await submit(f, client));
      assert.equal(attempts, 2);
      assert.equal(JSON.parse(carried).code, 'NETWORK_UNAVAILABLE');
      assert.equal(f.calls.filter(call => call.path === '/api/jobs').length, 1);
      assert.equal(f.delivered.length, 1);
    }
  });
  await test('request-ID generation failures degrade diagnostics without changing binding recovery', async () => {
    for (const generate of [() => { throw new Error('no crypto'); }, () => 'req_INVALID', () => null]) {
      const f = diagnosticFixture(), original = f.host.fetch;
      f.host.newRequestId = generate;
      let attempts = 0;
      f.host.fetch = async (path, request) => {
        assert.equal(new Headers(request.headers).get('X-Request-ID'), null);
        assert.equal(new Headers(request.headers).get('X-Tiku-Bind-Diagnostic'), null);
        if (path === '/api/jobs/session' && ++attempts === 1) throw new TypeError('offline');
        return original(path, request);
      };
      const client = api.createClient(f.host);
      await client.observe(await submit(f, client));
      assert.equal(attempts, 2);
      assert.equal(f.saved.size, 0);
      assert.equal(f.delivered.length, 1);
    }
  });
  await test('diagnostic elapsed time is bounded even when the optional clock fails or moves backwards', async () => {
    for (const [clock, elapsed] of [[() => { throw new Error('bad clock'); }, 0],
      [(() => { let value = 0; return () => ++value * 999999; })(), 120000],
      [(() => { let value = 0; return () => --value; })(), 0], [() => NaN, 0]]) {
      const f = diagnosticFixture(); f.host.clock = clock;
      f.host.fetch = async () => Response.json(null);
      await assert.rejects(submit(f, api.createClient(f.host)), error => error.transportDiagnostic.elapsed_ms === elapsed);
      assert.equal(f.diagnostic().pending.elapsed_ms, elapsed);
    }
  });
  await test('a changed binding context proves no business submission and consumes a carried diagnostic', async () => {
    const f = diagnosticFixture(), original = f.host.fetch;
    f.host.fetch = async () => { throw new TypeError('offline'); };
    await assert.rejects(submit(f, api.createClient(f.host)));
    const pending = f.diagnostic().pending;
    f.host.fetch = async (path, request) => {
      assert.equal(path, '/api/jobs/session');
      assert.deepEqual(JSON.parse(request.headers.get('X-Tiku-Bind-Diagnostic')), pending);
      return Response.json({ schema_version: 1, execution: { ...f.context, state_version: f.context.state_version + 1 } });
    };
    await assert.rejects(submit(f, api.createClient(f.host)), error => error.code === 'EXECUTION_STALE' && error.submissionNotSent);
    assert.equal(f.entries.size, 0);
    assert.equal(f.diagnostic().pending, null);
    assert.equal(f.diagnostic().entries.length, 2);
    f.host.fetch = async (path, request) => {
      assert.equal(new Headers(request.headers).get('X-Tiku-Bind-Diagnostic'), null);
      return original(path, request);
    };
    const reloaded = api.createClient(f.host);
    await reloaded.observe(await submit(f, reloaded));
    assert.equal(f.calls.filter(call => call.path === '/api/jobs').length, 1);
  });
  await test('malformed binding contexts are protocol failures and preserve the new pending evidence', async () => {
    for (const execution of [undefined, null, [], {}, { epoch, state_version: -1 },
      { epoch, state_version: '4' }, { epoch: 'invalid', state_version: 4 }]) {
      const f = diagnosticFixture();
      f.host.fetch = async () => { throw new TypeError('earlier offline'); };
      await assert.rejects(submit(f, api.createClient(f.host)));
      const previous = f.diagnostic().pending;
      let attempts = 0;
      f.host.fetch = async (path, request) => {
        assert.equal(path, '/api/jobs/session'); attempts++;
        assert.deepEqual(JSON.parse(request.headers.get('X-Tiku-Bind-Diagnostic')), previous);
        return Response.json({ schema_version: 1, execution });
      };
      await assert.rejects(submit(f, api.createClient(f.host)), error => error.code === 'RESPONSE_INVALID'
        && error.submissionNotSent && error.transportDiagnostic.phase === 'protocol');
      assert.equal(attempts, 1);
      assert.equal(f.diagnostic().entries.length, 3);
      assert.equal(f.diagnostic().pending.code, 'RESPONSE_INVALID');
      assert.notEqual(f.diagnostic().pending.request_id, previous.request_id);
      assert.deepEqual(f.diagnostic().entries[1], previous);
      assert.equal(f.entries.size, 0);
    }
  });
  console.log(count + ' client checks passed');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
