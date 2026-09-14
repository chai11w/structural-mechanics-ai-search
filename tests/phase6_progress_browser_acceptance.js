async page => {
  const checks = [];
  const verify = (ok, message) => { if (!ok) throw Error(message); checks.push(message); };
  const origin = page.url().split('/').slice(0,3).join('/');
  await page.waitForFunction(() => document.querySelector('#status-text').textContent === '准备就绪');
  verify(!(await page.locator('#execution-panel').isVisible()), 'product control panel stays hidden');
  await page.locator('#file').setInputFiles('.tmp_tests/phase6_5/synthetic-page.png');
  for (const message of ['正在检查图片并决定处理路线…', '正在理解整页题目和图形关系…',
    '已完成 1/2 张自动裁图校验…', '已完成 2/2 张自动裁图校验…']) {
    await page.waitForFunction(value => [...document.querySelectorAll('.message.pending .message-text')]
      .some(node => node.textContent.includes(value)), message, {timeout:10000});
    checks.push('pending bubble: ' + message);
  }
  await page.screenshot({path:'output/playwright/phase6-progress-restored.png',fullPage:true});
  await page.getByRole('button', {name:'四-1',exact:true}).waitFor();
  const result = await (await page.context().request.get(origin + '/fixture/counts')).json();
  verify(result.calls.length === 1, 'progress changes add no model calls');
  await page.reload();
  await page.getByRole('button', {name:'四-1',exact:true}).waitFor();
  verify(!(await page.locator('#execution-panel').isVisible()), 'refresh keeps product panel hidden');
  await page.locator('#top-new-chat').click();
  await page.waitForFunction(() => document.querySelector('#status-text').textContent === '任务状态已更新');
  verify(!(await page.locator('#execution-panel').isVisible()), 'new conversation works with panel hidden');
  return {checks, calls:result.calls.length};
}
