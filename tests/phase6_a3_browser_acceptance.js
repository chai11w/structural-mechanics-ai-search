async page => {
  await page.setViewportSize({width:1280,height:900});
  const origin = page.url().split('/').slice(0,3).join('/');
  const context = page.context();
  const counts = async () => (await context.request.get(origin + '/fixture/counts')).json();
  const history = async () => page.evaluate(() => JSON.parse(localStorage.getItem('tiku-agent-current-chat-v2') || '{"messages":[]}').messages);
  const checks = [];
  const verify = (ok, message) => { if (!ok) throw Error(message); checks.push(message); };
  const settled = async () => page.waitForFunction(() => !document.querySelector('#text').disabled
    && !Object.keys(localStorage).filter(k => k.startsWith('tiku-agent-background-job-v1:'))
      .map(k => JSON.parse(localStorage.getItem(k))).some(r => !r.done), null, { timeout:20000 });
  const reset = async () => {
    if (await page.locator('#a3-crop-back').isVisible()) await page.locator('#a3-crop-back').click();
    await page.locator('#top-new-chat').click();
    await page.waitForFunction(() => document.querySelector('#status-text').textContent === '任务状态已更新', null, { timeout:10000 });
  };
  const boot = page.waitForResponse(response => response.url() === origin + '/api/session' && response.status() === 200);
  await page.reload();
  await boot;
  await page.waitForFunction(()=>document.querySelector('#status-text').textContent==='准备就绪');
  await page.locator('#execution-panel').waitFor();
  await reset();
  const before = await counts();
  await page.locator('#file').setInputFiles('.tmp_phase6_4/synthetic-page.png');
  await page.getByRole('button',{name:'四-1',exact:true}).click();
  await page.getByRole('region',{name:'裁剪结构图',exact:true}).waitFor();
  await settled();
  await page.getByAltText('原始整页题图',{exact:true}).evaluate(image => image.decode());
  const box = await page.getByAltText('原始整页题图',{exact:true}).boundingBox();
  await page.mouse.move(box.x+box.width*.1,box.y+box.height*.1);
  await page.mouse.down();
  await page.mouse.move(box.x+box.width*.8,box.y+box.height*.8,{steps:12});
  await page.mouse.up();
  await page.getByRole('button',{name:'提交并继续搜题',exact:true}).click();
  await page.getByText('我还不能确定',{exact:false}).waitFor();
  await page.getByRole('textbox',{name:'消息',exact:true}).fill('4力法');
  await page.getByRole('button',{name:'发送消息',exact:true}).click();
  await page.getByRole('button',{name:'选择',exact:true}).waitFor();
  await settled();
  const candidateHistory = await history();
  verify(candidateHistory.filter(m => m.message === '我提交了裁剪后的题图。').length === 1,
    'later text does not duplicate submitted crop');
  const ids = candidateHistory.filter(m=>m.responseId).map(m=>m.responseId);
  const countAtCandidate = await counts();
  await page.reload();
  await page.getByRole('button',{name:'选择',exact:true}).waitFor();
  verify(JSON.stringify((await history()).filter(m=>m.responseId).map(m=>m.responseId)) === JSON.stringify(ids),
    'refresh preserves all Response identities and ordering');
  verify((await counts()).calls.length === countAtCandidate.calls.length, 'candidate restoration is read-only');
  // Feedback survives refresh and remains attached to the original Response.
  const feedbackBodies = [];
  page.on('request', request => {
    if (request.url().endsWith('/api/feedback') && request.method() === 'POST') feedbackBodies.push(JSON.parse(request.postData()));
  });
  await page.getByRole('button',{name:'赞，这条回复有帮助',exact:true}).last().click();
  await page.getByRole('button',{name:'提交',exact:true}).click();
  await page.locator('#feedback-backdrop').waitFor({state:'hidden'});
  await page.reload();
  await page.getByRole('button',{name:'踩，这条回复需要改进',exact:true}).last().click();
  await page.getByRole('button',{name:'提交',exact:true}).click();
  await page.locator('#feedback-backdrop').waitFor({state:'hidden'});
  verify(feedbackBodies.length === 2 && Boolean(feedbackBodies[0].rated_response_id)
    && feedbackBodies[0].rated_response_id === feedbackBodies[1].rated_response_id,
    'repeated feedback uses the same restored Response');
  await page.getByRole('button',{name:'选择',exact:true}).click();
  await page.getByAltText('题库答案',{exact:true}).waitFor();
  await settled();
  verify((await counts()).calls.length === before.calls.length + 3, 'A3 page/verifier/child each called once');
  verify(await page.getByRole('button',{name:'候选已失效',exact:true}).isDisabled(), 'consumed candidate loses its action');

  const second = await context.newPage();
  try {
    // Keep an old UI even when the other tab broadcasts reset. The shared
    // request fence/reset check still has to reject its stale click.
    await second.addInitScript(() => {
      const original = window.addEventListener.bind(window);
      window.addEventListener = (type,...args) => { if (type !== 'storage') original(type,...args); };
    });
    await second.goto(origin);
    await second.getByRole('button',{name:'四-2',exact:true}).waitFor();
    await reset();
    const afterReset = await counts();
    await second.getByRole('button',{name:'四-2',exact:true}).click();
    await second.waitForTimeout(500);
    const afterStale = await counts();
    verify(afterStale.operations.length === afterReset.operations.length && afterStale.calls.length === afterReset.calls.length,
      'stale tab after reset creates no operation or model call');
    verify((await history()).filter(m=>m.responseId).length === 0, 'old results cannot refill new epoch history');
  } finally { await second.close(); }
  await page.setViewportSize({width:390,height:844});
  verify(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile layout has no horizontal overflow');
  await page.screenshot({path:'output/playwright/phase6-4-mobile.png'});
  return { checks, counts:await counts() };
}
