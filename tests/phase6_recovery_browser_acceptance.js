async page => {
  const origin = page.url().split('/').slice(0,3).join('/');
  const context = page.context();
  const checks = [];
  const verify = (value, message) => { if (!value) throw Error(message); checks.push(message); };
  const counts = async () => (await context.request.get(origin + '/fixture/counts')).json();
  const history = async tab => tab.evaluate(() => JSON.parse(localStorage.getItem('tiku-agent-current-chat-v2')).messages);
  const settled = async () => page.waitForFunction(() => !document.querySelector('#text').disabled
    && !Object.keys(localStorage).filter(k => k.startsWith('tiku-agent-background-job-v1:'))
      .map(k => JSON.parse(localStorage.getItem(k))).some(r => !r.done), null, {timeout:20000});
  const showControls = async () => {
    // Explicit diagnostic opt-in; the product keeps this panel hidden.
    await page.addStyleTag({content: '.execution-panel:not([hidden]) { display: block !important; }'});
    if (!(await page.locator('#execution-panel').getAttribute('open')) &&
        !(await page.locator('#execution-panel').evaluate(el => el.open))) await page.locator('#execution-panel summary').click();
  };
  await page.setViewportSize({width:1280,height:900});
  await page.waitForFunction(() => document.querySelector('#status-text').textContent === '准备就绪');
  const before = await counts();
  await page.locator('#file').setInputFiles('.tmp_tests/phase6_5/synthetic-page.png');
  await page.getByRole('button',{name:'四-1',exact:true}).click();
  await page.getByRole('region',{name:'裁剪结构图',exact:true}).waitFor();
  await settled();
  const sourceImage = page.getByAltText('原始整页题图',{exact:true});
  await sourceImage.evaluate(image => image.decode());
  const box = await sourceImage.boundingBox();
  await page.mouse.move(box.x+box.width*.1,box.y+box.height*.1);
  await page.mouse.down();
  await page.mouse.move(box.x+box.width*.8,box.y+box.height*.8,{steps:12});
  await page.mouse.up();
  await page.getByRole('button',{name:'提交并继续搜题',exact:true}).click();
  await page.getByText('任务执行结果尚未确认',{exact:false}).waitFor();
  await settled();
  const unknown = await counts();
  const source = unknown.operations.find(op => op.kind === 'handle_crop' && op.status === 'UNKNOWN').id;
  verify(unknown.calls.length === before.calls.length + 3, 'page, verifier and child each called once before interruption');
  const priorIds = (await history(page)).filter(m => m.responseId).map(m => m.responseId);
  const controlPosts = [], controlReplies = [];
  let abortAck = true;
  await page.route('**/api/execution/recover', async route => {
    controlPosts.push(route.request().headers()['x-tiku-operation']);
    const response = await route.fetch();
    controlReplies.push(await response.json());
    if (abortAck) { abortAck = false; await route.abort('failed'); }
    else await route.fulfill({response});
  });
  await showControls();
  await page.locator('#execution-refresh').click();
  await page.getByRole('button',{name:'核对并恢复已保存结果',exact:true}).click();
  await page.locator('#execution-retry').waitFor();
  await page.reload();
  await page.locator('#execution-panel').waitFor({state:'attached'});
  await showControls();
  await page.locator('#execution-retry').click();
  await page.getByText('我还不能确定',{exact:false}).waitFor();
  await settled();
  const job = await (await context.request.get(origin + '/api/jobs/' + source)).json();
  const responseId = job.job.publication.result.response_id;
  const recovered = await history(page);
  verify(controlPosts.length === 2 && controlPosts[0] === controlPosts[1], 'lost recovery ACK reuses the original control operation after refresh');
  verify(controlReplies.every(reply => !reply.response_id && reply.images.length === 0), 'recovery ACK creates no scoreable business reply');
  verify(recovered.filter(m => m.backgroundKey === 'job:' + source).length === 1
    && recovered.find(m => m.backgroundKey === 'job:' + source).responseId === responseId, 'UNKNOWN notice replaced by the original stable publication');
  verify(recovered.filter(m => m.responseId).length === priorIds.length + 1, 'one recovered reply is added to history');
  verify(recovered.findIndex(m => m.backgroundKey === 'job:' + source + ':input')
    < recovered.findIndex(m => m.backgroundKey === 'job:' + source), 'recovered crop precedes its business reply');
  await page.reload();
  await page.getByText('我还不能确定',{exact:false}).waitFor();
  const second = await context.newPage();
  await second.goto(origin + '/');
  await second.getByText('我还不能确定',{exact:false}).waitFor();
  verify((await history(second)).filter(m => m.responseId === responseId).length === 1, 'second tab restores one original Response');
  await second.close();
  const feedback = [];
  page.on('request', req => { if (req.url().endsWith('/api/feedback')) feedback.push(JSON.parse(req.postData())); });
  for (const label of ['赞，这条回复有帮助', '踩，这条回复需要改进']) {
    await page.getByRole('button',{name:label,exact:true}).last().click();
    await page.getByRole('button',{name:'提交',exact:true}).click();
    await page.locator('#feedback-backdrop').waitFor({state:'hidden'});
    await page.reload();
    await page.getByText('我还不能确定',{exact:false}).waitFor();
  }
  verify(feedback.length === 2 && feedback.every(item => item.rated_response_id === responseId), 'repeated feedback stays attached to the original recovered Response');
  const final = await counts();
  verify(final.calls.length === unknown.calls.length && final.operations.filter(op => op.kind === 'recover_operation').length === 1,
    'recovery, refresh, second tab and feedback add zero model calls and one idempotent control');
  await page.screenshot({path:'output/playwright/phase6-5-recovery.png',fullPage:true});
  return {checks, calls:final.calls.length, source, responseId};
}
