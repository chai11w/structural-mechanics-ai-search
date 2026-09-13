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
async function submit(f, client) {
  return client.submit('/api/message/stream', options, f.context, fence, new Headers());
}
async function run() {
  let count = 0;
  async function test(name, action) { await action(); count++; console.log('PASS ' + name); }
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
  console.log(count + ' client checks passed');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
