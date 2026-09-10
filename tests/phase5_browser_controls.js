async (page) => {
  // Diagnostic fixture only: the product keeps this panel hidden by default.
  await page.context().addInitScript(() => {
    window.addEventListener('DOMContentLoaded', () => {
      const style = document.createElement('style');
      style.textContent = '#execution-panel:not([hidden]) { display: block; }';
      document.head.appendChild(style);
    });
  });
  await page.goto('http://127.0.0.1:8910/fixture/start?mode=ready');
  await page.evaluate(()=>fetch('/fixture/release',{method:'POST'}));
  await page.evaluate(()=>window.__phase5Blocked);
  for(const other of page.context().pages()) if(other!==page) await other.close();
  await page.goto('http://127.0.0.1:8910/fixture/start?mode=blocked');
  const second = await page.context().newPage();
  await second.goto('http://127.0.0.1:8910/');
  await second.locator('#execution-panel').waitFor({state:'visible'});
  await page.evaluate(async () => {
    const view = await (await fetch('/api/execution')).json();
    const target = view.execution_control.controls.find(x=>x.action==='stop_child').target;
    const id = Date.now()+':'+crypto.randomUUID();
    localStorage.setItem('tiku-agent-session-request-fence-v1:pending:'+encodeURIComponent(id),id);
    const operation = {key:id,epoch:view.execution.epoch,state_version:view.execution.state_version};
    window.__phase5Blocked = navigator.locks.request('tiku-agent-session-request-v1',async()=>{
      window.__phase5LockHeld=true;
      try {
        const r=await fetch('/api/a3/crop/stream',{method:'POST',headers:{'Content-Type':'application/json',
          'X-Tiku-Operation':JSON.stringify(operation),'X-Session-Coordination-Version':'6','X-Session-Request-Fence':id},
          body:JSON.stringify({...target,bounds:{x:0,y:0,width:1,height:1}})});
        return {status:r.status,text:await r.text()};
      } finally {window.__phase5LockHeld=false;}
    });
  });
  await second.waitForFunction(async()=> (await (await fetch('/fixture/status')).json()).entered, null, {polling:100,timeout:10000});
  const lockHeld = await page.evaluate(()=>window.__phase5LockHeld);
  if(!lockHeld) throw Error('ordinary browser lock was not held');
  if (await second.locator('#a3-crop-back').isVisible()) await second.locator('#a3-crop-back').click();
  await second.locator('#execution-panel summary').click();
  await second.getByRole('button',{name:'停止当前题',exact:true}).click();
  await second.waitForFunction(()=>!document.querySelector('#execution-refresh').disabled && document.querySelector('#status-text').textContent==='任务状态已更新',null,{polling:100,timeout:10000});
  const before=await second.evaluate(async()=> (await (await fetch('/fixture/status')).json()));
  if(before.released || !before.entered) throw Error('control waited for provider release');
  await second.evaluate(()=>fetch('/fixture/release',{method:'POST'}));
  const late=await page.evaluate(()=>window.__phase5Blocked);
  const after=await second.evaluate(async()=> (await (await fetch('/api/execution')).json()));
  if(after.task_state.active_child_task!==null || after.task_state.current_unit!==null
      || after.task_state.workflow.phase!=='WAIT_UNIT_SELECTION') throw Error('late result resurrected stopped child');
  const firstNotice = await page.locator('#execution-note').textContent();
  const lateResultRejected = late.text.includes('"type": "error"') || late.text.includes('"type":"error"');
  if (!lateResultRejected || !firstNotice.includes('另一页面')) throw Error('late response or cross-tab invalidation missing');
  const crossTab = {lockHeld, providerReleasedAtStop:before.released, lateResultRejected,
    firstNotice, finalWorkflow:after.task_state.workflow.phase, pending:after.execution_control.pending};
  await second.close();

  // Recover a saved child after the parent commit was interrupted.
  await page.goto('http://127.0.0.1:8910/fixture/start?mode=crash');
  await page.locator('#execution-panel').waitFor({state:'visible'});
  if (await page.locator('#a3-crop-back').isVisible()) await page.locator('#a3-crop-back').click();
  await page.locator('#execution-panel summary').click();
  const countBefore = await page.evaluate(async()=>(await (await fetch('/fixture/status')).json()).analysis_count);
  await page.getByRole('button',{name:'核对并恢复已保存结果',exact:true}).click();
  await page.waitForFunction(()=>!document.querySelector('#execution-refresh').disabled && document.querySelector('#status-text').textContent==='任务状态已更新',null,{polling:100,timeout:10000});
  const countAfter = await page.evaluate(async()=>(await (await fetch('/fixture/status')).json()).analysis_count);
  if(countAfter!==countBefore) throw Error('recovery invoked the model again');

  // Commit an end-page command but deliberately lose its response.
  const sent=[];
  page.on('request',request=>{
    if(request.url().endsWith('/api/execution/control')) sent.push({body:request.postData(),operation:request.headers()['x-tiku-operation']});
  });
  const commandsBefore = await page.evaluate(async()=>(await (await fetch('/fixture/status')).json()).operations.filter(x=>x.kind==='control_execution').length);
  await page.route('**/api/execution/control',async route=>{await route.fetch();await route.abort('failed');},{times:1});
  await page.getByRole('button',{name:'结束本页',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#execution-note').textContent.includes('连接中断'),null,{polling:100,timeout:10000});
  if(!await page.evaluate(()=>!!localStorage.getItem(TikuExecutionControl.JOURNAL_KEY))) throw Error('lost command was not persisted');
  await page.reload();
  await page.locator('#execution-panel').waitFor({state:'visible'});
  await page.locator('#execution-panel summary').click();
  await page.locator('#execution-retry').click();
  await page.waitForFunction(()=>!document.querySelector('#execution-refresh').disabled && document.querySelector('#status-text').textContent==='任务状态已更新',null,{polling:100,timeout:10000});
  const commandsAfter = await page.evaluate(async()=>(await (await fetch('/fixture/status')).json()).operations.filter(x=>x.kind==='control_execution').length);
  if(commandsAfter!==commandsBefore+1 || sent.length!==2 || sent[0].operation!==sent[1].operation || sent[0].body!==sent[1].body)
    throw Error('lost response created a new command');
  if(await page.evaluate(()=>!!localStorage.getItem(TikuExecutionControl.JOURNAL_KEY))) throw Error('confirmed journal not retired');
  const lostResponse={requests:sent.length,commandsCreated:commandsAfter-commandsBefore,sameOperation:true,recoveredAfterReload:true};

  const epochBefore = await page.evaluate(async()=>(await (await fetch('/api/execution')).json()).execution.epoch);
  await page.locator('#top-new-chat').click();
  await page.waitForFunction(()=>!document.querySelector('#execution-refresh').disabled && document.querySelector('#status-text').textContent==='任务状态已更新',null,{polling:100,timeout:10000});
  const reset = await page.evaluate(async()=>(await (await fetch('/api/execution')).json()));
  if(reset.execution.epoch===epochBefore || reset.task_state.workflow.exists) throw Error('reset did not rotate and clear');
  await page.setViewportSize({width:390,height:844});
  if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)) throw Error('mobile horizontal overflow');
  await page.screenshot({path:'output/playwright/phase5-controls-mobile.png'});
  return {crossTab,recovery:{analysisCountBefore:countBefore,analysisCountAfter:countAfter},lostResponse,resetRotated:true,mobileWidth:390};
}
