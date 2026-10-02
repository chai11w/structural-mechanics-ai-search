async page => {
  const checks = [];
  const check = (value, name) => { if (!value) throw Error(name); checks.push(name); };
  const firstResult = await page.getByRole('button', { name: '选择', exact: true }).count();
  await page.getByRole('button', { name: '换题重新搜', exact: true }).last().click();
  await page.getByRole('button', { name: /^四-2/ }).waitFor();
  const posts = await page.evaluate(() => __businessPosts);
  await page.evaluate(() => { __bindingFailures = 2; __bindingNativeError = 'network'; });
  await page.getByRole('button', { name: /^四-2/ }).click();
  await page.getByText('连接暂时不稳定，本次任务尚未提交。请重新连接后继续。', { exact: true }).waitFor();
  check(await page.evaluate(n => __businessPosts === n, posts), 'second question failure sends no business task');
  check(!/Failed to fetch|signal is aborted/.test(await page.locator('body').innerText()), 'switch-question network failure is Chinese');
  check(await page.getByRole('button', { name: '选择', exact: true }).count() === firstResult, 'first result remains visible after second-question failure');
  await page.getByRole('button', { name: '重新连接', exact: true }).filter({ visible: true }).click();
  await page.waitForFunction(() => document.querySelector('#status-text').textContent === '准备就绪');
  check(await page.evaluate(() => !Object.keys(localStorage).some(k => k.startsWith('tiku-agent-session-request-fence-v1:pending:'))), 'reconnect leaves no unsent request fence');
  await page.evaluate(() => {
    const key = 'tiku-agent-current-chat-v2';
    const value = JSON.parse(localStorage.getItem(key));
    for (const message of ['Failed to fetch', 'Internal Server Error', 'Unexpected token <']) {
      value.messages.push({ message, variant: 'error', createdAt: Date.now() });
    }
    value.messages.push({ message: 'I wrote this message', me: true, createdAt: Date.now() });
    localStorage.setItem(key, JSON.stringify(value));
  });
  await page.reload();
  await page.waitForFunction(() => document.querySelector('#status-text').textContent === '准备就绪');
  check(!/Failed to fetch|Internal Server Error|Unexpected token/.test(await page.locator('body').innerText()), 'restored legacy error is displayed in Chinese');
  check((await page.locator('body').innerText()).includes('I wrote this message'), 'user-authored English is preserved');
  return { checks };
}
