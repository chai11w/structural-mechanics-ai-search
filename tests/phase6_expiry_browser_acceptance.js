async page => {
  const check = (value, message) => { if (!value) throw Error(message); };
  const origin = page.url().split('/').slice(0, 3).join('/');
  await page.waitForFunction(() => document.querySelector('#status-text').textContent !== '正在恢复会话…');
  const before = await (await page.request.get(origin + '/fixture/counts')).json();
  const originalIds = new Set(before.operations.map(item => item.id));
  const seedExpired = async () => page.evaluate(() => {
    const at = Date.now() - 3 * 3600 * 1000;
    localStorage.setItem('tiku-agent-current-chat-v2', JSON.stringify({
      lastActivityAt: at, savedAt: at, messages: [{me: true, message: 'expired browser fixture', createdAt: at}],
    }));
    localStorage.setItem('tiku-agent-session-activity-v1', String(at));
  });
  await seedExpired();
  const reset = page.waitForResponse(r => r.url() === origin + '/api/reset' && r.status() === 200);
  await page.reload(); await reset;
  await page.waitForFunction(() => !document.querySelector('#empty').hidden);
  check(!(await page.locator('#chat').innerText()).includes('expired browser fixture'), 'expired chat remained');
  check(!(await page.locator('#chat').innerText()).includes('已为你开始新对话'), 'expiry notice hid home');

  await page.route('**/api/reset', route => route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({code:'SERVICE_UNAVAILABLE'})}));
  await seedExpired();
  const refused = page.waitForResponse(r => r.url() === origin + '/api/reset' && r.status() === 503);
  await page.reload(); await refused;
  await page.locator('#startup-retry').waitFor();
  check((await page.evaluate(() => localStorage.getItem('tiku-agent-current-chat-v2'))).includes('expired browser fixture'), 'unacknowledged expiry lost history');
  check(!await page.locator('.app-shell').isVisible(), 'failed startup reset exposed old conversation');
  check(!(await page.locator('#chat').innerText()).includes('已为你开始新对话'), 'failed reset claimed success');
  await page.unroute('**/api/reset');
  const recovered = page.waitForResponse(r => r.url() === origin + '/api/reset' && r.status() === 200);
  await page.locator('#startup-retry').click(); await recovered;
  await page.locator('#startup-screen').waitFor({state:'hidden'});
  await page.waitForFunction(() => !document.querySelector('#empty').hidden);

  await page.route('**/api/reset', route => route.fulfill({status:401,contentType:'application/json',body:JSON.stringify({status:'NEEDS_INPUT',layer:'login',code:'LOGIN_REQUIRED',retryable:false,action:'relogin',request_id:'req_startup_login',search_id:'',schema_version:1})}));
  const auth = page.waitForResponse(r => r.url() === origin + '/api/reset' && r.status() === 401);
  await page.locator('#top-new-chat').click(); await auth;
  await page.getByRole('button', {name:'重新登录',exact:true}).first().waitFor();
  const key = await page.evaluate(() => JSON.parse(localStorage.getItem('tiku-agent-execution-command-v1')).operation.key);
  await page.unroute('**/api/reset');
  const replay = page.waitForRequest(r => r.url() === origin + '/api/reset');
  await page.locator('#top-new-chat').click();
  check(JSON.parse((await replay).headers()['x-tiku-operation']).key === key, 'retry replaced control identity');
  await page.waitForFunction(() => localStorage.getItem('tiku-agent-execution-command-v1') === null);
  await page.waitForFunction(() => !document.querySelector('#empty').hidden);
  const counts = await (await page.request.get(origin + '/fixture/counts')).json();
  check(counts.calls === before.calls && counts.operations.filter(item => !originalIds.has(item.id)).every(item => item.kind === 'clear'), 'recovery invoked provider');
  return {expiredReturnsHome:true,failedResetPreservesHistory:true,noPrematureSuccess:true,reconnectWorks:true,newChatRetriesSameKey:true,loginErrorAction:true,providerCalls:0};
}
