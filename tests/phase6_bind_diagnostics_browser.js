/* Offline Edge acceptance. Models, images and runtime databases are synthetic.
 * Usage: node tests/phase6_bind_diagnostics_browser.js --python <python.exe>
 *   --playwright-module <installed playwright package> --output <artifact directory>
 */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { spawn } = require('node:child_process');
const readline = require('node:readline');

// Extend only the existing in-memory fakes: searched units cannot be reused,
// so four units are needed for an initial result and three actual switches.
const fixtureSource = String.raw`
import copy, ipaddress, socket, sys, threading
from dataclasses import replace
import uvicorn
from tests import test_a3_runtime as fake
from tests import phase6_a3_browser_fixture as fixture

original_connect = socket.socket.connect
def local_connect(sock, address):
    assert ipaddress.ip_address(address[0]).is_loopback, 'offline fixture forbids external connections'
    return original_connect(sock, address)
socket.socket.connect = local_connect
original_payload = fake._page_payload
def four_units():
    payload = original_payload()
    for number in (3, 4):
        unit = copy.deepcopy(payload['groups'][0]['units'][0])
        unit.update(unit_id=f'g1-u{number}', question_label=str(number),
                    title_text=f'Synthetic unit {number}', diagram_ids=[f'd{number}'])
        payload['groups'][0]['units'].append(unit)
        diagram = copy.deepcopy(payload['diagrams'][0])
        diagram.update(diagram_id=f'd{number}', unit_ids=[f'g1-u{number}'])
        payload['diagrams'].append(diagram)
    return payload
fake._page_payload = four_units
original_ground = fake.FakeAutoCropper.ground
def four_crops(self, image, units, understanding):
    page = original_ground(self, image, units, understanding)
    extra = tuple(replace(page.targets[0], target_id=f'c00{i}', unit_id=f'g1-u{i}',
                          question_label=f'四-{i}', bbox=box)
                  for i, box in ((3, (80, 500, 470, 750)), (4, (520, 500, 920, 750))))
    return replace(page, targets=page.targets + extra)
fake.FakeAutoCropper.ground = four_crops
original_run = uvicorn.Server.run
def controlled_run(server, *args, **kwargs):
    def stop():
        if sys.stdin.readline().strip() == 'STOP':
            server.should_exit = True
    threading.Thread(target=stop, daemon=True).start()
    return original_run(server, *args, **kwargs)
uvicorn.Server.run = controlled_run
sys.argv = ['offline-bind-browser', '--automatic']
fixture.main()
`;

async function acceptance(page, { imagePath, screenshotPath }) {
  const checks = [];
  const check = (condition, message) => { assert(condition, message); checks.push(message); };
  await page.addInitScript(() => {
    const fetch = window.fetch.bind(window);
    const get = Storage.prototype.getItem, set = Storage.prototype.setItem;
    const diagnosticKey = 'tiku-agent-bind-diagnostics-v1';
    window.__bindTest = { faults: [], requests: [], business: [], lookup: 0, lostAck: 0,
      storageUnavailable: false, storageFailures: 0 };
    Storage.prototype.getItem = function (key) {
      if (this === sessionStorage && key === diagnosticKey && __bindTest.storageUnavailable) {
        __bindTest.storageFailures++; throw new DOMException('synthetic storage failure', 'SecurityError');
      }
      return get.call(this, key);
    };
    Storage.prototype.setItem = function (key, value) {
      if (this === sessionStorage && key === diagnosticKey && __bindTest.storageUnavailable) {
        __bindTest.storageFailures++; throw new DOMException('synthetic storage failure', 'SecurityError');
      }
      return set.call(this, key, value);
    };
    window.fetch = async (input, options = {}) => {
      const url = new URL(typeof input === 'string' ? input : input.url, location.href);
      if (url.origin !== location.origin) throw Error('offline browser forbids external requests');
      const headers = new Headers(options.headers);
      const binding = url.pathname === '/api/jobs/session' && options.method === 'POST';
      const diagnostic = headers.get('X-Tiku-Bind-Diagnostic');
      if (binding || diagnostic) __bindTest.requests.push({ binding,
        requestId: headers.get('X-Request-ID'), diagnostic: diagnostic ? JSON.parse(diagnostic) : null });
      if (binding) {
        const fault = __bindTest.faults.shift();
        if (fault === 'fetch-timeout') throw new DOMException('synthetic timeout', 'TimeoutError');
        if (fault === 'body-network') return { ok: true, status: 200,
          json: async () => { throw new TypeError('synthetic body disconnect'); } };
        if (fault === 'protocol-null') return Response.json(null);
        if (fault === 'http-error') return Response.json({ schema_version: 1, code: 'EXECUTION_UNAVAILABLE' }, { status: 503 });
      }
      if (url.pathname === '/api/jobs/lookup') __bindTest.lookup++;
      const business = ['/api/jobs', '/api/jobs/image'].includes(url.pathname) && options.method === 'POST';
      const kind = business ? url.pathname.endsWith('/image') ? 'handle_image' : JSON.parse(options.body).kind : '';
      if (business) __bindTest.business.push(kind); // Deliberately exclude bodies, images and identities.
      const response = await fetch(input, options);
      if (kind === 'select_unit' && __bindTest.lostAck > 0) {
        __bindTest.lostAck--;
        throw new TypeError('synthetic lost acknowledgement after acceptance');
      }
      return response;
    };
  });
  await page.reload();
  await page.locator('#startup-screen').waitFor({ state: 'hidden' });
  await page.waitForFunction(() => document.querySelector('#status-text').textContent === '准备就绪');
  const settled = () => page.waitForFunction(() => !document.querySelector('#file').disabled
    && !Object.keys(localStorage).filter(key => key.startsWith('tiku-agent-background-job-v1:'))
      .map(key => JSON.parse(localStorage.getItem(key))).some(record => !record.done));
  const snapshot = () => page.evaluate(() => ({
    bindings: __bindTest.requests.filter(request => request.binding).length,
    selections: __bindTest.business.filter(kind => kind === 'select_unit').length,
    uploads: __bindTest.business.filter(kind => kind === 'handle_image').length,
    lookup: __bindTest.lookup,
  }));
  const candidates = () => page.locator('.select-candidate').count();
  const openSheet = async () => {
    if (await page.locator('#a3-sheet-backdrop').isVisible()) return;
    await page.locator('.a3-switch-question:visible').last().click();
    await page.locator('#a3-sheet-backdrop').waitFor({ state: 'visible' });
  };
  const select = async number => {
    await openSheet();
    await page.locator(`#a3-sheet-units button[data-workflow-unit-id="g1-u${number}"]`).click();
  };
  const finishCandidate = async before => {
    await settled();
    if (await candidates() <= before) {
      await page.getByText('我还不能确定', { exact: false }).last().waitFor();
      await page.locator('#text').fill('4力法');
      const posts = await page.evaluate(() => __bindTest.business.length);
      await page.getByRole('button', { name: '发送消息', exact: true }).click();
      await page.waitForFunction(before => __bindTest.business.length > before, posts);
      await settled();
    }
    check(await candidates() > before, 'new selected unit reaches visible candidates');
    check(await page.locator('.select-candidate').last().isVisible(), 'new candidate control is visible in the real DOM');
    await page.locator('.media-card img').evaluateAll(images => Promise.all(images.map(image => image.decode())));
    await page.locator('.select-candidate').last().evaluate(button => button.scrollIntoView({ block: 'center', behavior: 'instant' }));
    check(await page.locator('.select-candidate').last().evaluate(button => {
      const rect = button.getBoundingClientRect();
      return rect.top >= 56 && rect.bottom <= document.querySelector('#text').getBoundingClientRect().top;
    }), 'candidate action is inside the viewport above the composer');
    await page.screenshot({ path: screenshotPath.replace('.png', `-result-${await candidates()}.png`) });
  };
  const failureVisible = () => page.getByText('连接暂时不稳定，本次任务尚未提交。请重新连接后继续。', { exact: true }).last().waitFor();
  await page.locator('#file').setInputFiles(imagePath);
  await settled();
  if (!(await page.locator('#a3-sheet-backdrop').isVisible())) {
    await page.locator('.a3-open-auto-selection:visible').last().click();
  }
  const boxes = page.locator('#a3-sheet-units input[type="checkbox"]');
  check(await boxes.count() === 4, 'existing fixture exposes four synthetic independent units');
  for (let i = 0; i < 4; i++) await boxes.nth(i).check();
  await page.getByRole('button', { name: '校验所选 4 道题', exact: true }).click();
  await settled();
  if (!(await page.locator('#a3-sheet-backdrop').isVisible())) {
    await page.locator('.a3-open-auto-selection:visible').last().click();
  }
  await page.locator('#a3-sheet-units button[data-workflow-unit-id="g1-u1"]').click();
  await finishCandidate(0);
  check((await snapshot()).uploads === 1, 'one synthetic upload establishes all switch scenarios');

  // Switch 1: the sole automatic bind retry succeeds even when diagnostics storage fails.
  let before = await snapshot(), previousCandidates = await candidates();
  await page.evaluate(() => { __bindTest.faults = ['fetch-timeout']; __bindTest.storageUnavailable = true; });
  await select(2);
  await settled();
  let after = await snapshot();
  check(after.bindings === before.bindings + 2 && after.selections === before.selections + 1,
    'first failed binding retries once and submits selection once');
  check(await page.evaluate(() => __bindTest.storageFailures > 0), 'diagnostic storage failure is fail-open');
  await page.evaluate(() => { __bindTest.storageUnavailable = false; });
  await finishCandidate(previousCandidates);

  // Switch 2: two failures leave both the previous result and the unsent draft intact.
  before = await snapshot(); previousCandidates = await candidates();
  const draft = 'synthetic draft must survive';
  await page.locator('#text').fill(draft);
  await page.evaluate(() => { __bindTest.faults = ['body-network', 'body-network']; });
  await select(3);
  await failureVisible(); await settled();
  after = await snapshot();
  check(after.bindings === before.bindings + 2 && after.selections === before.selections,
    'two body failures stop before any business submission');
  check(await candidates() === previousCandidates, 'two failed bindings preserve prior visible candidates');
  check(await page.locator('#text').inputValue() === draft, 'two failed bindings preserve unsent text');
  check(await page.evaluate(() => !Object.keys(localStorage).some(key => key.startsWith('tiku-agent-session-request-fence-v1:pending:'))),
    'unsubmitted selection releases its request fence');
  await page.screenshot({ path: screenshotPath.replace('.png', '-two-failures.png') });
  await openSheet();
  check(await page.locator('#a3-sheet-units button[data-workflow-unit-id="g1-u3"]').isEnabled(),
    'failed unit remains selectable in the original question sheet');
  await page.screenshot({ path: screenshotPath.replace('.png', '-retry-choice.png') });
  await select(3);
  await finishCandidate(previousCandidates);
  check((await snapshot()).selections === before.selections + 1, 'manual selection after two failures submits once');

  // Switch 3: protocol/HTTP failures do not use the transport retry budget;
  // once accepted, a lost ACK is discovered by its original key, never resent.
  before = await snapshot(); previousCandidates = await candidates();
  for (const fault of ['protocol-null', 'http-error']) {
    const start = await snapshot();
    await page.evaluate(value => { __bindTest.faults = [value]; }, fault);
    await select(4); await settled();
    const end = await snapshot();
    check(end.bindings === start.bindings + 1 && end.selections === start.selections,
      `${fault} fails without automatic binding or business retry`);
  }
  await page.evaluate(() => { __bindTest.lostAck = 1; });
  await select(4);
  await finishCandidate(previousCandidates);
  after = await snapshot();
  check(after.selections === before.selections + 1 && after.lookup > before.lookup,
    'lost selection ACK restores original task with exactly one POST');
  check(after.uploads === 1 && after.selections === 4, 'three real switches never reupload or duplicate selections');

  const evidence = await page.evaluate(() => ({ requests: __bindTest.requests,
    diagnostics: JSON.parse(sessionStorage.getItem('tiku-agent-bind-diagnostics-v1')) }));
  const fields = ['code', 'elapsed_ms', 'phase', 'request_id', 'schema', 'status'];
  const valid = value => value && JSON.stringify(Object.keys(value).sort()) === JSON.stringify(fields)
    && value.schema === 1 && /^req_[0-9a-f]{32}$/.test(value.request_id)
    && ['fetch', 'body', 'protocol'].includes(value.phase)
    && ['REQUEST_TIMEOUT', 'NETWORK_UNAVAILABLE', 'RESPONSE_INVALID', 'HTTP_ERROR'].includes(value.code)
    && Number.isInteger(value.elapsed_ms) && value.elapsed_ms >= 0 && value.elapsed_ms <= 120000
    && Number.isInteger(value.status) && value.status >= 0 && value.status <= 599;
  check(evidence.requests.every(request => request.binding), 'diagnostic header is sent only on existing binding POSTs');
  const ids = evidence.requests.map(request => request.requestId);
  check(ids.every(id => /^req_[0-9a-f]{32}$/.test(id)) && new Set(ids).size === ids.length,
    'each binding attempt has a unique request ID');
  const headers = evidence.requests.filter(request => request.diagnostic).map(request => request.diagnostic);
  check(headers.every(valid), 'diagnostic header contains only the six bounded whitelist fields');
  check(evidence.requests.every((request, index) => !request.diagnostic
    || evidence.requests.slice(0, index).some(previous => previous.requestId === request.diagnostic.request_id)),
    'carried diagnostic refers to a previous failed attempt, never the current request');
  const log = evidence.diagnostics;
  check(log.schema === 1 && log.pending === null && log.entries.length === 5 && log.entries.length <= 16,
    'successful binding clears pending while keeping bounded failure history');
  check(log.entries.every(valid), 'stored diagnostics contain no image, body, URL, cookie or business parameters');
  check(JSON.stringify(log.entries.map(({ phase, code, status }) => [phase, code, status])) === JSON.stringify([
    ['fetch', 'REQUEST_TIMEOUT', 0], ['body', 'NETWORK_UNAVAILABLE', 200], ['body', 'NETWORK_UNAVAILABLE', 200],
    ['protocol', 'RESPONSE_INVALID', 200], ['protocol', 'HTTP_ERROR', 503],
  ]), 'failure phase and status distinguish fetch, body, protocol and HTTP rejection');
  check(log.entries.every(entry => headers.some(header => header.request_id === entry.request_id)),
    'every retained failure accompanies a later existing binding attempt');
  check(!/Failed to fetch|synthetic body disconnect|synthetic lost acknowledgement/.test(await page.locator('body').innerText()),
    'native transport exception text is absent from visible results');
  await page.screenshot({ path: screenshotPath });
  return { checks, counts: after, visibleCandidateLabels: await page.locator('.media-footer').allTextContents(),
    failureKinds: log.entries.map(({ phase, code, status }) => ({ phase, code, status })) };
}

async function main() {
  const option = name => process.argv[process.argv.indexOf(name) + 1];
  for (const required of ['--python', '--playwright-module', '--output']) {
    assert(process.argv.includes(required) && option(required), `required argument: ${required}`);
  }
  const output = path.resolve(option('--output'));
  await fs.mkdir(output, { recursive: true });
  const { chromium } = require(path.resolve(option('--playwright-module')));
  const child = spawn(option('--python'), ['-X', 'utf8', '-B', '-u', '-c', fixtureSource], {
    cwd: path.resolve(__dirname, '..'), windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
  });
  let browser, page, result, exited = false;
  let stderr = '';
  child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-12000); });
  const childExit = new Promise(resolve => child.once('exit', code => { exited = true; resolve(code); }));
  try {
    const meta = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(Error('offline fixture did not start')), 20000);
      const lines = readline.createInterface({ input: child.stdout });
      lines.on('line', line => {
        try {
          const value = JSON.parse(line);
          if (value.port && value.image) { clearTimeout(timeout); lines.close(); resolve(value); }
        } catch (_) { /* Only fixture metadata is accepted. */ }
      });
      child.once('error', reject);
      childExit.then(code => { clearTimeout(timeout); reject(Error(`fixture exited ${code}: ${stderr}`)); });
    });
    const origin = `http://127.0.0.1:${meta.port}`;
    browser = await chromium.launch({ channel: 'msedge', headless: true });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.route('**/*', route => new URL(route.request().url()).origin === origin
      ? route.continue() : route.abort('blockedbyclient'));
    page = await context.newPage();
    page.setDefaultTimeout(20000);
    await page.goto(origin + '/fixture/start');
    result = await acceptance(page, { imagePath: meta.image, screenshotPath: path.join(output, 'bind-diagnostics.png') });
  } catch (error) {
    if (page) {
      await page.screenshot({ path: path.join(output, 'failure.png') });
      await fs.writeFile(path.join(output, 'failure.json'), JSON.stringify({ error: error.message,
        syntheticPage: await page.locator('body').innerText(), counters: await page.evaluate(() => window.__bindTest) }, null, 2), { flag: 'wx' });
    }
    throw error;
  } finally {
    if (browser) await browser.close();
    if (!exited) child.stdin.end('STOP\n');
    let timer;
    const code = await Promise.race([childExit, new Promise(resolve => { timer = setTimeout(() => resolve('timeout'), 12000); })]);
    clearTimeout(timer);
    if (code === 'timeout') { child.kill(); throw Error('owned offline fixture failed graceful cleanup'); }
    assert.equal(code, 0, `offline fixture cleanup failed: ${stderr}`);
  }
  await fs.writeFile(path.join(output, 'result.json'), JSON.stringify({ ...result, fixtureExit: 0 }, null, 2), { flag: 'wx' });
  console.log(`${result.checks.length} offline browser checks passed`);
}

if (require.main === module) main().catch(error => { console.error(error); process.exitCode = 1; });
module.exports = { acceptance, fixtureSource };
