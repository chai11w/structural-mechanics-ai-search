async (page) => {
  const origin = page.url().split('/').slice(0, 3).join('/');
  const context = page.context();
  const checks = [];
  const verify = (ok, message) => { if (!ok) throw new Error(message); checks.push(message); };
  const counts = async () => (await context.request.get(origin + '/fixture/counts')).json();
  const gate = async release => {
    const r = await context.request.post(origin + '/fixture/gate', { data: { release } });
    if (!r.ok()) throw new Error('fixture gate failed ' + r.status());
  };
  const waitReply = async (tab, text) => {
    await tab.getByText('reply:' + text, { exact: true }).waitFor({ timeout: 20000 });
    verify(await tab.getByText('reply:' + text, { exact: true }).count() === 1, 'one visible reply: ' + text);
  };
  const send = async text => {
    await page.getByRole('textbox', { name: '消息', exact: true }).fill(text);
    await page.getByRole('button', { name: '发送消息', exact: true }).click();
  };
  const receipt = async () => page.evaluate(() => Object.keys(localStorage)
    .filter(key => key.startsWith('tiku-agent-background-job-v1:'))
    .map(key => JSON.parse(localStorage.getItem(key))).find(record => !record.done));
  const awaitRunning = async expected => {
    for (let n = 0; n < 100; n++) {
      const state = await counts();
      if (state.calls === expected && state.operations.some(op => op.status === 'RUNNING')) return state;
      await page.waitForTimeout(100);
    }
    throw new Error('provider did not start');
  };
  const suffix = '-' + Math.random().toString(16).slice(2);
  const initial = await counts();
  let second;
  try {
    await gate(false);
    await send(('refresh-running' + suffix));
    await awaitRunning(initial.calls + 1);
    const original = await receipt();
    verify(Boolean(original?.id), 'ACK durably associated before refresh');
    await page.reload();
    second = await context.newPage();
    await second.goto(origin);
    await page.getByText('任务正在后台处理…', { exact: true }).first().waitFor();
    await gate(true);
    await waitReply(page, ('refresh-running' + suffix));
    await waitReply(second, ('refresh-running' + suffix));
    let state = await counts();
    verify(state.calls === initial.calls + 1 && state.attempts === initial.attempts + 1,
      'refresh and second tab observe one call and attempt');
    await second.close(); second = null;
    await page.reload(); await waitReply(page, ('refresh-running' + suffix));
    verify((await counts()).calls === state.calls, 'terminal refresh is read-only');

    await gate(false);
    await send(('offline-running' + suffix));
    await awaitRunning(initial.calls + 2);
    await context.setOffline(true);
    await gate(true);
    await page.waitForTimeout(1300);
    await context.setOffline(false);
    await waitReply(page, ('offline-running' + suffix));
    verify((await counts()).calls === initial.calls + 2, 'offline and reconnect never replay');

    // Lose only the ACK after the server durably accepted the actual POST.
    await page.route(origin + '/api/jobs', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      await route.fetch();
      await route.abort('failed');
    });
    await send(('ack-lost' + suffix));
    await waitReply(page, ('ack-lost' + suffix));
    await page.unroute(origin + '/api/jobs');
    verify((await counts()).calls === initial.calls + 3, 'lost ACK discovers original operation');

    // A browser without Web Locks can view history, but not start a task.
    second = await context.newPage();
    await second.addInitScript(() => Object.defineProperty(navigator, 'locks', { value: undefined }));
    const noLocksReady = second.waitForResponse(response => response.url() === origin + '/api/session' && response.status() === 200);
    await second.goto(origin);
    await noLocksReady;
    await second.waitForFunction(()=>document.querySelector('#status-text').textContent==='准备就绪');
    await second.getByRole('textbox', { name: '消息', exact: true }).fill('must-not-submit');
    await second.getByRole('button', { name: '发送消息', exact: true }).click();
    await second.getByText('此浏览器无法安全协调后台任务，请刷新页面或更换浏览器。').waitFor();
    verify((await counts()).calls === initial.calls + 3, 'no Web Lock fails closed');
    await second.close(); second = null;

    await gate(false);
    await send('closed-page' + suffix);
    await awaitRunning(initial.calls + 4);
    await page.goto('about:blank'); // no task document or observer remains
    await gate(true);
    await page.goto(origin);
    await waitReply(page, 'closed-page' + suffix);
    verify((await counts()).calls === initial.calls + 4, 'closing the task document does not cancel or replay');

    await gate(false);
    await page.route(origin + '/api/jobs', async route => { await route.fetch(); await route.abort('failed'); });
    await page.route('**/api/jobs/lookup?**', route => route.abort('failed'));
    await send('ack-refresh' + suffix);
    await awaitRunning(initial.calls + 5);
    await page.waitForFunction(() => Object.keys(localStorage).filter(k=>k.startsWith('tiku-agent-background-job-v1:'))
      .map(k=>JSON.parse(localStorage.getItem(k))).some(r=>!r.done && !r.id));
    await page.reload();
    await page.unroute('**/api/jobs/lookup?**');
    await page.unroute(origin + '/api/jobs');
    await gate(true);
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    await waitReply(page, 'ack-refresh' + suffix);
    verify((await counts()).calls === initial.calls + 5, 'lost ACK plus refresh discovers saved key without business replay');

    second = await context.newPage();
    await second.addInitScript(() => {
      const original = window.addEventListener.bind(window);
      window.addEventListener = (type,...args) => { if(type !== 'storage') original(type,...args); };
    });
    const staleTabReady = second.waitForResponse(response => response.url() === origin + '/api/session' && response.status() === 200);
    await second.goto(origin);
    await staleTabReady;
    await second.waitForFunction(()=>document.querySelector('#status-text').textContent==='准备就绪');
    // Wait for the complete shared bootstrap transaction, not just its status
    // text (which is updated before the Web Lock callback has returned).
    await second.evaluate(() => navigator.locks.request('tiku-agent-session-request-v1', () => true));
    await page.evaluate(() => navigator.locks.request('tiku-agent-session-request-v1', () => true));
    await send('shared-history' + suffix);
    await waitReply(page, 'shared-history' + suffix);
    await second.getByRole('textbox',{name:'消息',exact:true}).fill('stale-tab-input');
    await second.getByRole('button',{name:'发送消息',exact:true}).click();
    await waitReply(second, 'shared-history' + suffix);
    verify((await counts()).calls === initial.calls + 6, 'stale tab preserves published reply and cannot execute old version');
    await second.close(); second = null;

    state = await counts();
    verify(state.attempts === initial.attempts + 6, 'six commands have exactly six attempts');
    return { checks, counts: state };
  } finally {
    await context.setOffline(false);
    await gate(true);
    await page.unroute(origin + '/api/jobs');
    if (second) await second.close();
  }
}
