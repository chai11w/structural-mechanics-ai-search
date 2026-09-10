// Run against tests.phase5_browser_fixture with the product's default CSS.
async (page) => {
  const origin = await page.evaluate(() => location.origin);
  await page.goto(origin + '/fixture/start?mode=ready');
  await page.waitForFunction(() => !document.querySelector('#execution-panel').hidden);
  if (await page.locator('#execution-panel').isVisible()) throw Error('product panel must stay hidden');
  const sent = [];
  page.on('request', request => {
    if (request.url().endsWith('/api/reset')) sent.push({
      operation: request.headers()['x-tiku-operation'], body: request.postData(),
    });
  });
  const epoch = () => page.evaluate(async () => (await (await fetch('/api/execution')).json()).execution.epoch);
  const journal = () => page.evaluate(() => localStorage.getItem(TikuExecutionControl.JOURNAL_KEY));
  const initialEpoch = await epoch();
  const initialAnalysisCount = await page.evaluate(async () => (await (await fetch('/fixture/status')).json()).analysis_count);
  const initialResets = await page.evaluate(async () => (await (await fetch('/fixture/status')).json())
    .operations.filter(row => row.kind === 'clear').length);
  const counts = [];
  for (const recovery of ['visible_retry', 'new_chat_after_reload']) {
    const start = sent.length;
    await page.route('**/api/reset', async route => {
      await route.fetch();
      await route.abort('failed');
    }, { times: 1 });
    await page.getByRole('button', { name: '开始新对话', exact: true }).click();
    await page.locator('.message-recovery').filter({ hasText: '核对上次操作' }).waitFor({ state: 'visible' });
    if (!await journal()) throw Error('unconfirmed reset journal was lost');
    const committedEpoch = await epoch();
    if (await page.locator('#execution-panel').isVisible()) throw Error('error exposed the control panel');
    if (recovery === 'new_chat_after_reload') {
      await page.reload();
      await page.locator('.message-recovery').filter({ hasText: '核对上次操作' }).waitFor({ state: 'visible' });
      await page.getByRole('button', { name: '开始新对话', exact: true }).click();
    } else {
      await page.locator('.message-recovery').filter({ hasText: '核对上次操作' }).click();
    }
    await page.waitForFunction(() => !localStorage.getItem(TikuExecutionControl.JOURNAL_KEY)
      && document.querySelector('#status-text').textContent === '任务状态已更新'
      && !document.querySelector('#execution-refresh').disabled);
    if (sent.length !== start + 2 || sent[start].operation !== sent[start + 1].operation
        || sent[start].body !== sent[start + 1].body || await epoch() !== committedEpoch) {
      throw Error('reconciliation created a fresh reset or changed its operation');
    }
    if (await page.locator('[data-notice-key="execution-control"]').count()) throw Error('resolved notice remained');
    counts.push({ recovery, requests: 2, sameOperation: true, journalCleared: true });
  }
  const beforeNew = await epoch();
  await page.getByRole('button', { name: '开始新对话', exact: true }).click();
  await page.waitForFunction(() => !localStorage.getItem(TikuExecutionControl.JOURNAL_KEY)
    && !document.querySelector('#execution-refresh').disabled);
  const finalEpoch = await epoch();
  if (finalEpoch === beforeNew || finalEpoch === initialEpoch) throw Error('new reset did not rotate epoch');
  const fixture = await page.evaluate(async () => (await (await fetch('/fixture/status')).json()));
  if (fixture.analysis_count !== initialAnalysisCount) throw Error('reset recovery invoked the model');
  // Two lost-response commands and one fresh reset follow the fixture setup.
  const resets = fixture.operations.filter(row => row.kind === 'clear');
  if (resets.length !== initialResets + 3 || resets.some(row => row.status !== 'SUCCEEDED')) throw Error('duplicate reset receipt');
  await page.setViewportSize({ width: 390, height: 844 });
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw Error('mobile overflow');
  if (await page.locator('#execution-panel').isVisible()) throw Error('panel became visible');
  return { counts, resetRequests: sent.length, resetReceiptsExcludingFixture: resets.length - initialResets,
    nextResetWorks: true, panelHidden: true, analysisCount: fixture.analysis_count, mobileWidth: 390 };
}
