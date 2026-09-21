async page => {
  const checks = [];
  const check = (ok, why) => { if (!ok) throw Error(why); checks.push(why); };
  const ready = () => page.locator('#startup-screen').waitFor({state:'hidden'});
  const seed = async expired => page.evaluate(expired => {
    const at = Date.now() - (expired ? 3 * 3600000 : 1000);
    localStorage.setItem('tiku-agent-current-chat-v2', JSON.stringify({lastActivityAt:at,savedAt:at,messages:[{me:true,message:'startup preserved history',createdAt:at}]}));
    localStorage.setItem('tiku-agent-session-activity-v1', String(at));
  }, expired);
  const stored = () => page.evaluate(() => localStorage.getItem('tiku-agent-current-chat-v2'));
  const covered = async label => {
    check(await page.locator('#startup-screen').isVisible(), label + ': loading visible');
    check(!await page.locator('.app-shell').isVisible(), label + ': neither home nor transcript visible');
  };
  await ready();
  check(await page.locator('#empty').isVisible(), 'fresh visit resolves to home');
  await page.setViewportSize({width:390,height:844});
  let releaseScript;
  const scriptGate = new Promise(resolve => { releaseScript = resolve; });
  await page.route('**/assets/demo.js?*', async route => { await scriptGate; await route.continue(); });
  await page.reload({waitUntil:'commit'});
  await page.locator('#startup-screen').waitFor();
  await covered('before application JavaScript');
  await page.waitForFunction(() => getComputedStyle(document.querySelector('.startup-progress')).visibility === 'visible');
  await page.screenshot({path:'output/playwright/startup-mobile.png'});
  await page.setViewportSize({width:1280,height:900});
  await page.screenshot({path:'output/playwright/startup-desktop.png'});
  releaseScript(); await ready(); await page.unroute('**/assets/demo.js?*');

  await seed(true);
  let releaseSession;
  const sessionGate = new Promise(resolve => { releaseSession = resolve; });
  await page.route('**/api/session', async route => { await sessionGate; await route.continue(); });
  await page.reload();
  await covered('expired history while server read pending');
  check((await stored()).includes('startup preserved history'), 'pending read retains stored history');
  let releaseReset;
  const resetGate = new Promise(resolve => { releaseReset = resolve; });
  await page.route('**/api/reset', async route => { await resetGate; await route.continue(); });
  const resetStarted = page.waitForRequest('**/api/reset'); releaseSession(); await resetStarted;
  await covered('expired history while reset pending');
  releaseReset(); await ready();
  check(await page.locator('#empty').isVisible(), 'acknowledged expiry reveals home');
  check(!(await stored() || '').includes('startup preserved history'), 'acknowledged expiry clears old records');
  await page.unroute('**/api/session'); await page.unroute('**/api/reset');

  // A valid authoritative reply overrides an expired client clock.
  await seed(true);
  let releaseValid;
  const validGate = new Promise(resolve => { releaseValid = resolve; });
  await page.route('**/api/session', async route => {
    const response = await route.fetch(); const body = await response.json();
    body.session.session_valid = true; await validGate;
    await route.fulfill({response,json:body});
  });
  await page.reload(); await covered('valid conversation awaiting verdict');
  releaseValid(); await ready();
  check(await page.getByText('startup preserved history',{exact:true}).isVisible(), 'server-valid conversation shown despite local expiry');
  check(!await page.locator('#empty').isVisible(), 'server-valid conversation has no welcome page');
  await page.unroute('**/api/session');

  await seed(true);
  await page.route('**/api/reset', route => route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({code:'SERVICE_UNAVAILABLE'})}));
  await page.reload(); await page.locator('#startup-screen[data-state="error"]').waitFor();
  await covered('reset failure');
  check((await stored()).includes('startup preserved history'), 'failed reset retains stored history');
  await page.unroute('**/api/reset'); await page.locator('#startup-retry').click(); await ready();
  check(await page.locator('#empty').isVisible(), 'retry completes original reset');

  await seed(false);
  await page.route('**/api/session', route => route.abort('internetdisconnected'));
  await page.reload(); await page.locator('#startup-screen[data-state="error"]').waitFor();
  await covered('network failure');
  check((await stored()).includes('startup preserved history'), 'network failure retains stored history');
  await page.unroute('**/api/session'); await page.locator('#startup-retry').click(); await ready();
  check(await page.getByText('startup preserved history',{exact:true}).isVisible(), 'fresh local transcript preserved on recovery');
  await page.emulateMedia({reducedMotion:'reduce'});
  check(await page.locator('.startup-spinner').evaluate(el => getComputedStyle(el).animationName) === 'none', 'reduced-motion spinner is static');
  await page.emulateMedia({reducedMotion:'no-preference'});
  const counts = await (await page.request.get(page.url().split('/').slice(0,3).join('/') + '/fixture/counts')).json();
  check(counts.calls === 0, 'startup acceptance performs zero provider calls');
  return {checks};
}
