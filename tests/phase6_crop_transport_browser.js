async (page, { imagePath, screenshotPath }) => {
  const checks = [];
  const check = (value, message) => { if (!value) throw Error(message); checks.push(message); };
  await page.addInitScript(() => {
    const realFetch = window.fetch.bind(window), timer = window.setTimeout.bind(window);
    window.setTimeout = (fn, ms, ...args) => timer(fn, ms === 15000 && window.__bindingFailures > 0 ? 80 : ms, ...args);
    window.__bindingFailures = 0; window.__bindingCalls = 0; window.__businessPosts = 0;
    window.__lostBusinessAck = 0; window.__transportAborts = [];
    window.__queryFailures = 0;
    window.fetch = async (input, options) => {
      const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
      if (path === '/api/jobs/session') {
        window.__bindingCalls++;
        if (window.__bindingFailures > 0) {
          window.__bindingFailures--;
          if (window.__bindingNativeError === 'network') throw new TypeError('Failed to fetch');
          return new Promise((resolve, reject) => options.signal.addEventListener('abort', () => {
            window.__transportAborts.push(options.signal.reason.message);
            reject(options.signal.reason);
          }, { once: true }));
        }
      }
      const business = ['/api/jobs', '/api/jobs/image'].includes(path) && options?.method === 'POST';
      if (/^\/api\/jobs\/[0-9a-f]{32}$/.test(path) && window.__queryFailures > 0) {
        window.__queryFailures--;
        throw new TypeError('Failed to fetch');
      }
      if (business) window.__businessPosts++;
      const response = await realFetch(input, options);
      if (business && window.__lostBusinessAck > 0) {
        window.__lostBusinessAck--;
        throw new DOMException('signal is aborted without reason', 'AbortError');
      }
      return response;
    };
  });
  await page.reload();
  await page.locator('#startup-screen').waitFor({ state: 'hidden' });
  await page.waitForFunction(() => document.querySelector('#status-text').textContent === '准备就绪');
  await page.locator('#file').setInputFiles(imagePath);
  await page.getByRole('button', { name: '四-1', exact: true }).click();
  await page.getByRole('region', { name: '裁剪结构图', exact: true }).waitFor();
  await page.getByAltText('原始整页题图', { exact: true }).evaluate(image => image.decode());
  const box = await page.getByAltText('原始整页题图', { exact: true }).boundingBox();
  await page.mouse.move(box.x + box.width * .1, box.y + box.height * .1);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * .8, box.y + box.height * .8, { steps: 8 });
  await page.mouse.up();
  const before = await page.evaluate(() => ({ posts: __businessPosts, bindings: __bindingCalls }));
  const bounds = await page.locator('#a3-selection').getAttribute('style');
  await page.evaluate(() => { __bindingFailures = 2; });
  await page.getByRole('button', { name: '提交并继续搜题', exact: true }).click();
  await page.getByText('连接暂时中断，本次裁剪尚未提交，裁剪范围已保留，请再次提交。', { exact: true }).first().waitFor();
  check(await page.evaluate(n => __businessPosts === n, before.posts), 'binding timeout sends no crop job');
  check(await page.evaluate(n => __bindingCalls === n + 2, before.bindings), 'binding retries exactly once');
  check(!/signal is aborted|Failed to fetch|SyntaxError/.test(await page.locator('body').innerText()), 'native English is never rendered');
  check(await page.locator('#a3-selection').getAttribute('style') === bounds, 'crop bounds survive timeout');
  check(await page.evaluate(() => !Object.keys(localStorage).some(k => k.startsWith('tiku-agent-session-request-fence-v1:pending:'))), 'unsubmitted crop leaves no blocking fence');
  await page.screenshot({ path: screenshotPath });
  check(await page.getByRole('button', { name: '提交并继续搜题', exact: true }).isEnabled(), 'crop can be resubmitted without leaving the crop view');
  await page.evaluate(() => { __bindingFailures = 1; __queryFailures = 5; });
  await page.getByRole('button', { name: '提交并继续搜题', exact: true }).click();
  await page.locator('#a3-reconnect').waitFor({ state: 'visible' });
  check(await page.locator('#a3-selection').getAttribute('style') === bounds, 'accepted crop keeps selection while result connection is interrupted');
  await page.setViewportSize({ width: 390, height: 844 });
  check((await page.locator('#a3-crop-status').innerText()).includes('请重新连接查看结果'),
    'crop recovery guidance survives rerender and resize');
  const reconnectBox = await page.locator('#a3-reconnect').boundingBox();
  check(reconnectBox && reconnectBox.y >= 0 && reconnectBox.y + reconnectBox.height <= 844,
    'crop reconnect stays within the mobile viewport');
  await page.screenshot({ path: screenshotPath.replace('.png', '-mobile-reconnect.png') });
  await page.locator('#a3-reconnect').click();
  await page.getByText('我还不能确定', { exact: false }).waitFor();
  check(await page.locator('#a3-reconnect').isHidden(), 'crop-panel reconnect restores original result and retires its button');
  await page.setViewportSize({ width: 1280, height: 900 });
  check(await page.evaluate(n => __businessPosts === n + 1, before.posts), 'transient abort recovers to exactly one crop submission');
  await page.evaluate(() => { __lostBusinessAck = 1; });
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('4力法');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await page.getByRole('button', { name: '选择', exact: true }).waitFor();
  check(await page.evaluate(n => __businessPosts === n + 2, before.posts), 'lost business ACK is queried without a duplicate submission');
  check(!/signal is aborted|Failed to fetch|SyntaxError/.test(await page.locator('body').innerText()), 'recovered result contains no native English error');
  return { checks };
}
