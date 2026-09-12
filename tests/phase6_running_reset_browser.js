async page => {
  const origin=page.url().split('/').slice(0,3).join('/');
  const context=page.context();
  const counts=async()=>(await context.request.get(origin+'/fixture/counts')).json();
  const gate=async release=>context.request.post(origin+'/fixture/gate',{data:{release}});
  await page.reload();
  await page.waitForFunction(()=>document.querySelector('#status-text').textContent==='准备就绪');
  const before=await counts();
  const text='reset-running-'+Math.random().toString(16).slice(2);
  try {
    await gate(false);
    await page.getByRole('textbox',{name:'消息',exact:true}).fill(text);
    await page.getByRole('button',{name:'发送消息',exact:true}).click();
    for(let n=0;n<100;n++){
      if((await counts()).calls===before.calls+1)break;
      await page.waitForTimeout(100);
    }
    if((await counts()).calls!==before.calls+1)throw Error('provider did not start');
    await page.locator('#top-new-chat').click();
    await page.waitForFunction(()=>document.querySelector('#status-text').textContent==='任务状态已更新',null,{timeout:10000});
    await gate(true);
    await page.waitForTimeout(1500);
    if(await page.getByText('reply:'+text,{exact:true}).count())throw Error('late result entered reset conversation');
    if(await page.getByText('会话已更新，原任务结果不会写入当前对话。',{exact:true}).count())throw Error('old observer overwrote reset status');
    if(await page.locator('#text').isDisabled())throw Error('old observer stranded busy UI');
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#status-text').textContent==='准备就绪',null,{timeout:10000});
    if(await page.getByText('reply:'+text,{exact:true}).count())throw Error('old receipt revived after refresh');
    const after=await counts();
    if(after.calls!==before.calls+1)throw Error('reset replayed the provider');
    return {callsAdded:1,lateResultSuppressed:true,lateNoticeSuppressed:true,refreshDoesNotReviveEpoch:true};
  } finally {await gate(true);}
}
