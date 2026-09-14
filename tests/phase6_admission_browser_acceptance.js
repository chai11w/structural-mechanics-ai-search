async page => {
  const origin = page.url().split('/').slice(0, 3).join('/');
  const context = page.context();
  const checks = [];
  const verify = (value, message) => { if (!value) throw Error(message); checks.push(message); };
  const counts = async () => (await context.request.get(origin + '/fixture/counts')).json();
  const gate = release => context.request.post(origin + '/fixture/gate', {data:{release}});
  const send = async text => {
    await page.getByRole('textbox', {name:'消息', exact:true}).fill(text);
    await page.getByRole('button', {name:'发送消息', exact:true}).click();
  };
  const suffix = '-' + Math.random().toString(16).slice(2);
  await page.waitForFunction(() => document.querySelector('#status-text').textContent === '准备就绪');
  const before = await counts();
  try {
    // Server semantics are independently exercised with a real full queue in
    // test_background_rejection; this checks browser recovery from that response.
    await page.route(origin + '/api/jobs', route => route.fulfill({status:429, contentType:'application/json',
      body:JSON.stringify({schema_version:1,code:'EXECUTION_QUEUE_FULL',admission:'rejected'})}));
    await send('rejected' + suffix);
    await page.getByText('当前任务较多，本次任务未接收，请稍后重新提交。', {exact:true}).waitFor();
    verify(await page.evaluate(() => !Object.keys(localStorage).filter(k => k.startsWith('tiku-agent-background-job-v1:'))
      .map(k => JSON.parse(localStorage.getItem(k))).some(r => !r.done)), 'rejection leaves no pending job');
    verify((await counts()).calls === before.calls, 'rejected submission made no model call');
    await page.unroute(origin + '/api/jobs');
    await send('manual-retry' + suffix);
    await page.getByText('reply:manual-retry' + suffix, {exact:true}).waitFor();
    verify((await counts()).calls === before.calls + 1, 'manual retry works without resetting the conversation');

    await gate(false);
    await send('retired-running' + suffix);
    for (let i = 0; i < 100 && (await counts()).calls < before.calls + 2; i++) await page.waitForTimeout(100);
    verify((await counts()).calls === before.calls + 2, 'original task reached the provider');
    await page.locator('#top-new-chat').click();
    await page.waitForFunction(() => document.querySelector('#status-text').textContent === '任务状态已更新');
    await gate(true);
    await page.waitForTimeout(1200);
    const oldId = await page.evaluate(() => {
      const prefix = 'tiku-agent-background-job-v1:';
      const keys = Object.keys(localStorage).filter(k => k.startsWith(prefix));
      const old = keys.map(k => JSON.parse(localStorage.getItem(k))).find(r => !r.done);
      if (!old?.id) throw Error('running reset did not leave a recoverable original receipt');
      for (const key of keys) if (JSON.parse(localStorage.getItem(key)).done) localStorage.removeItem(key);
      for (let i = 1; i < 64; i++) {
        const record = {...old, key:'retired-browser-' + String(i).padStart(4,'0')};
        localStorage.setItem(prefix + record.key, JSON.stringify(record));
      }
      return old.id;
    });
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#status-text').textContent === '准备就绪');
    await send('after-64-retired' + suffix);
    await page.getByText('reply:after-64-retired' + suffix, {exact:true}).waitFor();
    verify((await counts()).calls === before.calls + 3, '64 old receipts do not block or replay the new task');
    verify(await page.evaluate(() => Object.keys(localStorage).filter(k => k.startsWith('tiku-agent-background-job-v1:')).length === 1),
      'only current transport receipt remains');
    verify((await counts()).operations.some(op => op.id === oldId && op.status === 'CANCELLED'),
      'retiring browser metadata preserves cancelled server execution evidence');
    verify(!(await page.locator('#execution-panel').isVisible()), 'product control panel remains hidden');
    return {checks, callsAdded:3};
  } finally {
    await gate(true);
    await page.unroute(origin + '/api/jobs');
  }
}
