async page => {
  const origin = page.url().split('/').slice(0,3).join('/');
  const counts = async () => (await page.context().request.get(origin + '/fixture/counts')).json();
  await page.reload();
  await page.locator('#execution-panel').waitFor({state:'attached'});
  await page.locator('#top-new-chat').click();
  await page.waitForFunction(() => document.querySelector('#status-text').textContent === '任务状态已更新');
  const before = await counts();
  await page.locator('#file').setInputFiles('.tmp_phase6_4/synthetic-page.png');
  await page.getByRole('button',{name:'选择要查询的题目',exact:true}).click();
  await page.getByRole('checkbox',{name:'四-1自动裁图 四-1 待校验',exact:true}).check();
  await page.getByRole('checkbox',{name:'四-2自动裁图 四-2 待校验',exact:true}).check();
  await page.getByRole('button',{name:'校验所选 2 道题',exact:true}).click();
  await page.waitForFunction(() => !document.querySelector('#text').disabled
    && Object.keys(localStorage).filter(k=>k.startsWith('tiku-agent-background-job-v1:'))
      .map(k=>JSON.parse(localStorage.getItem(k))).some(r=>r.kind==='prepare_units' && r.done), null, {timeout:20000});
  const prepared = await counts();
  if (prepared.calls.length !== before.calls.length + 3) throw Error('expected one page call and two unit checks');
  if (prepared.operations.filter(o=>o.kind==='prepare_units').length !== before.operations.filter(o=>o.kind==='prepare_units').length + 1)
    throw Error('prepare command count mismatch');
  const responses = await page.evaluate(()=>JSON.parse(localStorage.getItem('tiku-agent-current-chat-v2')).messages.filter(m=>m.responseId).map(m=>m.responseId));
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('#status-text').textContent==='准备就绪');
  const restored = await page.evaluate(()=>JSON.parse(localStorage.getItem('tiku-agent-current-chat-v2')).messages.filter(m=>m.responseId).map(m=>m.responseId));
  if (JSON.stringify(responses)!==JSON.stringify(restored)) throw Error('prepared response changed on refresh');
  if ((await counts()).calls.length !== prepared.calls.length) throw Error('prepared units replayed on refresh');
  return {prepareOperations:1,additionalCalls:3,stableResponses:responses.length,reloadCalls:0};
}
