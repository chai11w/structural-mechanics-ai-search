async page => {
  const origin=page.url().split('/').slice(0,3).join('/');
  const target=await page.evaluate(()=>{
    const key='tiku-agent-current-chat-v2';
    const saved=JSON.parse(localStorage.getItem(key));
    const item=saved.messages.find(m=>m.responseId && m.backgroundKey);
    if(!item)throw Error('no retained response fixture');
    saved.messages=saved.messages.filter(m=>m.responseId!==item.responseId);
    localStorage.setItem(key,JSON.stringify(saved));
    return {id:item.responseId,key:item.backgroundKey};
  });
  const reads=[];
  page.on('request',request=>{if(request.url().startsWith(origin+'/api/jobs'))reads.push(request.url());});
  const boot=page.waitForResponse(response=>response.url()===origin+'/api/session' && response.status()===200);
  await page.reload();await boot;
  await page.waitForFunction(()=>document.querySelector('#status-text').textContent==='准备就绪');
  await page.waitForTimeout(1200);
  const restored=await page.evaluate(id=>JSON.parse(localStorage.getItem('tiku-agent-current-chat-v2')).messages.some(m=>m.responseId===id),target.id);
  if(restored || reads.length)throw Error('completed record defeated history retention');
  return {retainedReceiptNotRefetched:true,removedHistoryNotResurrected:true,observationReads:reads.length};
}
